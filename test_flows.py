"""End-to-end checks over the real routes, no mocking of the app itself.

Run: python test_flows.py
"""
import io
import re
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from app import create_app
from config import Config
from models import (Booking, Course, Experiment, LabPi, Session, SystemLog,
                    User, db, utcnow)

PASSES, FAILURES = [], []

TEST_NODE_SECRET = "test-node-shared-secret-" + "x" * 32


def check(name, condition, detail=""):
    (PASSES if condition else FAILURES).append(name)
    print(f"  {'PASS' if condition else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not condition else ""))


def build_app(tmp):
    class T(Config):
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{tmp}/t.db"
        TESTING = True
        WTF_CSRF_ENABLED = False
        SECRET_KEY = "t" * 64
        NODE_SHARED_SECRET = TEST_NODE_SECRET
        PORTAL_BASE_URL = "http://testserver"
        SOP_FOLDER = Path(tmp) / "sop"
        UPLOAD_FOLDER = Path(tmp) / "up"
    app = create_app(T)
    app.config["SOP_FOLDER"].mkdir(parents=True, exist_ok=True)
    return app


def seed(app):
    with app.app_context():
        c1 = Course(code="E3-241", name="Embedded Systems Lab")
        c2 = Course(code="E9-201", name="Signal Processing Lab")
        db.session.add_all([c1, c2])
        db.session.flush()

        e1 = Experiment(name="DC Motor Speed Control", slug="dc-motor",
                        summary="PWM and RPM feedback.", course_id=c1.id,
                        max_duration_min=60, sop_pdf="dc-motor.pdf")
        e2 = Experiment(name="Fourier Analysis", slug="fourier",
                        summary="Not on the student's course.", course_id=c2.id)
        db.session.add_all([e1, e2])
        db.session.flush()

        (app.config["SOP_FOLDER"] / "dc-motor.pdf").write_bytes(b"%PDF-1.4 test\n")

        node = LabPi(node_id="bench-1", name="Bench 1", ip_address="10.0.0.9",
                     board="arduino", experiment_id=e1.id, api_token="node-token")
        node.last_seen = utcnow()
        db.session.add(node)

        admin = User(full_name="Admin", email="admin@t.edu", role="admin")
        admin.set_password("adminpass1")
        student = User(full_name="Asha Rao", email="asha@t.edu", roll_number="EE1")
        student.set_password("studentpass1")
        student.courses = [c1]
        other = User(full_name="Vikram", email="vik@t.edu")
        other.set_password("otherpass1")
        other.courses = [c1]
        db.session.add_all([admin, student, other])
        db.session.commit()
        return {"e1": e1.id, "e2": e2.id, "node": node.id}


def login(client, email, password):
    return client.post("/login", data={"email": email, "password": password},
                       follow_redirects=True)


def main():
    tmp = tempfile.mkdtemp()
    app = build_app(tmp)
    ids = seed(app)
    hdr = {"X-Node-Secret": TEST_NODE_SECRET}
    node_hdr = {**hdr, "X-Node-Token": "node-token"}

    print("\nStartup secret validation")
    from config import Config as _Cfg

    class _BadSecret(_Cfg):
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{tmp}/bad.db"
        TESTING = True
        SECRET_KEY = "dev-only-change-me"
        NODE_SHARED_SECRET = "n" * 40
        SOP_FOLDER = Path(tmp) / "sop"
        UPLOAD_FOLDER = Path(tmp) / "up"

    class _ShortSecret(_BadSecret):
        SECRET_KEY = "short"

    class _BadNodeSecret(_BadSecret):
        SECRET_KEY = "s" * 40
        NODE_SHARED_SECRET = "change-this-node-secret"

    for label, cfg in (("the shipped placeholder SECRET_KEY", _BadSecret),
                       ("a too-short SECRET_KEY", _ShortSecret),
                       ("the shipped placeholder NODE_SHARED_SECRET", _BadNodeSecret)):
        refused = False
        try:
            create_app(cfg)
        except RuntimeError:
            refused = True
        check(f"startup refuses {label}", refused)

    print("\nAuthentication")
    with app.test_client() as c:
        r = login(c, "asha@t.edu", "wrongpass")
        check("bad password is rejected", r.status_code == 401)
        r = login(c, "asha@t.edu", "studentpass1")
        check("correct password signs in", b"My experiments" in r.data)

    print("\nSession invalidation")
    with app.test_client() as c:
        login(c, "asha@t.edu", "studentpass1")
        with app.app_context():
            before = User.query.filter_by(email="asha@t.edu").first().session_token
        c.get("/logout")
        with app.app_context():
            after = User.query.filter_by(email="asha@t.edu").first().session_token
        check("login issues a session token", before is not None)
        check("logout rotates the session token so other sessions die",
              before is not None and after is not None and before != after)

    print("\nIdle session timeout")
    with app.test_client() as c:
        login(c, "asha@t.edu", "studentpass1")
        with c.session_transaction() as sess:
            sess["last_active"] = (utcnow() - timedelta(minutes=31)).isoformat()
        r = c.get("/dashboard", follow_redirects=False)
        check("idle timeout redirects instead of erroring",
              r.status_code == 302, f"got {r.status_code}")
        check("idle timeout sends the user to sign-in",
              "/login" in r.headers.get("Location", ""))

    print("\nDuplicate camera proxy")
    rules = {str(r) for r in app.url_map.iter_rules()}
    check("unauthenticated /session/<key>/camera-stream is gone",
          "/session/<key>/camera-stream" not in rules)
    check("the login-guarded camera route still exists",
          "/lab/<session_key>/camera-stream" in rules)
    with app.test_client() as c:
        r = c.get("/session/ABC1234567/camera-stream", follow_redirects=False)
        check("old camera URL does not serve a stream", r.status_code == 404,
              f"got {r.status_code}")

    print("\nCourse-scoped visibility")
    with app.test_client() as c:
        login(c, "asha@t.edu", "studentpass1")
        r = c.get("/dashboard")
        check("enrolled experiment is listed", b"DC Motor Speed Control" in r.data)
        check("unenrolled experiment is hidden", b"Fourier Analysis" not in r.data)
        r = c.get(f"/experiment/{ids['e2']}/details")
        check("unenrolled experiment detail is 404", r.status_code == 404)
        r = c.get(f"/experiment/{ids['e2']}/book")
        check("unenrolled experiment booking is 404", r.status_code == 404)

    print("\nExperiment detail popup and manual")
    with app.test_client() as c:
        login(c, "asha@t.edu", "studentpass1")
        r = c.get(f"/experiment/{ids['e1']}/details")
        d = r.get_json()
        check("detail JSON returns the experiment", d["name"] == "DC Motor Speed Control")
        check("detail reports a manual", d["has_manual"] is True)
        r = c.get(f"/experiment/{ids['e1']}/manual")
        check("manual downloads as PDF", r.status_code == 200 and r.data.startswith(b"%PDF"))
        check("manual is an attachment",
              "attachment" in r.headers.get("Content-Disposition", ""))

    print("\nBooking")
    slot = (utcnow() + timedelta(days=1)).replace(minute=0, second=0, microsecond=0)
    slot_str = slot.strftime("%Y-%m-%dT%H:%M")
    with app.test_client() as c:
        login(c, "asha@t.edu", "studentpass1")
        r = c.get(f"/experiment/{ids['e1']}/book")
        check("slot picker renders 24 hourly slots", r.data.count(b'name="slot"') == 24)

        r = c.post(f"/experiment/{ids['e1']}/book", data={"slot": slot_str},
                   follow_redirects=True)
        check("booking succeeds", b"Booking confirmed" in r.data)
        check("booking redirects to My bookings", b"My bookings" in r.data)

        past = (utcnow() - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
        r = c.post(f"/experiment/{ids['e1']}/book", data={"slot": past},
                   follow_redirects=True)
        check("past slot is refused", b"already passed" in r.data)

    with app.test_client() as c:
        login(c, "vik@t.edu", "otherpass1")
        r = c.post(f"/experiment/{ids['e1']}/book", data={"slot": slot_str},
                   follow_redirects=True)
        check("double-booking the same slot is refused", b"booked that slot" in r.data)

    print("\nTimes are IST in, UTC stored")
    from services import timeutil
    with app.app_context():
        # 14:00 IST is 08:30 UTC. If these ever match, the conversion is a no-op.
        local = datetime(2026, 9, 1, 14, 0)
        stored = timeutil.from_local(local)
        check("IST input converts to UTC for storage",
              stored == datetime(2026, 9, 1, 8, 30), str(stored))
        check("stored UTC renders back as IST",
              timeutil.fmt(stored, "%H:%M") == "14:00",
              timeutil.fmt(stored, "%H:%M"))
        check("round trip is lossless", timeutil.from_local(
              timeutil.to_local(stored).replace(tzinfo=None)) == stored)

    with app.test_client() as c:
        login(c, "asha@t.edu", "studentpass1")
        # Book 23:00 IST tomorrow: stored as 17:30 UTC the same day.
        tomorrow = timeutil.local_today() + timedelta(days=1)
        late = tomorrow.strftime("%Y-%m-%dT23:00")
        r = c.post(f"/experiment/{ids['e1']}/book", data={"slot": late},
                   follow_redirects=True)
        check("a late-evening IST slot books", b"Booking confirmed" in r.data)
        with app.app_context():
            b = (Booking.query.filter(Booking.user_id ==
                 User.query.filter_by(email="asha@t.edu").first().id)
                 .order_by(Booking.id.desc()).first())
            check("23:00 IST is stored as 17:30 UTC",
                  b.start_time.hour == 17 and b.start_time.minute == 30,
                  f"{b.start_time}")
            check("it displays back as 23:00 IST",
                  timeutil.fmt(b.start_time, "%H:%M") == "23:00",
                  timeutil.fmt(b.start_time, "%H:%M"))
        r = c.get(f"/experiment/{ids['e1']}/book?date={tomorrow.isoformat()}")
        check("that slot now shows as taken in the grid",
              b"is-yours" in r.data or b"is-taken" in r.data)
        # The whole IST day must be offered, including hours that fall on the
        # previous UTC day - the bug that made early slots look past.
        check("the grid still offers all 24 IST hours",
              r.data.count(b'name="slot"') == 24)

    print("\nStarting a session")
    with app.app_context():
        b = Booking.query.first()
        code = b.code
        b.start_time = utcnow() - timedelta(minutes=1)
        b.end_time = utcnow() + timedelta(minutes=59)
        db.session.commit()

    with app.test_client() as c:
        login(c, "asha@t.edu", "studentpass1")
        with mock.patch("services.nodes.requests.post") as post:
            post.return_value.raise_for_status = lambda: None
            r = c.get(f"/booking/{code}/start")
        check("start redirects to the portal lab page", r.status_code == 302)
        check("redirect targets the portal, not the raw Pi",
              "/lab/" in r.headers["Location"],
              r.headers.get("Location", ""))
        with app.app_context():
            key = Session.query.join(Booking).filter(
                Booking.code == code).order_by(Session.id.desc()).first().session_key

        r2 = c.get(f"/booking/{code}/start")
        check("re-starting keeps the same lab page", "/lab/" in r2.headers["Location"])

    with app.test_client() as c:
        login(c, "vik@t.edu", "otherpass1")
        r = c.get(f"/booking/{code}/start")
        check("another student cannot start your booking", r.status_code == 404)

    print("\nNode session endpoints require the node's own token")
    with app.test_client() as c:
        r = c.post("/api/node/session/validate", headers=hdr,
                   json={"session_key": key, "node_id": "bench-1"})
        check("validate with the shared secret alone is refused",
              r.status_code == 401, f"got {r.status_code}")
        r = c.post("/api/node/session/end", headers=hdr,
                   json={"session_key": key, "node_id": "bench-1"})
        check("session/end with the shared secret alone is refused",
              r.status_code == 401, f"got {r.status_code}")
        r = c.post("/api/node/session/end",
                   headers={**hdr, "X-Node-Token": "wrong-token"},
                   json={"session_key": key, "node_id": "bench-1"})
        check("session/end with a wrong node token is refused",
              r.status_code == 401, f"got {r.status_code}")

    print("\nFirmware upload authorisation")

    def _fw():
        return {"firmware": (io.BytesIO(b":100000000C9434000C943E000C943E000C943E00A8\n:00000001FF\n"), "blink.hex"), "board": "arduino"}

    with app.test_client() as c:
        r = c.post(f"/session/{key}/firmware", data=_fw(),
                   content_type="multipart/form-data")
        check("unauthenticated firmware upload is refused",
              r.status_code in (401, 403), f"got {r.status_code}")
    with app.test_client() as c:
        login(c, "vik@t.edu", "otherpass1")
        r = c.post(f"/session/{key}/firmware", data=_fw(),
                   content_type="multipart/form-data")
        check("another student cannot flash your bench",
              r.status_code == 403, f"got {r.status_code}")
    with app.test_client() as c:
        login(c, "asha@t.edu", "studentpass1")
        with mock.patch("services.nodes.flash") as fl:
            fl.return_value = (True, "flashed")
            r = c.post(f"/session/{key}/firmware", data=_fw(),
                       content_type="multipart/form-data")
        check("the owning student can still flash",
              r.status_code == 200, f"got {r.status_code} {r.data[:120]}")

    print("\nNode API")
    with app.test_client() as c:
        r = c.post("/api/node/register", json={"node_id": "bench-2", "name": "Bench 2",
                                               "ip": "10.0.0.10", "experiment_slug": "dc-motor"})
        check("register without the secret is rejected", r.status_code == 401)

        r = c.post("/api/node/register", headers=hdr,
                   json={"node_id": "bench-2", "name": "Bench 2", "ip": "10.0.0.10",
                         "experiment_slug": "dc-motor", "board": "esp32"})
        check("register with the secret works", r.status_code == 200 and r.get_json()["created"])
        check("register returns a node token", bool(r.get_json().get("api_token")))

        r = c.post("/api/node/heartbeat", headers=node_hdr,
                   json={"node_id": "bench-1", "cpu": 31.5, "ram": 48.0, "temp": 52.3,
                         "battery_percent": 88, "battery_status": "AC", "uptime": 7200})
        check("heartbeat is accepted", r.status_code == 200)

        r = c.post("/api/node/heartbeat", headers={**hdr, "X-Node-Token": "wrong"},
                   json={"node_id": "bench-1", "cpu": 1})
        check("heartbeat with a wrong token is rejected", r.status_code == 401)

        with app.app_context():
            bench2 = LabPi.query.filter_by(node_id="bench-2").first()
            bench2_token = bench2.api_token if bench2 else None

        r = c.get("/api/node/bench-1/sessions", headers=node_hdr)
        sessions = r.get_json()["sessions"]
        check("poller sees the live session", len(sessions) == 1)
        check("session carries who it belongs to", sessions[0]["user"] == "Asha Rao")

        r = c.post("/api/node/session/validate", headers=node_hdr,
                   json={"session_key": key, "node_id": "bench-1"})
        check("node validates a good key", r.get_json()["valid"] is True)

        r = c.post("/api/node/session/validate", headers=node_hdr,
                   json={"session_key": "NOPE123456", "node_id": "bench-1"})
        check("node rejects an unknown key", r.status_code == 404)

        r = c.post("/api/node/session/validate",
                   headers={**hdr, "X-Node-Token": bench2_token},
                   json={"session_key": key, "node_id": "bench-2"})
        check("another node cannot validate this node's key", r.status_code == 403)

    print("\nSession expiry")
    with app.app_context():
        s = Session.query.filter_by(session_key=key).first()
        s.expires_at = utcnow() - timedelta(minutes=1)
        db.session.commit()
    with app.test_client() as c:
        r = c.post("/api/node/session/validate", headers=node_hdr,
                   json={"session_key": key, "node_id": "bench-1"})
        check("expired key stops validating", r.status_code == 403)
        r = c.get("/api/node/bench-1/sessions", headers=node_hdr)
        check("expired session leaves the poll list", r.get_json()["sessions"] == [])

    print("\nAdmin: access control")
    with app.test_client() as c:
        login(c, "asha@t.edu", "studentpass1")
        check("student cannot reach admin", c.get("/admin/").status_code == 403)
        check("student cannot reach users", c.get("/admin/users").status_code == 403)
    with app.test_client() as c:
        r = c.get("/admin/", follow_redirects=True)
        check("signed-out user is sent to sign in", b"Sign in" in r.data)

    print("\nAdmin: bulk upload")
    csv_data = (
        "full_name,email,roll_number,courses\n"
        "New Student,new@t.edu,EE99,E3-241\n"
        "Second Student,second@t.edu,EE98,E3-241;E9-201\n"
        "Bad Row,not-an-email,EE97,E3-241\n"
        "Duplicate,asha@t.edu,EE96,E3-241\n"
        "Unknown Course,third@t.edu,EE95,NOPE-999\n"
    )
    with app.test_client() as c:
        login(c, "admin@t.edu", "adminpass1")
        with mock.patch("services.mailer._send", return_value=True) as sent:
            r = c.post("/admin/users/bulk-upload", follow_redirects=True,
                       data={"file": (io.BytesIO(csv_data.encode()), "users.csv")},
                       content_type="multipart/form-data")
        check("bulk upload reports what it created", b"Added 3 users" in r.data)
        check("invitation emails are sent", sent.call_count == 3, f"{sent.call_count} calls")
        check("invalid email is reported", b"not a valid email" in r.data)
        check("existing address is reported", b"already has an account" in r.data)
        check("unknown course code is reported", b"unknown course code" in r.data)

    with app.app_context():
        u = User.query.filter_by(email="new@t.edu").first()
        check("imported user exists", u is not None)
        check("imported user is enrolled", [c.code for c in u.courses] == ["E3-241"])
        check("imported user must change password", u.must_change_password is True)
        u2 = User.query.filter_by(email="second@t.edu").first()
        check("multi-course enrolment works", len(u2.courses) == 2)
        check("row with a bad email created nothing",
              User.query.filter_by(full_name="Bad Row").first() is None)

    print("\nAdmin: forced password change")
    with app.test_client() as c:
        with app.app_context():
            u = User.query.filter_by(email="new@t.edu").first()
            u.set_password("temp-pass-1")
            db.session.commit()
        r = login(c, "new@t.edu", "temp-pass-1")
        check("first sign-in forces a password change", b"Choose your password" in r.data)
        r = c.get("/dashboard", follow_redirects=True)
        check("dashboard is reachable after the prompt", r.status_code == 200)
        r = c.post("/change-password", follow_redirects=True,
                   data={"new_password": "my-new-password", "confirm_password": "my-new-password"})
        check("password change succeeds", b"Password updated" in r.data)

    print("\nAdmin: devices added by IP")
    with app.test_client() as c:
        login(c, "admin@t.edu", "adminpass1")
        with mock.patch("services.nodes.requests.get") as get:
            get.return_value.status_code = 200
            get.return_value.json = lambda: {"node_id": "bench-3", "name": "Bench 3",
                                             "board": "esp32", "experiment_slug": "dc-motor"}
            r = c.post("/admin/devices/add", data={"ip_address": "10.0.0.11", "port": "5000"},
                       follow_redirects=True)
        check("probing an IP adds the node", b"Bench 3 added" in r.data)
        with app.app_context():
            n = LabPi.query.filter_by(node_id="bench-3").first()
            check("probed node keeps its IP", n.ip_address == "10.0.0.11")
            check("probed node is linked to its experiment", n.experiment.slug == "dc-motor")

        import requests as rq
        with mock.patch("services.nodes.requests.get",
                        side_effect=rq.exceptions.ConnectionError()):
            r = c.post("/admin/devices/add", data={"ip_address": "10.0.0.99"},
                       follow_redirects=True)
        check("unreachable IP gives a useful message", b"Could not connect" in r.data)

        # A Pi with no /api/info still identifies via another endpoint.
        with mock.patch("services.nodes.requests.get") as get:
            resp = mock.Mock()
            resp.status_code = 200
            resp.json = lambda: {"experiment_name": "DC Motor", "board_type": "arduino"}
            get.side_effect = [mock.Mock(status_code=404), resp]
            r = c.post("/admin/devices/add",
                       data={"ip_address": "10.0.0.12", "node_id": "bench-4"},
                       follow_redirects=True)
        check("a Pi without /api/info is still added", b"bench-4" in r.data or b"added" in r.data)

        # Offline Pi plus a known ID: added anyway rather than refused.
        with mock.patch("services.nodes.requests.get",
                        side_effect=rq.exceptions.ConnectionError()):
            r = c.post("/admin/devices/add",
                       data={"ip_address": "10.0.0.13", "node_id": "bench-offline"},
                       follow_redirects=True)
        check("an offline node can be added by ID", b"did not answer" in r.data)
        with app.app_context():
            check("the manually added node exists",
                  LabPi.query.filter_by(node_id="bench-offline").first() is not None)

    print("\nAdmin: node UI settings (proxies live to the node's /api/admin/*)")
    with app.test_client() as c:
        login(c, "admin@t.edu", "adminpass1")

        cfg = {
            "controls": {"board_select": True, "flash_firmware": False},
            "defaults": {"main_view": "plotter", "dynamic_controls_visible": False,
                        "serial_plotter_allow_port_switch": True,
                        "serial_plotter_default_port_id": "", "serial_plotter_required_prefixes": [],
                        "debug_board_id": "", "debug_port": "", "osc_board_id": "", "osc_port": ""},
            "serial_ports": [{"id": "p1", "label": "Student MCU", "port": "", "baud": 115200,
                              "student_visible": True, "auto_connect": False,
                              "allow_disconnect": True, "is_primary_target": True}],
            "required_controls": [{"id": "c1", "type": "slider", "label": "Speed", "portId": "p1",
                                   "min": 0, "max": 255, "precision": 0, "cmdFormat": "{value}"}],
            "experiment_name": "DC Motor Speed Control",
            "available_ports": [], "osc_available_ports": [],
        }
        with mock.patch("services.nodes.requests.request") as req:
            req.return_value.status_code = 200
            req.return_value.raise_for_status = lambda: None
            req.return_value.json = lambda: cfg
            r = c.get(f"/admin/lab-pi/{ids['node']}/ui-settings")
        check("UI settings page renders for an online node", r.status_code == 200)
        check("shows the node's serial port profile", b"Student MCU" in r.data)
        check("shows the node's required dynamic control", b"Speed" in r.data)
        check("save button targets the same node", str(ids["node"]).encode() in r.data)

        with mock.patch("services.nodes.requests.request") as req:
            req.side_effect = Exception("Connection refused")
            r = c.get(f"/admin/lab-pi/{ids['node']}/ui-settings", follow_redirects=True)
        check("a node without the admin API yet fails clearly, not with a 500",
              r.status_code == 200 and b"Could not reach" in r.data)

        with app.app_context():
            from datetime import timedelta as _td
            offline = LabPi(node_id="bench-cold", name="Cold Bench", ip_address="10.0.0.50")
            offline.last_seen = utcnow() - _td(hours=1)
            db.session.add(offline); db.session.commit()
            offline_id = offline.id
        r = c.get(f"/admin/lab-pi/{offline_id}/ui-settings", follow_redirects=True)
        check("an offline node redirects instead of trying to reach it",
              b"is not online" in r.data)

        with mock.patch("services.nodes.requests.request") as req:
            req.return_value.status_code = 200
            req.return_value.raise_for_status = lambda: None
            # follow_redirects=True means this mock also serves the page's own
            # GET /api/admin/ui-config reload after the redirect - give every
            # call the full cfg shape, not just a bare {"ok": True}.
            req.return_value.json = lambda: cfg
            r = c.post(f"/admin/lab-pi/{ids['node']}/ui-settings",
                       data={"all_control_keys": "board_select,flash_firmware",
                             "control_board_select": "on", "main_view": "plotter"},
                       follow_redirects=True)
            posted_body = req.call_args_list[0].kwargs.get("json")
        check("save posts the built ui-config back to the node",
              r.status_code == 200 and b"saved and pushed" in r.data)
        check("save builds controls from the checked boxes",
              posted_body["controls"] == {"board_select": True, "flash_firmware": False})

    print("\nAdmin: bookings and user detail")
    with app.test_client() as c:
        login(c, "admin@t.edu", "adminpass1")
        r = c.get("/admin/bookings")
        check("bookings list renders", b"Asha Rao" in r.data)
        r = c.get(f"/admin/bookings/{code}")
        check("booking detail shows the booking", code.encode() in r.data)
        check("booking detail shows the student's email", b"asha@t.edu" in r.data)
        check("booking detail shows the session key", key.encode() in r.data)
        r = c.get("/admin/bookings?status=cancelled")
        check("status filter narrows the list", b"No bookings match" in r.data)
        with app.app_context():
            uid = User.query.filter_by(email="asha@t.edu").first().id
        r = c.get(f"/admin/users/{uid}")
        check("user detail shows booking history", code.encode() in r.data)

    print("\nLegacy compatibility is off by default")
    from config import Config as _DefaultCfg
    check("LEGACY_NODE_COMPAT defaults to off", _DefaultCfg.LEGACY_NODE_COMPAT is False)
    with app.test_client() as c:
        r = c.post("/api/lab-pi/heartbeat", json={"node_id": "anon-1", "cpu": 1})
        check("anonymous legacy heartbeat is refused by default",
              r.status_code == 401, f"got {r.status_code}")
        r = c.post("/api/lab-pi/register", json={"lab_pi_id": "anon-1"})
        check("anonymous legacy register is refused by default",
              r.status_code == 401, f"got {r.status_code}")
        r = c.post("/api/lab-pi/register", headers=hdr,
                   json={"lab_pi_id": "bench-1"})
        check("authenticated legacy register no longer echoes api_token",
              r.status_code == 200 and "api_token" not in (r.get_json() or {}))
    with app.test_client() as c:
        r = c.post("/api/node/register", headers=hdr, json={"node_id": "bench-1"})
        body = r.get_json() or {}
        check("re-registering a known node does not echo its api_token",
              r.status_code == 200 and "api_token" not in body,
              f"got {sorted(body)}")

    print("\nLegacy /api/lab-pi compatibility")
    with app.test_client() as c:
        # An unknown Pi heartbeating with no secret, exactly as existing
        # hardware does. It must be accepted and registered, not 404'd.
        r = c.post("/api/lab-pi/heartbeat", headers=hdr, json={
            "node_id": "lab-legacy-1", "cpu": 22.5, "ram": 44.0,
            "temp": 47.1, "uptime": 3600})
        check("legacy heartbeat is accepted", r.status_code == 200, str(r.status_code))
        with app.app_context():
            n = LabPi.query.filter_by(node_id="lab-legacy-1").first()
            check("unknown legacy node is auto-registered", n is not None)
            check("legacy node shows online", n.is_online)
            check("legacy telemetry is stored",
                  n.latest is not None and n.latest.cpu_percent == 22.5)

        # Alternative field spellings from older firmware.
        r = c.post("/api/lab-pi/heartbeat", headers=hdr, json={
            "lab_pi_id": "lab-legacy-2", "cpu_usage": 60, "memory": 30,
            "cpu_temp": 55.5, "battery": 74, "power_source": "battery"})
        check("alternative field names are understood", r.status_code == 200)
        with app.app_context():
            n2 = LabPi.query.filter_by(node_id="lab-legacy-2").first()
            check("cpu_usage maps to cpu", n2.latest.cpu_percent == 60)
            check("battery state is normalised", n2.latest.battery_status == "Battery")

        r = c.post("/api/lab-pi/register", headers=hdr, json={
            "node_id": "lab-legacy-1", "name": "Bench Legacy",
            "experiment_slug": "dc-motor", "board": "esp32"})
        check("legacy register works", r.status_code == 200)
        with app.app_context():
            n = LabPi.query.filter_by(node_id="lab-legacy-1").first()
            check("legacy register links the experiment", n.experiment.slug == "dc-motor")

        r = c.get("/api/lab-pi/lab-legacy-1/sessions", headers=hdr)
        check("legacy session poll works", r.status_code == 200)
        r = c.get("/api/lab-pi/never-heard-of-it/sessions", headers=hdr)
        check("unknown node polling gets an empty list, not an error",
              r.status_code == 200 and r.get_json()["sessions"] == [])

        # The exact payload remote_lab_pi sends today.
        r = c.post("/api/lab-pi/heartbeat",
                   headers={**hdr, "X-Lab-Pi-Id": "lab-real-1"},
                   json={"lab_pi_id": "lab-real-1", "name": "Lab Pi amar",
                         "ip_address": "<bench-1>", "mac_address": "b8:27:eb:01",
                         "status": "ONLINE", "session_active": False,
                         "current_session_key": None,
                         "cpu_usage": 18.4, "ram_usage": 37.2, "temperature": 51.6,
                         "battery_soc": 96, "battery_voltage": 4.1,
                         "battery_ac_status": "AC_CONNECTED", "battery_charging": True})
        check("the real Pi heartbeat payload is accepted", r.status_code == 200)
        with app.app_context():
            n = LabPi.query.filter_by(node_id="lab-real-1").first()
            check("real node registers itself", n is not None)
            check("its IP comes from ip_address", n.ip_address == "<bench-1>")
            check("cpu_usage is stored", n.latest.cpu_percent == 18.4)
            check("battery_soc is stored", n.latest.battery_percent == 96)
            check("AC_CONNECTED maps to AC", n.latest.battery_status == "AC")

        # X-Master-Api-Key is the header the deployed Pi actually sends.
        r = c.post("/api/lab-pi/heartbeat",
                   headers={"X-Master-Api-Key": TEST_NODE_SECRET},
                   json={"lab_pi_id": "lab-real-1", "cpu_usage": 20})
        check("X-Master-Api-Key is accepted", r.status_code == 200)
        r = c.post("/api/lab-pi/heartbeat",
                   headers={"X-Master-Api-Key": "nope"},
                   json={"lab_pi_id": "lab-real-1", "cpu_usage": 20})
        check("a wrong X-Master-Api-Key is refused", r.status_code == 401)

        # The poller's real endpoint.
        r = c.get("/api/lab-pi/lab-real-1/active-session", headers=hdr)
        check("active-session answers", r.status_code == 200)
        check("no session reads as stopped", r.get_json()["status"] == "stopped")
        r = c.get("/api/lab-pi/not-registered/active-session", headers=hdr)
        check("an unknown node gets 404 so the poller can say so",
              r.status_code == 404)

        # A wrong secret is still refused even in compat mode.
        r = c.post("/api/lab-pi/heartbeat", headers={"X-Node-Secret": "wrong"},
                   json={"node_id": "lab-legacy-1", "cpu": 1})
        check("a wrong secret is refused even in compat mode", r.status_code == 401)

    print("\nLEGACY_NODE_COMPAT cannot waive authentication")
    tmp2 = tempfile.mkdtemp()
    app2 = build_app(tmp2)
    app2.config["LEGACY_NODE_COMPAT"] = True
    with app2.test_client() as c:
        r = c.post("/api/lab-pi/heartbeat", json={"node_id": "x", "cpu": 1})
        check("even with compat explicitly ON, an unauthenticated node is refused",
              r.status_code == 401, f"got {r.status_code}")
        r = c.post("/api/lab-pi/heartbeat", headers=hdr,
                   json={"node_id": "x", "cpu": 1})
        check("with compat ON and a valid secret, the legacy path still works",
              r.status_code == 200, f"got {r.status_code}")

    print("\nAdmin sign-in is separate and role-checked")
    with app.test_client() as c:
        r = c.get("/admin/login")
        check("staff sign-in page renders", b"Administrator sign-in" in r.data)
        r = c.post("/admin/login", data={"email": "asha@t.edu", "password": "studentpass1"})
        check("a student is refused at the staff door", r.status_code == 403)
        check("refusal explains where to go instead", b"student sign-in" in r.data)
        r = c.post("/admin/login", data={"email": "admin@t.edu", "password": "adminpass1"})
        check("admin signs in and lands on the panel",
              r.status_code == 302 and "/admin" in r.headers["Location"])

    print("\nForced password change keeps admin chrome")
    with app.app_context():
        a = User.query.filter_by(email="admin@t.edu").first()
        a.must_change_password = True
        a.set_password("temp-admin-1")
        db.session.commit()
    with app.test_client() as c:
        r = login(c, "admin@t.edu", "temp-admin-1")
        check("admin is sent to change password", b"Choose your password" in r.data)
        check("admin sees the admin sidebar, not the student bar",
              b"Admin mode" in r.data and b"My bookings" not in r.data)
    with app.app_context():
        a = User.query.filter_by(email="admin@t.edu").first()
        a.must_change_password = False
        a.set_password("adminpass1")
        db.session.commit()

    print("\nFirmware upload gateway")
    from services import firmware as fwmod
    import io as _io
    good_hex = b":100000000C9434000C9446000C9446000C94460082\n" + b":00000001FF\n"

    with app.app_context():
        def rejects(name, data, board="arduino"):
            try:
                fwmod.validate(name, data, board); return None
            except fwmod.Rejected as e:
                return str(e)
        check("valid .hex passes", rejects("blink.hex", good_hex) is None)
        check("a .exe is rejected", rejects("evil.bin", b"MZ" + bytes([0x90,0]) + bytes(40), "esp32"))
        check("a shell script is rejected", rejects("x.hex", b"#!/bin/sh" + bytes([10]) + b"rm -rf /"))
        check("a zip is rejected", rejects("x.bin", b"PK" + bytes([3,4]) + bytes(40), "esp32"))
        check("wrong extension is rejected", rejects("firmware.txt", good_hex))
        check("empty file is rejected", rejects("x.hex", b""))
        check("a raw .bin renamed to .hex is caught", rejects("fake.hex", bytes([0,1,2,3]) * 10))
        check("board/extension mismatch is caught", rejects("blink.bin", bytes(200), "arduino"))
        check("oversize file is rejected", rejects("big.bin", bytes(9*1024*1024), "esp32"))
        check("a CSV renamed to .out is caught",
              rejects("user_registration_template.out",
                      b"full_name,email,role,roll_number\nAsha Rao,asha@t.edu,user,EE1\n", "tiva"))
        check("a text README renamed to .bin is caught",
              rejects("readme.bin", b"This is a description of the project.\n" * 5, "generic"))
        check("binary-shaped .out content (NUL bytes, non-printable) is NOT caught by the text check",
              rejects("real.out", bytes([0x7f, 0, 1, 2] * 20) + bytes(50) + bytes([0xff, 0xfe] * 20), "tiva") is None)
        check("tiva's real .out workflow still passes",
              rejects("sw1.out", bytes([0x7f, 0, 1, 2] * 20) + bytes(50), "tiva") is None)
        check("a .uf2 for tiva is rejected (BOARD_EXTENSIONS previously had no tiva entry at all)",
              rejects("x.uf2", b"UF2\n" + bytes(50), "tiva"))
        check("a .uf2 for msp430 is rejected (same missing-entry gap)",
              rejects("x.uf2", b"UF2\n" + bytes(50), "msp430"))

        def suspicious(name, data, board="arduino"):
            try:
                fwmod.validate(name, data, board); return None
            except fwmod.Rejected as e:
                return e.suspicious
        check("a disguised .exe is flagged suspicious",
              suspicious("evil.bin", b"MZ" + bytes([0x90, 0]) + bytes(40), "esp32") is True)
        check("a disguised script is flagged suspicious",
              suspicious("x.hex", b"#!/bin/sh" + bytes([10]) + b"rm -rf /") is True)
        check("a wrong extension is NOT flagged suspicious (honest mistake)",
              suspicious("firmware.txt", good_hex) is False)
        check("a board/extension mismatch is NOT flagged suspicious (honest mistake)",
              suspicious("blink.bin", bytes(200), "arduino") is False)

    with app.app_context():
        from models import Session as S
        u = User.query.filter_by(email="asha@t.edu").first()
        nb = Booking(user_id=u.id, experiment_id=ids["e1"], lab_pi_id=ids["node"],
                     start_time=utcnow() - timedelta(minutes=1),
                     end_time=utcnow() + timedelta(minutes=59), status="active")
        db.session.add(nb); db.session.flush()
        sess = S(booking_id=nb.id, user_id=u.id, experiment_id=ids["e1"],
                 lab_pi_id=ids["node"], starts_at=utcnow(),
                 expires_at=nb.end_time, status="active")
        db.session.add(sess); db.session.commit()
        live_key = sess.session_key; sess_id = sess.id; nb_code = nb.code

    with app.test_client() as c:
        # Firmware upload now requires the session's owner to be signed in.
        login(c, "asha@t.edu", "studentpass1")
        r = c.post("/session/NOTAKEY/firmware",
                   data={"firmware": (_io.BytesIO(good_hex), "blink.hex")},
                   content_type="multipart/form-data")
        check("upload without a live session is refused", r.status_code == 403)

        with mock.patch("services.nodes.requests.post") as post:
            r = c.post(f"/session/{live_key}/firmware",
                       data={"firmware": (_io.BytesIO(b"MZ" + bytes([0x90]) + bytes(60)), "evil.hex"),
                             "board": "arduino"}, content_type="multipart/form-data")
            check("a malicious upload is rejected with 422", r.status_code == 422)
            check("a rejected upload is never forwarded", post.call_count == 0)
            check("the response flags it as suspicious", r.get_json().get("suspicious") is True)

        with app.app_context():
            check("a suspicious rejection is logged at error level, not warning",
                  SystemLog.query.filter_by(category="firmware", level="error")
                                 .filter(SystemLog.message.contains("SUSPICIOUS")).count() >= 1)

        with mock.patch("services.nodes.requests.post") as post:
            post.return_value.status_code = 200
            post.return_value.raise_for_status = lambda: None
            post.return_value.json = lambda: {"status": "Flashing started for arduino"}
            r = c.post(f"/session/{live_key}/firmware",
                       data={"firmware": (_io.BytesIO(good_hex), "blink.hex"), "board": "arduino"},
                       content_type="multipart/form-data")
            check("a valid upload is accepted", r.status_code == 200)
            check("a valid upload is forwarded to the bench's /flash",
                  post.call_count == 1 and "/flash" in post.call_args[0][0])

        login(c, "asha@t.edu", "studentpass1")
        r = c.get(f"/lab/{nb_code}")
        check("the portal lab page renders for the owner", r.status_code == 200)
        check("lab page relays serial/chart/oscilloscope natively, not an iframe",
              b"pillBoard" in r.data and b"<iframe" not in r.data)
        check("lab page can expand to the chart/oscilloscope/camera instrument pages",
              b"/chart?key=" in r.data and b"/oscilloscope?key=" in r.data
              and b"/camera?key=" in r.data)
        check("lab page hosts the firmware upload, routed through the portal's scan endpoint",
              b"flashBtn" in r.data and b"/session/" in r.data and b"/firmware" in r.data)

    with app.app_context():
        app.config["NATIVE_LAB_UI"] = False
    with app.test_client() as c:
        login(c, "asha@t.edu", "studentpass1")
        r = c.get(f"/lab/{nb_code}")
        check("NATIVE_LAB_UI=false falls back to the iframe page",
              r.status_code == 200 and b"labFrame" in r.data)
    with app.app_context():
        app.config["NATIVE_LAB_UI"] = True

    with app.app_context():
        from models import FirmwareUpload
        check("every upload is recorded for audit",
              FirmwareUpload.query.filter_by(session_id=sess_id).count() >= 2)
        check("the good one is marked forwarded",
              FirmwareUpload.query.filter_by(status="forwarded").count() >= 1)
        check("the bad one is marked rejected",
              FirmwareUpload.query.filter_by(status="rejected").count() >= 1)

    print("\nSocket relay authorisation")
    from app import socketio as _sio
    with mock.patch.object(app.extensions["pi_relay"], "forward"):
        sc = _sio.test_client(app, query_string=f"key={live_key}")
        check("an unauthenticated socket is not relayed",
              not sc.is_connected(), "still connected")

        def _cookie_headers(client):
            ck = client.get_cookie("session")
            return {"Cookie": f"session={ck.value}"} if ck else {}

        with app.test_client() as c:
            login(c, "vik@t.edu", "otherpass1")
            sc = _sio.test_client(app, headers=_cookie_headers(c),
                                  query_string=f"key={live_key}")
            check("another student's socket is not relayed",
                  not sc.is_connected(), "still connected")

        with app.test_client() as c:
            login(c, "asha@t.edu", "studentpass1")
            sc = _sio.test_client(app, headers=_cookie_headers(c),
                                  query_string=f"key={live_key}")
            check("the owning student's socket connects",
                  sc.is_connected(), "owner was rejected")
            sc.disconnect()

    print("\nEvery page renders")
    with app.test_client() as c:
        login(c, "admin@t.edu", "adminpass1")
        for path in ["/dashboard", "/my-bookings", "/change-password",
                     "/admin/", "/admin/users", "/admin/courses", "/admin/experiments",
                     "/admin/devices", "/admin/bookings", "/admin/sessions", "/admin/logs"]:
            r = c.get(path)
            check(f"GET {path}", r.status_code == 200, str(r.status_code))
        # An admin hitting / lands on the admin panel rather than the hero page.
        r = c.get("/")
        check("GET / sends an admin to the admin panel",
              r.status_code == 302 and "/admin" in r.headers["Location"])
    with app.test_client() as c:
        for path in ["/login", "/forgot-password", "/admin/login"]:
            check(f"GET {path}", c.get(path).status_code == 200)

    print(f"\n{'=' * 56}")
    print(f"  {len(PASSES)} passed, {len(FAILURES)} failed")
    if FAILURES:
        for f in FAILURES:
            print(f"    FAILED: {f}")
    print(f"{'=' * 56}\n")
    return 1 if FAILURES else 0

    print("\nOne open booking per experiment")
    s1 = (utcnow() + timedelta(days=3)).replace(minute=0, second=0, microsecond=0)
    f1 = s1.strftime("%Y-%m-%dT%H:%M")
    f2 = (s1 + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")

    with app.test_client() as c:
        login(c, "vik@t.edu", "otherpass1")
        r = c.post(f"/experiment/{ids['e1']}/book", data={"slot": f1},
                   follow_redirects=True)
        check("first slot on an experiment is accepted", b"Booking confirmed" in r.data)

        r = c.post(f"/experiment/{ids['e1']}/book", data={"slot": f2},
                   follow_redirects=True)
        check("a second slot on the same experiment is refused",
              b"before booking this experiment again" in r.data)

        r = c.get(f"/experiment/{ids['e1']}/book")
        check("picker offers no bookable slot while one is held",
              b'data-free="1"' not in r.data)
        check("picker explains why", b"already hold a slot" in r.data)

        r = c.get("/dashboard")
        check("dashboard card stops inviting a booking", b"Slot booked" in r.data)

    with app.app_context():
        vid = User.query.filter_by(email="vik@t.edu").first().id
        b = (Booking.query.filter_by(user_id=vid)
             .order_by(Booking.start_time).first())
        b.status = "completed"
        b.completed_at = utcnow()
        db.session.commit()

    with app.test_client() as c:
        login(c, "vik@t.edu", "otherpass1")
        r = c.post(f"/experiment/{ids['e1']}/book", data={"slot": f2},
                   follow_redirects=True)
        check("booking works again once the earlier session is finished",
              b"Booking confirmed" in r.data)


if __name__ == "__main__":
    sys.exit(main())
