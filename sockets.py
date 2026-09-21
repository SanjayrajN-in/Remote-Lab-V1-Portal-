"""SocketIO handlers for the portal-hosted lab page.

The portal has no hardware of its own - see services/pi_relay.py. This module
just resolves which node a connecting browser's session belongs to, joins it
to a room named after the session key, and forwards browser-originated events
to that node over pi_relay. Ported from the 'connect'/'disconnect' handlers
and the relay-forwarding loop in remote_lab_admin/app.py, adapted to look
sessions up in the database (Session/LabPi) instead of admin's in-memory
active_sessions cache - the portal already has this as durable state.
"""
import threading

from flask import current_app, request
from flask_socketio import emit, join_room, leave_room

from models import Session
from services.pi_relay import FORWARDED_EVENTS

# socket.io sid -> session_key, so disconnect/forwarding know which session a
# given browser tab belongs to without trusting client-supplied data again.
_sid_session_map = {}
_sid_lock = threading.Lock()


def _node_for_session(session_key):
    """The live session's node, or None if the key is unknown/expired/has no
    node assigned."""
    if not session_key:
        return None
    sess = Session.query.filter_by(session_key=session_key).first()
    if not sess or not sess.is_live or not sess.node:
        return None
    return sess.node


def _session_for_sid():
    with _sid_lock:
        return _sid_session_map.get(request.sid)


def disconnect_relays(session_key):
    """Tear down this session's relay connections (the SocketIO client to
    the node, and any WebRTC audio legs). Call this wherever a session is
    explicitly ended (cancel, admin revoke, expiry) - not just left to happen
    when the browser tab closes."""
    current_app.extensions["pi_relay"].disconnect(session_key)
    current_app.extensions["audio_relay"].disconnect(session_key)


def init_app(socketio):
    @socketio.on("connect")
    def on_connect():
        # The page passes its session_key as a query param on the socket.io
        # connection itself - sockets don't inherit the page URL's
        # querystring for free.
        session_key = request.args.get("key")
        if not session_key:
            emit("feedback", "Server: socket connected (no session_key - nothing will work until one is set)")
            return

        node = _node_for_session(session_key)
        if not node:
            emit("feedback", f"Server: no active bench found for session {session_key}")
            return

        join_room(session_key)
        with _sid_lock:
            _sid_session_map[request.sid] = session_key

        emit("feedback", "Server: socket connected")
        # Warm the relay connection immediately and ask the node for its
        # current port list, rather than waiting for the page to ask.
        pi_relay = current_app.extensions["pi_relay"]
        pi_relay.forward(session_key, node.base_url, "list_ports", {})

    @socketio.on("disconnect")
    def on_disconnect():
        session_key = _session_for_sid()
        if not session_key:
            return
        with _sid_lock:
            _sid_session_map.pop(request.sid, None)
        leave_room(session_key)
        # Only drop the node connection once no browser tab for this session
        # is listening anymore - a second tab (or a reconnect) shouldn't kill it.
        room_members = socketio.server.manager.get_participants("/", session_key)
        if not any(True for _ in room_members):
            current_app.extensions["pi_relay"].disconnect(session_key)

    def _relay_from_browser(event):
        """Forward `event` (with its data payload) from the connecting
        browser's socket straight through to that session's node."""
        def handler(data=None):
            session_key = _session_for_sid()
            if not session_key:
                emit("feedback", "[relay] No active session on this connection")
                return
            node = _node_for_session(session_key)
            if not node:
                emit("feedback", "[relay] No bench is currently assigned to this session")
                return
            current_app.extensions["pi_relay"].forward(session_key, node.base_url, event, data)
        return handler

    for _relayed_event in FORWARDED_EVENTS:
        socketio.on_event(_relayed_event, _relay_from_browser(_relayed_event))
