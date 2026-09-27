"""Command-line administration, for when you cannot get in through the browser.

    python manage.py reset-password admin@vlab.edu
    python manage.py reset-password admin@vlab.edu --password "my-new-password"
    python manage.py make-admin someone@iisc.ac.in
    python manage.py list-admins
    python manage.py unlock admin@vlab.edu
    python manage.py scan-manuals

`reset-password` is the one that matters: re-running install.sh does not touch
an existing admin's password, so if the original was lost there was previously
no way back in short of deleting the database. This is that way back.

Run it from the project directory with the virtualenv active:

    source venv/bin/activate
    python manage.py reset-password admin@vlab.edu
"""
import argparse
import getpass
import secrets
import sys

from app import create_app
from models import Experiment, SystemLog, User, db, utcnow
from services.pdfguard import UnsafePDF, check_pdf

ALPHABET = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _generate(n=14):
    return "".join(secrets.choice(ALPHABET) for _ in range(n))


def _find(email):
    user = User.query.filter_by(email=email.strip().lower()).first()
    if not user:
        print(f"\n  No account with the address {email}.")
        known = User.query.order_by(User.email).limit(10).all()
        if known:
            print("  Accounts that do exist:")
            for u in known:
                print(f"    {u.email}  ({u.role})")
        sys.exit(1)
    return user


def reset_password(args):
    user = _find(args.email)

    if args.password:
        new = args.password
    elif args.prompt:
        new = getpass.getpass("  New password: ")
        if new != getpass.getpass("  Confirm:      "):
            print("\n  Those didn't match. Nothing changed.")
            sys.exit(1)
    else:
        new = _generate()

    if len(new) < 8:
        print("\n  Use at least 8 characters. Nothing changed.")
        sys.exit(1)

    user.set_password(new)
    # A password you chose yourself here is final; a generated one should be
    # replaced at first sign-in.
    user.must_change_password = not (args.password or args.prompt)
    user.is_active_flag = True
    SystemLog.write(f"Password reset from the command line for {user.email}",
                    level="warning", category="admin")
    db.session.commit()

    print(f"\n  Password reset for {user.email} ({user.role}).")
    if not (args.password or args.prompt):
        print(f"  New password: {new}")
        print("  You'll be asked to change it at first sign-in.")
    print(f"\n  Sign in at /{'admin/login' if user.is_admin else 'login'}\n")


def make_admin(args):
    user = _find(args.email)
    if user.is_admin:
        print(f"\n  {user.email} is already an administrator.\n")
        return
    user.role = "admin"
    SystemLog.write(f"{user.email} promoted to admin from the command line",
                    level="warning", category="admin")
    db.session.commit()
    print(f"\n  {user.email} is now an administrator.\n")


def list_admins(args):
    admins = User.query.filter_by(role="admin").order_by(User.email).all()
    if not admins:
        print("\n  There are no administrator accounts.")
        print("  Create one:  python seed.py --admin-email you@example.edu\n")
        return
    print(f"\n  {len(admins)} administrator account(s):\n")
    for u in admins:
        state = "active" if u.is_active else "DISABLED"
        seen = f"{u.last_login:%Y-%m-%d %H:%M}" if u.last_login else "never signed in"
        pending = "  (temporary password not yet changed)" if u.must_change_password else ""
        print(f"    {u.email:<32} {state:<9} last seen {seen}{pending}")
    print()


def unlock(args):
    user = _find(args.email)
    user.is_active_flag = True
    db.session.commit()
    print(f"\n  {user.email} is active again.\n")


def scan_manuals(args):
    """Re-check every stored lab manual against services/pdfguard. Manuals
    uploaded before that check existed were only checked by their header."""
    from flask import current_app
    folder = current_app.config["SOP_FOLDER"]
    bad = 0
    print(f"\n  Lab manuals in {folder}:")
    for exp in Experiment.query.filter(Experiment.sop_pdf.isnot(None)).order_by(Experiment.id):
        path = folder / exp.sop_pdf
        if not path.is_file():
            verdict = "MISSING"
        else:
            try:
                check_pdf(path)
                verdict = "ok"
            except UnsafePDF as e:
                verdict = f"REFUSED - {e}"
        if verdict != "ok":
            bad += 1
            if args.detach:
                exp.sop_pdf = None
                verdict += "  (detached)"
        print(f"    {exp.slug:<32} {exp.sop_pdf or '':<36} {verdict}")
    if args.detach and bad:
        SystemLog.write(f"scan-manuals detached {bad} lab manual(s) from the command line",
                        level="warning", category="admin")
        db.session.commit()
    print(f"\n  {bad} manual(s) need attention." if bad else "\n  All manuals pass.")
    if bad and not args.detach:
        print("  Re-upload them from Admin > Experiments, or rerun with --detach "
              "to stop serving them.")
    print()
    return 1 if bad and not args.detach else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("reset-password", help="Set a new password for an account.")
    p.add_argument("email")
    p.add_argument("--password", help="Set this exact password instead of generating one.")
    p.add_argument("--prompt", action="store_true", help="Type the new password interactively.")
    p.set_defaults(func=reset_password)

    p = sub.add_parser("make-admin", help="Promote an existing account to administrator.")
    p.add_argument("email")
    p.set_defaults(func=make_admin)

    p = sub.add_parser("list-admins", help="Show every administrator account.")
    p.set_defaults(func=list_admins)

    p = sub.add_parser("unlock", help="Re-enable a deactivated account.")
    p.add_argument("email")
    p.set_defaults(func=unlock)

    p = sub.add_parser("scan-manuals", help="Re-check every stored lab manual PDF.")
    p.add_argument("--detach", action="store_true",
                   help="Unlink refused or missing manuals from their experiments.")
    p.set_defaults(func=scan_manuals)

    args = ap.parse_args()
    app = create_app()
    with app.app_context():
        sys.exit(args.func(args) or 0)


if __name__ == "__main__":
    main()
