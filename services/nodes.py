"""Master -> Lab Pi communication.

The master never touches serial ports, relays or cameras. It only decides
*who* may use *which* rig and *when*, then hands the node a session key. The
node (remote_lab_pi) owns the hardware and validates that key on arrival.

Three calls go out from here:

  probe(ip)              GET  /api/info            - identify a node by IP
                                                     when an admin adds it
  push_session(session)  POST /api/session/start   - grant access
  revoke(session)        POST /api/session/end     - cancel access early

Two calls come in, handled in blueprints/node_api.py: register and heartbeat.
Nodes also poll GET /api/node/<node_id>/sessions, which is what
lab_pi_session_poller.py on the Pi already expects - so a node behind NAT
still works even when the master cannot reach it.
"""
import logging

import requests
from datetime import timezone

from flask import current_app

log = logging.getLogger(__name__)


def _timeout():
    return current_app.config.get("NODE_TIMEOUT", 4)


def _headers(token=None, node_id=None):
    """The deployed Pi checks X-Master-Api-Key (see _verify_master_request in
    remote_lab_pi/app.py). X-Node-Secret is sent alongside for nodes running
    the newer node_integration.py."""
    secret = current_app.config["NODE_SHARED_SECRET"]
    h = {"X-Master-Api-Key": secret, "X-Node-Secret": secret}
    if node_id:
        h["X-Lab-Pi-Id"] = node_id
    if token:
        h["X-Node-Token"] = token
    return h


# Endpoints tried when identifying a node by IP, best first. The deployed
# remote_lab_pi has no /api/info - it exposes /api/ui-config and /ports - so
# asking only for /api/info would 404 on every real Pi.
PROBE_PATHS = ("/api/info", "/api/ui-config", "/ports", "/")


def probe(ip_address, port=5000):
    """Ask an IP to identify itself. Returns (info, error).

    A Lab Pi that answers *anything* is a Lab Pi worth adding; the admin can
    fill in the details afterwards. Refusing to add a node just because it
    does not serve one particular JSON endpoint is worse than adding it with
    fewer fields.
    """
    last_status = None
    for path in PROBE_PATHS:
        url = f"http://{ip_address}:{port}{path}"
        try:
            r = requests.get(url, timeout=_timeout(), headers=_headers(),
                             allow_redirects=True)
        except requests.exceptions.ConnectTimeout:
            return None, (f"No response from {ip_address}:{port}. The address is "
                          f"reachable but nothing answered in time - check the "
                          f"lab-pi service is running.")
        except requests.exceptions.ConnectionError:
            return None, (f"Could not connect to {ip_address}:{port}. Check the IP "
                          f"and port, that the lab-pi service is running, and that "
                          f"nothing is filtering the port between here and there.")
        except Exception as e:  # pragma: no cover
            log.exception("probe failed")
            return None, f"Could not read node details: {e}"

        last_status = r.status_code
        if r.status_code >= 400:
            continue

        info = {}
        try:
            payload = r.json()
            if isinstance(payload, dict):
                info = payload
        except ValueError:
            pass  # HTML is fine - it still proves a Lab Pi is answering

        return {
            "node_id": info.get("node_id") or info.get("lab_pi_id"),
            "name": info.get("name") or info.get("experiment_name"),
            "board": info.get("board") or info.get("board_type"),
            "experiment_slug": info.get("experiment_slug"),
            "location": info.get("location"),
            "reached_via": path,
        }, None

    return None, (f"{ip_address}:{port} is reachable but did not answer any known "
                  f"Lab Pi endpoint (last status {last_status}). Check the port is "
                  f"the lab-pi web service and not something else.")


def push_session(session):
    """Tell the node to accept this session key. Returns (ok, error)."""
    node = session.node
    if not node:
        return False, "This booking has no node assigned."
    # session_end_time is JavaScript milliseconds - remote_lab_pi divides it
    # by 1000 and treats it as a Unix timestamp.
    expires_ms = int(session.expires_at.replace(tzinfo=timezone.utc).timestamp() * 1000)
    payload = {
        "session_key": session.session_key,
        "booking_id": session.booking.code if session.booking else None,
        "user_email": session.user.email,
        "session_end_time": expires_ms,
        # Extra fields, ignored by the deployed Pi, used by newer nodes.
        "user": session.user.full_name,
        "experiment": session.experiment.name,
        "experiment_slug": session.experiment.slug,
        "starts_at": session.starts_at.isoformat(),
        "expires_at": session.expires_at.isoformat(),
    }
    try:
        r = requests.post(f"{node.base_url}/api/lab-pi/session-start", json=payload,
                          timeout=_timeout(),
                          headers=_headers(node.api_token, node.node_id))
        r.raise_for_status()
        return True, None
    except Exception as e:
        log.warning("push_session to %s failed: %s", node.node_id, e)
        # Not fatal: the node's poller will pick the session up on its next
        # cycle. The user is told it may take a few seconds.
        return False, str(e)


def revoke(session):
    node = session.node
    if not node:
        return False, "No node assigned."
    try:
        r = requests.post(f"{node.base_url}/api/lab-pi/session-end",
                          json={"session_key": session.session_key},
                          timeout=_timeout(),
                          headers=_headers(node.api_token, node.node_id))
        r.raise_for_status()
        return True, None
    except Exception as e:
        log.warning("revoke on %s failed: %s", node.node_id, e)
        return False, str(e)


FALLBACK_UI_CONFIG = {
    "controls": {},
    "defaults": {"main_view": "plotter", "dynamic_controls_visible": False,
                 "serial_plotter_allow_port_switch": False,
                 "serial_plotter_default_port_id": ""},
    "required_controls": [], "serial_ports": [], "experiment_name": None,
}


def fetch_ui_config(node):
    if not node:
        return FALLBACK_UI_CONFIG
    try:
        r = requests.get(f"{node.base_url}/api/ui-config",
                         headers=_headers(node.api_token, node.node_id), timeout=5)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning("ui-config fetch from %s failed: %s", node.node_id, e)
        return FALLBACK_UI_CONFIG


def flash(session, data, filename, board):
    """Forward validated firmware to the node's /flash. Returns (ok, message).

    The Pi's /flash expects multipart 'firmware', plus 'board' and an optional
    'port', and it checks that a session is active before accepting - which is
    why this is only ever called for a live session.
    """
    node = session.node
    if not node:
        return False, "This session has no bench assigned."
    try:
        r = requests.post(
            f"{node.base_url}/flash",
            files={"firmware": (filename, data)},
            data={"board": board, "session_key": session.session_key},
            headers=_headers(node.api_token, node.node_id),
            timeout=max(30, current_app.config.get("NODE_TIMEOUT", 4) * 6),
        )
        if r.status_code == 403:
            return False, ("The bench refused it - firmware flashing may be "
                           "disabled for this session, or the session isn't active "
                           "on the bench yet.")
        if r.status_code >= 400:
            # Surface whatever reason the bench actually gave instead of a
            # bare "400 Client Error" - raise_for_status() alone discards the
            # body, which is usually the one thing that says what was wrong
            # (bad board/port, no firmware field, session mismatch, ...).
            try:
                detail = r.json().get("error") or r.json().get("status") or r.text
            except ValueError:
                detail = r.text
            log.warning("flash to %s rejected (%s): %s", node.node_id, r.status_code, detail)
            return False, f"The bench rejected the upload: {detail}" if detail else \
                          f"The bench rejected the upload ({r.status_code})."
        r.raise_for_status()
        try:
            body = r.json()
        except ValueError:
            body = {}
        return True, body.get("status", "Flashing started.")
    except requests.exceptions.Timeout:
        return False, "The bench didn't respond in time while receiving the file."
    except requests.exceptions.ConnectionError:
        return False, f"Could not reach the bench at {node.base_url}."
    except Exception as e:
        log.warning("flash to %s failed: %s", node.node_id, e)
        return False, str(e)


def upload_debug_elf(session, data, filename):
    """Forward the same file that was just flashed to the node's debug ELF
    slot, so a .out/.elf upload feeds both /flash and the debugger's Load
    Symbols step from one upload - see blueprints/firmware.py, which calls
    this right after a successful flash instead of making the student pick
    the same file twice. Returns (ok, message). Best-effort: a board with no
    debug profile, or a node running old firmware without this route, isn't
    an error - it just means there's nothing to debug-load here."""
    node = session.node
    if not node:
        return False, "This session has no bench assigned."
    try:
        r = requests.post(
            f"{node.base_url}/debug/upload-elf",
            files={"elf": (filename, data)},
            headers=_headers(node.api_token, node.node_id),
            timeout=15,
        )
        r.raise_for_status()
        try:
            body = r.json()
        except ValueError:
            body = {}
        return True, body.get("status", "Symbols uploaded.")
    except Exception as e:
        log.info("debug ELF forward to %s skipped/failed: %s", node.node_id, e)
        return False, str(e)


def experiment_url(session):
    """Where the browser is sent when the user chooses Go to lab."""
    return f"{session.node.base_url}/experiment?key={session.session_key}"


# Safe fallback if a node's /api/ui-config can't be reached - controls
# default to empty (Jinja reads e.g. ui_config.controls.board_select as
# Undefined/falsy), so a fetch failure hides/disables controls rather than
# rendering them as if nothing were restricted.
FALLBACK_UI_CONFIG = {
    "controls": {},
    "defaults": {"main_view": "plotter", "dynamic_controls_visible": False,
                 "serial_plotter_allow_port_switch": False,
                 "serial_plotter_default_port_id": ""},
    "required_controls": [],
    "serial_ports": [],
    "experiment_name": None,
}


def ui_config(node):
    """The node's admin-configured UI restrictions - which boards/controls
    are enabled, serial port profiles, required controls - fetched fresh on
    every lab page load so a change on the node's own settings takes effect
    on the very next session, not just the next portal deploy."""
    try:
        r = requests.get(f"{node.base_url}/api/ui-config", timeout=_timeout(),
                         headers=_headers(node.api_token, node.node_id))
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning("Could not fetch ui-config from %s: %s", node.node_id, e)
        return FALLBACK_UI_CONFIG


def camera_url(node):
    """Where the node's ustreamer MJPEG feed lives - a fixed port alongside
    the node's main app, not part of node.base_url."""
    port = current_app.config.get("CAMERA_PORT", 8080)
    return f"http://{node.ip_address}:{port}"


def audio_offer_url(node):
    """Where the node's aiortc audio server's /offer endpoint lives - see
    services/audio_relay.py for the two-hop relay this feeds."""
    port = current_app.config.get("AUDIO_PORT", 9000)
    return f"http://{node.ip_address}:{port}/offer"


def open_camera_stream(node):
    """Start (but don't read) the upstream MJPEG request. Returns a streaming
    requests.Response; the caller pipes it into a Flask Response and is
    responsible for closing it. Raises on failure - the route decides how to
    report that."""
    url = f"{camera_url(node)}/?action=stream&resolution=1920x1080&quality=100"
    return requests.get(url, stream=True, timeout=10)


def factory_reset(session, board, port):
    """Flash the node's own default firmware for `board`. Returns
    (ok, payload_or_message)."""
    node = session.node
    if not node:
        return False, "No bench is assigned to this session."
    try:
        r = requests.post(f"{node.base_url}/factory_reset",
                          json={"board": board or "generic", "port": port or ""},
                          timeout=15)
        r.raise_for_status()
        return True, r.json()
    except Exception as e:
        log.warning("factory_reset on %s failed: %s", node.node_id, e)
        return False, f"Failed to reach the bench: {e}"


# Board types with an actual GDB/OpenOCD debug profile on the node side -
# see the "Debug" sidebar entry in templates/portal/lab.html.
DEBUGGABLE_BOARD_TYPES = {"stm32", "nucleo_f446re", "black_pill", "tiva"}

# Mirrors remote_lab_pi/debug/boards.py's BOARDS keys exactly - the portal
# doesn't import that package, so this list is kept in lockstep by hand.
# Ported from remote_lab_admin/app.py's DEBUG_BOARDS.
DEBUG_BOARDS = [
    ("", "Auto (use this node's Board field)"),
    ("stm32", "STM32 (Cortex-M, ST-Link)"),
    ("nucleo_f446re", "ST Nucleo-F446RE (STM32F4, onboard ST-Link)"),
    ("black_pill", "Black Pill (STM32F4x1, ST-Link/CMSIS-DAP)"),
    ("tiva", "TI Tiva C Series (Cortex-M, onboard ICDI)"),
]

# Same option set as the Board field on the node edit page - used for the
# Oscilloscope's (informational) MCU field, not restricted to debug-capable
# boards. Ported from remote_lab_admin/app.py's BOARD_TYPE_CHOICES.
BOARD_TYPE_CHOICES = [
    ("arduino", "Arduino"), ("attiny", "ATTiny"), ("stm32", "STM32"),
    ("black_pill", "Black Pill"), ("esp32", "ESP32"), ("esp8266", "ESP8266"),
    ("nucleo_f446re", "Nucleo F446RE"), ("tiva", "Tiva"),
    ("tms320f28377s", "TMS320F28377S"), ("msp430", "MSP430"), ("generic", "Generic"),
]


def admin_api(node, method, path, json_body=None):
    """Proxy an admin-config call to a node's Tier-A JSON API (/api/admin/*)
    so the portal's admin console can edit a node's UI/layout config without
    an admin ever visiting that node's own IP. Returns (ok, payload_or_error).

    Ported from remote_lab_admin's _lab_pi_admin_api - same node-side API,
    same auth. As of 2026-08-31 neither registered node (<bench-1>,
    <bench-2>) answers this yet (404), so every call here degrades to
    a clear error until the node firmware catches up - nothing here assumes
    it's there."""
    if not node.ip_address:
        return False, "This node has no IP address on file."
    url = f"{node.base_url}{path}"
    try:
        r = requests.request(method, url, json=json_body,
                             headers=_headers(node.api_token, node.node_id),
                             timeout=_timeout())
        r.raise_for_status()
        return True, r.json()
    except Exception as e:
        return False, f"Could not reach {node.node_id} at {node.ip_address}: {e}"


def toggle_relay(session, state):
    """Physical relay/power toggle for a live session's node. Returns
    (ok, payload_or_message)."""
    node = session.node
    if not node:
        return False, "No bench is assigned to this session."
    try:
        r = requests.post(f"{node.base_url}/toggle_relay",
                          json={"state": state, "session_key": session.session_key},
                          timeout=_timeout())
        r.raise_for_status()
        return True, r.json()
    except Exception as e:
        log.warning("toggle_relay on %s failed: %s", node.node_id, e)
        return False, f"Failed to control relay: {e}"
