"""SocketIO server + browser-facing handlers for the live experiment relay."""
import threading
from flask import request
from flask_socketio import SocketIO, emit, join_room, leave_room
from models import Session
from services.lab_pi_relay import LabPiRelayManager

# cors_allowed_origins is set from configuration in init_socketio(). It used
# to be "*" here, which told the server to accept a Socket.IO handshake from
# any web page - so any site a signed-in student visited could open a relay
# to their bench.
# async_handlers=False keeps each browser's events in arrival order - see app.py.
socketio = SocketIO(async_mode="threading", async_handlers=False)

_relay = None
_sid_session = {}
_sid_lock = threading.Lock()

BROWSER_COMMANDS = [
    "connect_serial", "disconnect_serial", "reset_serial", "send_command",
    "list_ports", "update_osc_settings", "osc_auto_level",
    "debug_start", "debug_stop", "debug_command", "debug_load_symbols",
]


def init_socketio(app, master_api_key):
    global _relay
    socketio.init_app(
        app, cors_allowed_origins=app.config.get("PORTAL_ALLOWED_ORIGINS") or [])
    _relay = LabPiRelayManager(socketio, master_api_key)
    app.extensions["lab_pi_relay"] = _relay
    _register_handlers(app)
    return socketio


def _lab_pi_url_for(session_key):
    s = Session.query.filter_by(session_key=session_key).first()
    if not s or not s.is_live or not s.node:
        return None
    return s.node.base_url


def _session_for_sid():
    with _sid_lock:
        return _sid_session.get(request.sid)


def _register_handlers(app):

    @socketio.on("connect")
    def on_connect(auth=None):
        session_key = request.args.get("key")
        if not session_key:
            emit("feedback", "Server: socket connected (no session_key)")
            return
        with app.app_context():
            lab_pi_url = _lab_pi_url_for(session_key)
        if not lab_pi_url:
            emit("feedback", f"Server: no active bench for session {session_key}")
            return
        join_room(session_key)
        with _sid_lock:
            _sid_session[request.sid] = session_key
        emit("feedback", "Server: socket connected")
        _relay.forward(session_key, lab_pi_url, "list_ports", {})

    @socketio.on("disconnect")
    def on_disconnect():
        session_key = _session_for_sid()
        with _sid_lock:
            _sid_session.pop(request.sid, None)
        if not session_key:
            return
        leave_room(session_key)
        members = socketio.server.manager.get_participants("/", session_key)
        if not any(True for _ in members):
            _relay.disconnect(session_key)

    def _make_forwarder(event):
        def handler(data=None):
            session_key = _session_for_sid()
            if not session_key:
                emit("feedback", "[relay] No active session")
                return
            with app.app_context():
                lab_pi_url = _lab_pi_url_for(session_key)
            if not lab_pi_url:
                emit("feedback", "[relay] No bench assigned")
                return
            _relay.forward(session_key, lab_pi_url, event, data)
        return handler

    for event in BROWSER_COMMANDS:
        socketio.on_event(event, _make_forwarder(event))
