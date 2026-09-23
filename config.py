"""Configuration. Everything sensitive comes from the environment.

Copy .env.example to .env and edit it; nothing here needs changing for a
normal deployment.
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def _bool(name, default=False):
    return os.environ.get(name, str(default)).lower() in ("1", "true", "yes", "on")


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-only-change-me")
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL", f"sqlite:///{BASE_DIR / 'data' / 'portal.db'}"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}

    UPLOAD_FOLDER = BASE_DIR / "uploads"
    SOP_FOLDER = BASE_DIR / "static" / "sop"
    MAX_CONTENT_LENGTH = 32 * 1024 * 1024

    # Public URL of this master server, used in invitation emails.
    PORTAL_BASE_URL = os.environ.get("PORTAL_BASE_URL", "http://localhost:5000")

    # Shared secret every Lab Pi presents when registering / heart-beating.
    NODE_SHARED_SECRET = os.environ.get("NODE_SHARED_SECRET", "change-this-node-secret")
    NODE_TIMEOUT = float(os.environ.get("NODE_TIMEOUT", "4"))

    # Accept Lab Pis that still use the older /api/lab-pi/... paths and send
    # no shared secret. On by default so existing hardware keeps working;
    # turn it off once every node runs install/node_integration.py.
    LEGACY_NODE_COMPAT = _bool("LEGACY_NODE_COMPAT", True)

    # The lab page proxies serial/chart/oscilloscope/camera/audio through the
    # portal instead of sending the browser to the node's own address (see
    # sockets.py, services/pi_relay.py, services/audio_relay.py). Off falls
    # back to the old iframe-to-the-node page in templates/portal/lab.html -
    # an instant rollback if something regresses on real hardware.
    NATIVE_LAB_UI = _bool("NATIVE_LAB_UI", True)

    # Fixed ports the node's own services listen on, alongside its main
    # SocketIO app (node.port, usually 10000) - not configurable per-node
    # because every node image is provisioned the same way.
    CAMERA_PORT = int(os.environ.get("CAMERA_PORT", "8080"))
    AUDIO_PORT = int(os.environ.get("AUDIO_PORT", "9000"))

    # Everything stored is UTC; everything shown or typed is this zone.
    TIMEZONE = os.environ.get("TIMEZONE", "Asia/Kolkata")
    TIMEZONE_LABEL = os.environ.get("TIMEZONE_LABEL", "IST")

    # Booking policy
    SLOT_MINUTES = int(os.environ.get("SLOT_MINUTES", "60"))
    MAX_ADVANCE_DAYS = int(os.environ.get("MAX_ADVANCE_DAYS", "14"))
    MAX_OPEN_BOOKINGS_PER_USER = int(os.environ.get("MAX_OPEN_BOOKINGS_PER_USER", "3"))

    # Mail. If MAIL_SERVER is unset, messages are written to the log instead
    # of being sent, so the portal is fully usable before SMTP is configured.
    MAIL_SERVER = os.environ.get("MAIL_SERVER")
    MAIL_PORT = int(os.environ.get("MAIL_PORT", "587"))
    MAIL_USE_TLS = _bool("MAIL_USE_TLS", True)
    MAIL_USE_SSL = _bool("MAIL_USE_SSL", False)
    MAIL_USERNAME = os.environ.get("MAIL_USERNAME")
    MAIL_PASSWORD = os.environ.get("MAIL_PASSWORD")
    MAIL_DEFAULT_SENDER = os.environ.get("MAIL_DEFAULT_SENDER", "Remote Lab <no-reply@vlab.edu>")

    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = True
    REMEMBER_COOKIE_SECURE = True
    REMEMBER_COOKIE_HTTPONLY = True
    # --- session / security policy ---
    from datetime import timedelta as _td
    PERMANENT_SESSION_LIFETIME = _td(minutes=30)
    IDLE_TIMEOUT_MINUTES = 30
    LOGIN_MAX_ATTEMPTS = 5
    LOGIN_LOCK_MINUTES = 15
    PASSWORD_CHANGE_MIN_MINUTES = 5
    REMEMBER_COOKIE_HTTPONLY = True
