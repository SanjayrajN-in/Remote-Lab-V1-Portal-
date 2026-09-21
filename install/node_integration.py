"""Reference node-side integration for remote_lab_pi.

Drop this into the Lab Pi repo and register the blueprint on its existing
Flask app:

    from node_integration import bp as master_bp, start_background_tasks
    app.register_blueprint(master_bp)
    start_background_tasks(app)

It provides the three endpoints the master calls, plus the two background
loops the node runs (register-and-heartbeat, and session polling). The poller
is what makes the system tolerant of a node behind NAT or a master that was
briefly down: sessions arrive either way.

Environment (in the node's .env):

    MASTER_URL          http://<portal-host>:5000
    NODE_SHARED_SECRET  must equal the master's value
    MASTER_NODE_TOKEN   returned by register; shown in the admin panel
    LAB_PI_ID           e.g. lab-bench-1
    LAB_PI_NAME         e.g. Lab Bench 1
    EXPERIMENT_SLUG     e.g. dc-motor-speed-control
    BOARD               arduino | esp32 | stm32 | msp430 ...
"""
import logging
import os
import threading
import time

import requests
from flask import Blueprint, jsonify, request

log = logging.getLogger(__name__)
bp = Blueprint("master_link", __name__)

MASTER_URL = os.environ.get("MASTER_URL", "").rstrip("/")
SHARED_SECRET = os.environ.get("NODE_SHARED_SECRET", "")
NODE_TOKEN = os.environ.get("MASTER_NODE_TOKEN", "")
NODE_ID = os.environ.get("LAB_PI_ID", "lab-pi-1")
NODE_NAME = os.environ.get("LAB_PI_NAME", NODE_ID)
EXPERIMENT_SLUG = os.environ.get("EXPERIMENT_SLUG", "")
BOARD = os.environ.get("BOARD", "arduino")
PORT = int(os.environ.get("PORT", "5000"))
LOCATION = os.environ.get("LAB_PI_LOCATION", "")

HEARTBEAT_SECONDS = 30
POLL_SECONDS = 10

# Session keys this node currently honours: key -> session dict.
SESSIONS = {}
_lock = threading.Lock()


# --------------------------------------------------------------------------
# Endpoints the master calls
# --------------------------------------------------------------------------

def _secret_ok():
    return request.headers.get("X-Node-Secret", "") == SHARED_SECRET


@bp.get("/api/info")
def info():
    """Identify this node when an admin adds it by IP."""
    if not _secret_ok():
        return jsonify({"error": "bad node secret"}), 401
    return jsonify({
        "node_id": NODE_ID,
        "name": NODE_NAME,
        "board": BOARD,
        "experiment_slug": EXPERIMENT_SLUG,
        "location": LOCATION,
        "port": PORT,
    })


@bp.post("/api/session/start")
def session_start():
    """Accept a session key pushed by the master."""
    if not _secret_ok():
        return jsonify({"error": "bad node secret"}), 401
    data = request.get_json(silent=True) or {}
    key = data.get("session_key")
    if not key:
        return jsonify({"error": "session_key is required"}), 400
    with _lock:
        SESSIONS[key] = data
    log.info("Session %s accepted for %s", key, data.get("user"))
    return jsonify({"ok": True})


@bp.post("/api/session/end")
def session_end():
    """Revoke a key early, on the master's instruction."""
    if not _secret_ok():
        return jsonify({"error": "bad node secret"}), 401
    key = (request.get_json(silent=True) or {}).get("session_key")
    with _lock:
        SESSIONS.pop(key, None)
    on_session_revoked(key)
    log.info("Session %s revoked", key)
    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# Validating a browser that arrives with ?key=
# --------------------------------------------------------------------------

def validate_key(key):
    """Return (ok, info_or_reason).

    Checked locally first so the bench keeps working through a brief master
    outage, then confirmed upstream when the key is unknown here.
    """
    if not key:
        return False, "No session key in the link."

    with _lock:
        local = SESSIONS.get(key)
    if local:
        return True, local

    if not MASTER_URL:
        return False, "This bench is not linked to a portal."
    try:
        r = requests.post(
            f"{MASTER_URL}/api/node/session/validate",
            json={"session_key": key, "node_id": NODE_ID},
            headers={"X-Node-Secret": SHARED_SECRET}, timeout=5,
        )
        data = r.json()
    except Exception as e:
        log.warning("Could not reach the portal to validate %s: %s", key, e)
        return False, "The portal is unreachable. Try again in a moment."

    if not data.get("valid"):
        return False, data.get("reason", "That session key is not valid here.")
    with _lock:
        SESSIONS[key] = data
    return True, data


def report_session_finished(key):
    """Tell the master a session ended on this side."""
    with _lock:
        SESSIONS.pop(key, None)
    if not MASTER_URL:
        return
    try:
        requests.post(f"{MASTER_URL}/api/node/session/end",
                      json={"session_key": key},
                      headers={"X-Node-Secret": SHARED_SECRET}, timeout=5)
    except Exception as e:
        log.warning("Could not report session %s as finished: %s", key, e)


def on_session_revoked(key):
    """Hook: called when a key is withdrawn.

    Wire this to the node's own teardown - close the serial port, stop the
    camera and audio streams, cut the relay, and disconnect any socket still
    attached to that session.
    """
    pass


# --------------------------------------------------------------------------
# Background loops
# --------------------------------------------------------------------------

def _local_ip():
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def register():
    """Announce this node. Idempotent - safe on every boot."""
    global NODE_TOKEN
    if not MASTER_URL:
        log.warning("MASTER_URL is not set; running standalone.")
        return False
    try:
        r = requests.post(
            f"{MASTER_URL}/api/node/register",
            json={"node_id": NODE_ID, "name": NODE_NAME, "ip": _local_ip(),
                  "port": PORT, "board": BOARD, "location": LOCATION,
                  "experiment_slug": EXPERIMENT_SLUG},
            headers={"X-Node-Secret": SHARED_SECRET}, timeout=8,
        )
        r.raise_for_status()
        data = r.json()
        NODE_TOKEN = data.get("api_token") or NODE_TOKEN
        log.info("Registered with the portal as %s", NODE_ID)
        return True
    except Exception as e:
        log.warning("Registration failed: %s", e)
        return False


def _telemetry():
    """CPU, RAM, temperature, battery and uptime. Degrades gracefully."""
    out = {"node_id": NODE_ID, "ip": _local_ip()}
    try:
        import psutil
        out["cpu"] = psutil.cpu_percent(interval=None)
        out["ram"] = psutil.virtual_memory().percent
        out["uptime"] = int(time.time() - psutil.boot_time())
    except Exception:
        pass
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            out["temp"] = int(f.read().strip()) / 1000.0
    except Exception:
        pass
    try:
        # remote_lab_pi already ships dfrobot_ups.py for the battery HAT.
        from dfrobot_ups import read_battery
        battery = read_battery()
        out["battery_percent"] = battery.get("percent")
        out["battery_status"] = battery.get("status")
    except Exception:
        pass
    return out


def _heartbeat_loop():
    while True:
        try:
            if not NODE_TOKEN:
                register()
            requests.post(
                f"{MASTER_URL}/api/node/heartbeat", json=_telemetry(),
                headers={"X-Node-Secret": SHARED_SECRET,
                         "X-Node-Token": NODE_TOKEN}, timeout=8,
            )
        except Exception as e:
            log.debug("Heartbeat failed: %s", e)
        time.sleep(HEARTBEAT_SECONDS)


def _poll_loop():
    """Reconcile local sessions with the master's list.

    The master's answer is authoritative: anything held here but absent there
    has been cancelled or has expired, so it is revoked.
    """
    while True:
        try:
            r = requests.get(
                f"{MASTER_URL}/api/node/{NODE_ID}/sessions",
                headers={"X-Node-Secret": SHARED_SECRET,
                         "X-Node-Token": NODE_TOKEN}, timeout=8,
            )
            r.raise_for_status()
            upstream = {s["session_key"]: s for s in r.json().get("sessions", [])}
            with _lock:
                gone = set(SESSIONS) - set(upstream)
                SESSIONS.clear()
                SESSIONS.update(upstream)
            for key in gone:
                on_session_revoked(key)
                log.info("Session %s withdrawn by the portal", key)
        except Exception as e:
            log.debug("Session poll failed: %s", e)
        time.sleep(POLL_SECONDS)


def start_background_tasks(app=None):
    """Start registration, heartbeat and polling as daemon threads."""
    if not MASTER_URL:
        log.warning("MASTER_URL is not set; background tasks not started.")
        return
    register()
    for target in (_heartbeat_loop, _poll_loop):
        threading.Thread(target=target, daemon=True).start()
    log.info("Portal link running: heartbeat %ss, poll %ss",
             HEARTBEAT_SECONDS, POLL_SECONDS)
