"""Per-session relay between the portal and each Lab Pi's SocketIO server."""
import threading
import socketio as socketio_client_lib

RELAYED_EVENTS = [
    "sensor_data", "feedback", "serial_status", "ports_list",
    "flashing_status", "osc_data", "osc_settings_sync",
    "board_type_updated", "ui_config_updated", "debug_event",
]


class LabPiRelayManager:
    def __init__(self, socketio_server, master_api_key):
        self.socketio = socketio_server
        self.master_api_key = master_api_key
        self._clients = {}
        self._lock = threading.Lock()

    def ensure_connected(self, session_key, lab_pi_url):
        with self._lock:
            client = self._clients.get(session_key)
            if client is not None and client.connected:
                return client
            if client is not None:
                self._disconnect_locked(session_key)
            client = socketio_client_lib.Client(reconnection=True, reconnection_attempts=5)
            self._register_relay_handlers(client, session_key)
            try:
                client.connect(lab_pi_url, auth={"key": self.master_api_key},
                               transports=["websocket", "polling"], wait_timeout=5)
            except Exception as e:
                print(f"[LabPiRelay] connect to {lab_pi_url} failed: {e}")
                return None
            self._clients[session_key] = client
            return client

    def _register_relay_handlers(self, client, session_key):
        for event_name in RELAYED_EVENTS:
            def handler(data=None, _event=event_name):
                self.socketio.emit(_event, data, room=session_key)
            client.on(event_name, handler)

    def forward(self, session_key, lab_pi_url, event, data):
        client = self.ensure_connected(session_key, lab_pi_url)
        if client is None:
            self.socketio.emit("feedback",
                f'[relay] Lab Pi unreachable - "{event}" not delivered', room=session_key)
            return
        try:
            client.emit(event, data or {})
        except Exception as e:
            print(f"[LabPiRelay] emit '{event}' failed: {e}")

    def disconnect(self, session_key):
        with self._lock:
            self._disconnect_locked(session_key)

    def _disconnect_locked(self, session_key):
        client = self._clients.pop(session_key, None)
        if client is not None:
            try:
                client.disconnect()
            except Exception:
                pass
