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
    SECRET_KEY = os.environ.get("SECRET_KEY", "")
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

    # Origins allowed to open a Socket.IO connection. Defaults to the portal's
    # own URL; set PORTAL_ALLOWED_ORIGINS (comma-separated) if the page is
    # served from more than one hostname.
    PORTAL_ALLOWED_ORIGINS = [o.strip() for o in os.environ.get(
        "PORTAL_ALLOWED_ORIGINS", PORTAL_BASE_URL).split(",") if o.strip()]

    # Shared secret every Lab Pi presents when registering / heart-beating.
    NODE_SHARED_SECRET = os.environ.get("NODE_SHARED_SECRET", "")
    NODE_TIMEOUT = float(os.environ.get("NODE_TIMEOUT", "4"))

    # Ranges the portal may make outbound node requests to, and the ports a
    # bench actually serves. LabPi.ip_address is written from request input
    # and drives every master-to-node URL, so it is constrained here rather
    # than trusted. Defaults to the RFC1918 ranges: narrow these to your
    # actual bench subnet in .env for a tighter allow-list.
    LAB_NODE_CIDRS = [c.strip() for c in os.environ.get(
        "LAB_NODE_CIDRS", "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16").split(",")
        if c.strip()]
    NODE_ALLOWED_PORTS = {int(p) for p in os.environ.get(
        "NODE_ALLOWED_PORTS", "5000,8080,9000,10000").split(",") if p.strip()}

    # Accept Lab Pis that still use the older /api/lab-pi/... paths. This only
    # ever relaxed *which path* a node may use - it never removed the need for
    # a shared secret, and as of the F-01 fix an absent header is refused
    # whatever this is set to. Off by default: a deployment that loses its
    # environment file must not silently widen its own attack surface.
    LEGACY_NODE_COMPAT = _bool("LEGACY_NODE_COMPAT", False)

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


# Values that must never protect a running instance. Anything here - or a
# secret shorter than MIN_SECRET_LENGTH - stops the app at start-up rather
# than letting it serve on a credential that is published in this repository.
MIN_SECRET_LENGTH = 32

_PLACEHOLDER_SECRETS = {
    "",
    "dev-only-change-me",
    "change-this-node-secret",
    "generate-with-python-c-import-secrets-print-secrets-token-hex-32",
    "generate-another-long-random-string",
}


def validate_secrets(config, min_len=MIN_SECRET_LENGTH):
    """Refuse to start on a missing, placeholder or too-short secret.

    Nothing in this project calls load_dotenv(); the values arrive from
    systemd's EnvironmentFile. A process started outside the unit - a manual
    `python app.py`, a container without the env file - therefore used to come
    up happily on the shipped defaults, with no warning. It now fails loudly.
    """
    problems = []
    for name in ("SECRET_KEY", "NODE_SHARED_SECRET"):
        value = config.get(name) or ""
        if value in _PLACEHOLDER_SECRETS:
            problems.append(f"{name} is unset or still the shipped placeholder")
        elif len(value) < min_len:
            problems.append(
                f"{name} is {len(value)} characters; at least {min_len} are required")
    if problems:
        raise RuntimeError(
            "Refusing to start:\n  - " + "\n  - ".join(problems)
            + "\n\nGenerate one with: "
              "python -c 'import secrets; print(secrets.token_hex(32))'")
