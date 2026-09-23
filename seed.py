"""Create the first admin account and some starting data.

    python seed.py                      # admin + courses + the three experiments
    python seed.py --demo               # ...plus sample students and bookings
    python seed.py --admin-email you@x  # override the admin address

Safe to run twice: existing rows are left alone.
"""
import argparse
import secrets
import sys
from datetime import timedelta

from app import create_app
from models import (Booking, Course, Department, Experiment, LabPi, User, db,
                    utcnow)

EXPERIMENTS = [
    {
        "name": "DC Motor Speed Control",
        "slug": "dc-motor-speed-control",
        "summary": "Drive a DC motor with PWM and close the loop on real-time RPM "
                   "feedback from an optical encoder.",
        "description": "You control a brushed DC motor through an H-bridge driven by "
                       "PWM from the board. An optical encoder feeds pulses back, and "
                       "the bench plots commanded duty against measured RPM so you can "
                       "see your controller settle - or fail to.",
        "objectives": "Generate PWM at a chosen frequency and duty cycle\n"
                      "Read encoder pulses and convert them to RPM\n"
                      "Close a PI loop and tune it against a step change\n"
                      "Export the response curve as CSV",
        "apparatus": "12 V brushed DC motor with optical encoder\n"
                     "L298N H-bridge driver\n"
                     "Bench supply, current-limited to 2 A",
        "max_duration_min": 60,
    },
    {
        "name": "Temperature & Humidity Monitoring",
        "slug": "temperature-humidity-monitoring",
        "summary": "Talk to a DHT22 over its one-wire protocol, log readings, and "
                   "watch them plot live.",
        "description": "The DHT22 speaks a timing-sensitive single-wire protocol you "
                       "implement yourself rather than pulling in a library. Readings "
                       "stream to the chart as they arrive, and a heater resistor beside "
                       "the sensor lets you drive a step change and watch it respond.",
        "objectives": "Implement the DHT22 start pulse and read the 40-bit frame\n"
                      "Verify the checksum and reject bad frames\n"
                      "Stream readings over serial at a fixed interval\n"
                      "Plot both channels and export the log",
        "apparatus": "DHT22 temperature and humidity sensor\n"
                     "Heater resistor with relay control\n"
                     "Arduino-compatible board",
        "max_duration_min": 60,
    },
    {
        "name": "Electronic Load Control",
        "slug": "electronic-load-control",
        "summary": "Sequence a stepper through angle, speed and direction with "
                   "precise timing.",
        "description": "A bipolar stepper on a microstepping driver. You write the "
                       "sequencing logic, and the bench shows you both the commanded "
                       "position and what the shaft encoder actually reports - missed "
                       "steps show up immediately.",
        "objectives": "Generate step and direction signals at a controlled rate\n"
                      "Move a precise number of degrees and verify against the encoder\n"
                      "Ramp acceleration to avoid losing steps\n"
                      "Reverse direction cleanly under load",
        "apparatus": "NEMA-17 bipolar stepper\n"
                     "A4988 microstepping driver\n"
                     "Shaft encoder and adjustable brake",
        "max_duration_min": 60,
    },
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--admin-email", default="admin@vlab.edu")
    ap.add_argument("--admin-password", default=None,
                    help="Default: generated and printed once.")
    ap.add_argument("--demo", action="store_true",
                    help="Also create sample students, a node and some bookings.")
    args = ap.parse_args()

    app = create_app()
    with app.app_context():
        db.create_all()

        dept = Department.query.filter_by(code="DESE").first()
        if not dept:
            dept = Department(name="Electronic Systems Engineering", code="DESE")
            db.session.add(dept)

        course = Course.query.filter_by(code="E3-241").first()
        if not course:
            course = Course(code="E3-241", name="Embedded Systems Laboratory",
                            description="Hands-on microcontroller work on real benches.")
            db.session.add(course)
            db.session.flush()

        for spec in EXPERIMENTS:
            if not Experiment.query.filter_by(slug=spec["slug"]).first():
                db.session.add(Experiment(course_id=course.id, **spec))

        admin = User.query.filter_by(email=args.admin_email).first()
        admin_pw = None
        if not admin:
            admin_pw = args.admin_password or secrets.token_urlsafe(12)
            admin = User(full_name="Lab Administrator", email=args.admin_email,
                         role="admin", department=dept, must_change_password=True)
            admin.set_password(admin_pw)
            db.session.add(admin)

        db.session.commit()

        if args.demo:
            _seed_demo(course)

        print("\n  Database ready.\n")
        if admin_pw:
            print(f"  Admin sign-in : {args.admin_email}")
            print(f"  Password      : {admin_pw}")
            print("  You'll be asked to change it on first sign-in.\n")
        else:
            print(f"  Admin {args.admin_email} already existed - password unchanged.\n")


def _seed_demo(course):
    students = [
        ("Asha Rao", "asha.rao@example.edu", "EE21B001"),
        ("Vikram Nair", "vikram.nair@example.edu", "EE21B002"),
        ("Priya Menon", "priya.menon@example.edu", "EE21B003"),
    ]
    # One random password each, shown once, same as the admin account above.
    # These were a single hardcoded string shared by all three and printed in
    # the README, so every deployment that ever ran --demo had three known
    # accounts on it - and nothing stops --demo being run against a database
    # that is already in real use.
    created = []
    for name, email, roll in students:
        if User.query.filter_by(email=email).first():
            continue
        u = User(full_name=name, email=email, roll_number=roll,
                 must_change_password=True)
        password = secrets.token_urlsafe(12)
        u.set_password(password)
        u.courses = [course]
        db.session.add(u)
        created.append((u, password))

    if not LabPi.query.filter_by(node_id="lab-bench-1").first():
        exp = Experiment.query.filter_by(slug="temperature-humidity-monitoring").first()
        db.session.add(LabPi(node_id="lab-bench-1", name="Lab Bench 1",
                             ip_address="<bench-host>", board="arduino",
                             location="DESE lab, bench 1",
                             experiment_id=exp.id if exp else None))
    db.session.commit()

    if created:
        exp = Experiment.query.filter_by(slug="dc-motor-speed-control").first()
        base = utcnow().replace(minute=0, second=0, microsecond=0) + timedelta(hours=2)
        for i, (u, _pw) in enumerate(created[:2]):
            start = base + timedelta(hours=i)
            db.session.add(Booking(user_id=u.id, experiment_id=exp.id,
                                   start_time=start, end_time=start + timedelta(hours=1)))
        db.session.commit()

    if created:
        print("\n  Demo students - each is asked to change this on first sign-in:")
        for u, password in created:
            print(f"    {u.email:30} {password}")
        print("\n  These are shown once and are not recoverable. Re-run with a "
              "fresh\n  database if you lose them.")


if __name__ == "__main__":
    sys.exit(main())
