"""Per-session relay between the portal and each Lab Pi's own SocketIO app.

Why this exists: browsers should only ever talk to the portal (see
HANDOFF_v6.md / the /lab/<code> route in blueprints/portal.py). Live data
(serial readings, oscilloscope waveforms, flashing progress) used to stream
straight from a node's own SocketIO server to the student's browser inside an
iframe. Now the portal holds one SocketIO *client* connection per active
session to that session's node, and re-emits whatever the node sends into a
Server room named after the session_key - every browser tab for that session
joins that same room.

Ported from remote_lab_admin/lab_pi_relay.py, which runs this same relay in
production - the wire protocol (event names, payload shapes) comes from the
node image both projects target, so this is a straight port, not a rewrite.

One node == one physical experiment == at most one active session, but the
portal fleet-wide may be relaying many of these client connections at once
(one per concurrently active experiment), which is why this is a dict keyed
by session_key rather than a single global connection.
"""
import threading

import socketio as socketio_client_lib

# Events a node pushes to whoever is running its experiment. Kept as a
# passthrough list (not hardcoded per-event handling) so a new event added on
# the node side only needs to be added here, not re-plumbed by hand.
RELAYED_EVENTS = [
    "sensor_data",
    "feedback",
    "serial_status",
    "ports_list",
    "flashing_status",
    "osc_data",
    "osc_settings_sync",
    "board_type_updated",
    "ui_config_updated",
    "debug_event",
]

# Browser-originated events forwarded straight through to the session's node.
FORWARDED_EVENTS = (
    "list_ports", "connect_serial", "disconnect_serial", "send_command",
    "reset_serial", "waveform_config", "update_osc_settings", "osc_auto_level",
    "debug_start", "debug_stop", "debug_load_symbols", "debug_command",
)


class LabPiRelayManager:
    def __init__(self, socketio_server, shared_secret):
        self.socketio = socketio_server
        self.shared_secret = shared_secret
        self._clients = {}  # session_key -> socketio_client_lib.Client
        self._lock = threading.Lock()

    def ensure_connected(self, session_key, node_url):
        """Return a connected client for this session, connecting (or
        reconnecting, if a stale connection died) if needed."""
        with self._lock:
            client = self._clients.get(session_key)
            if client is not None and client.connected:
                return client
            if client is not None:
                self._disconnect_locked(session_key)

            client = socketio_client_lib.Client(reconnection=True, reconnection_attempts=5)
            self._register_relay_handlers(client, session_key)
            try:
                client.connect(
                    node_url,
                    auth={"key": self.shared_secret},
                    transports=["websocket", "polling"],
                    wait_timeout=5,
                )
            except Exception as e:
                print(f"[LabPiRelay] Could not connect to node at {node_url} for session {session_key}: {e}")
                return None

            self._clients[session_key] = client
            return client

    def _register_relay_handlers(self, client, session_key):
        # Straight passthrough - the portal's page speaks the same wire
        # format the node itself already sends to its own /experiment page.
        for event_name in RELAYED_EVENTS:
            def handler(data=None, _event=event_name):
                self.socketio.emit(_event, data, room=session_key)

            client.on(event_name, handler)

    def forward(self, session_key, node_url, event, data):
        """Send a browser-originated command through to the session's node.
        Connects on demand so a page refresh / reconnect doesn't need a
        separate 'please connect' step first."""
        client = self.ensure_connected(session_key, node_url)
        if client is None:
            self.socketio.emit(
                "feedback",
                f'[relay] Lab bench unreachable — "{event}" was not delivered',
                room=session_key,
            )
            return
        try:
            client.emit(event, data or {})
        except Exception as e:
            print(f"[LabPiRelay] emit '{event}' failed for session {session_key}: {e}")

    def disconnect(self, session_key):
        with self._lock:
            self._disconnect_locked(session_key)

    def _disconnect_locked(self, session_key):
        client = self._clients.pop(session_key, None)
        if client is not None:
            # Best-effort: if a debug session was left running (GDB/OpenOCD
            # session on the node), tearing down this relay connection
            # without telling the node to stop leaves the bench believing a
            # session is still active - the next student to connect gets
            # "a debug session is already active on this board" with no way
            # to clear it themselves. A stop when nothing was running is a
            # harmless no-op on the node side.
            try:
                client.emit("debug_stop", {})
            except Exception:
                pass
            try:
                client.disconnect()
            except Exception:
                pass

    def active_session_count(self):
        with self._lock:
            return len(self._clients)
