"""Remote Lab portal - master server.

Run:  python app.py            (development)
      gunicorn -w 4 'app:create_app()'   (production; see install/)
"""
import logging
import os
from pathlib import Path

from flask import Flask, flash, redirect, render_template, request, url_for
from flask_login import LoginManager
from flask_socketio import SocketIO

from config import BASE_DIR, Config
from models import User, db, utcnow
from services import timeutil
from services.audio_relay import AudioRelayManager
from services.mailer import mail
from services.pi_relay import LabPiRelayManager

_compat_warned = False

login_manager = LoginManager()
login_manager.login_view = "portal.home"
login_manager.login_message = "Sign in to continue."
login_manager.login_message_category = "info"

# threading, not eventlet: coexists cleanly with audio_relay's own background
# asyncio loop, same combination remote_lab_admin already runs in production.
# NOTE: relay state (which node a session is talking to) lives in this
# process's memory - the portal must run as a single worker (see
# install/remote-lab-portal.service).
socketio = SocketIO(async_mode="threading")


@login_manager.user_loader
def load_user(user_id):
    from flask import session as _s
    user = db.session.get(User, int(user_id))
    if user is None:
        return None
    if user.session_token and _s.get("stok") not in (None, user.session_token):
        return None
    return user


def create_app(config_object=Config):
    app = Flask(__name__)
    app.config.from_object(config_object)

    for folder in (BASE_DIR / "data", app.config["UPLOAD_FOLDER"],
                   app.config["SOP_FOLDER"]):
        Path(folder).mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    db.init_app(app)
    mail.init_app(app)
    login_manager.init_app(app)

    # --- idle session timeout ---------------------------------------------
    # Everyone is logged out after IDLE_TIMEOUT_MINUTES of inactivity, EXCEPT a
    # non-admin who currently holds a live experiment session (so a long
    # experiment isn't cut off). Admins have no exception.
    @app.before_request
    def _enforce_idle_timeout():
        from datetime import timedelta, datetime as _dt
        from flask import session as flask_session, request as _rq
        from flask_login import current_user, logout_user
        from models import Session as LabSession
        if not current_user.is_authenticated:
            return
        if _rq.endpoint in (None, "static") or _rq.path.startswith(
                ("/api/", "/socket.io/", "/session/")):
            return
        limit = app.config.get("IDLE_TIMEOUT_MINUTES", 30)
        now = utcnow()
        last = flask_session.get("last_active")
        idle = False
        if last:
            try:
                idle = (now - _dt.fromisoformat(last)) > timedelta(minutes=limit)
            except (ValueError, TypeError):
                idle = False
        if idle:
            has_live = False
            if not current_user.is_admin:
                has_live = (LabSession.query
                            .filter(LabSession.user_id == current_user.id,
                                    LabSession.status == "active",
                                    LabSession.expires_at > now)
                            .first() is not None)
            if not has_live:
                logout_user()
                flask_session.clear()
                flash("You were signed out after 30 minutes of inactivity.", "info")
                return redirect(url_for("auth.login"))
        flask_session["last_active"] = now.isoformat()
        flask_session.permanent = True
    from services.realtime import init_socketio
    init_socketio(app, app.config.get("NODE_SHARED_SECRET", ""))
    socketio.init_app(app)

    app.extensions["pi_relay"] = LabPiRelayManager(socketio, app.config["NODE_SHARED_SECRET"])
    app.extensions["audio_relay"] = AudioRelayManager()

    import sockets
    sockets.init_app(socketio)

    from blueprints.admin import bp as admin_bp
    from blueprints.auth import bp as auth_bp
    from blueprints.legacy_api import bp as legacy_bp
    from blueprints.firmware import bp as firmware_bp
    from blueprints.node_api import bp as node_bp
    from blueprints.portal import bp as portal_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(portal_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(node_bp)
    app.register_blueprint(firmware_bp)
    # Accepts Lab Pis that still speak the older /api/lab-pi/... paths.
    app.register_blueprint(legacy_bp)

    # Logged once per process rather than once per worker per import, which
    # otherwise repeats this eight times on a four-worker gunicorn start.
    global _compat_warned
    if app.config.get("LEGACY_NODE_COMPAT", True) and not _compat_warned:
        _compat_warned = True
        app.logger.warning(
            "LEGACY_NODE_COMPAT is on: /api/lab-pi/* accepts nodes that do not "
            "send X-Node-Secret. Turn it off once every Pi runs node_integration.py."
        )

    @app.template_filter("dt")
    def _dt(value, fmt="%Y-%m-%d %H:%M"):
        """Stored UTC rendered in the configured local zone (IST)."""
        return timeutil.fmt(value, fmt)

    @app.template_filter("dtz")
    def _dtz(value, fmt="%Y-%m-%d %H:%M"):
        """As above, with the zone named - for anywhere it could be ambiguous."""
        if not value:
            return "-"
        return f"{timeutil.fmt(value, fmt)} {timeutil.tz_label()}"

    @app.template_filter("duration")
    def _duration(seconds):
        if not seconds:
            return "-"
        seconds = int(seconds)
        d, rem = divmod(seconds, 86400)
        h, rem = divmod(rem, 3600)
        m, _ = divmod(rem, 60)
        if d:
            return f"{d}d {h}h"
        return f"{h}h {m}m" if h else f"{m}m"

    @app.context_processor
    def _globals():
        return {"now": utcnow(),
                "local_now": timeutil.local_now(),
                "tz_label": timeutil.tz_label()}

    @app.errorhandler(403)
    def _403(e):
        return render_template("error.html", code=403,
                               title="Not your page",
                               message="That area is limited to lab administrators."), 403

    @app.errorhandler(404)
    def _404(e):
        return render_template("error.html", code=404,
                               title="Nothing here",
                               message="That page doesn't exist, or it isn't part of "
                                       "a course you're enrolled in."), 404

    @app.errorhandler(500)
    def _500(e):
        db.session.rollback()
        return render_template("error.html", code=500,
                               title="Something broke",
                               message="The error was logged. Try again, and tell the "
                                       "lab administrator if it keeps happening."), 500

    @app.after_request
    def _security_headers(resp):
        resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        if request.is_secure or request.headers.get("X-Forwarded-Proto") == "https":
            resp.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        resp.headers.setdefault("Content-Security-Policy",
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://cdn.jsdelivr.net "
            "https://cdn.socket.io https://cdnjs.cloudflare.com; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com "
            "https://cdnjs.cloudflare.com; "
            "font-src 'self' https://fonts.gstatic.com https://cdnjs.cloudflare.com data:; "
            "img-src 'self' data: blob:; "
            "connect-src 'self' ws: wss:; "
            "frame-ancestors 'self'")
        from flask_login import current_user
        if current_user.is_authenticated:
            resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
            resp.headers["Pragma"] = "no-cache"
            resp.headers["Expires"] = "0"
        return resp

    with app.app_context():
        db.create_all()

    return app


app = create_app()

if __name__ == "__main__":
    # socketio.run, not app.run: the lab page's serial/chart/oscilloscope
    # relay needs the SocketIO server actually serving, not just Flask's.
    socketio.run(app, host=os.environ.get("HOST", "0.0.0.0"),
                port=int(os.environ.get("PORT", "5000")),
                debug=os.environ.get("FLASK_DEBUG") == "1",
                allow_unsafe_werkzeug=True)
