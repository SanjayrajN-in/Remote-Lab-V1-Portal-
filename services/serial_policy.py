"""Rules for how a node's serial settings combine - one place for both the
admin UI-settings page (which warns about combinations that can't work) and
the socket relay (which refuses serial actions the settings forbid, instead
of trusting the browser to have greyed the button out).

Works on the ui-config dict a node returns from /api/ui-config or
/api/admin/ui-config: "controls" (the global on/off switches), "defaults",
and "serial_ports" (one profile per MCU the node talks to).
"""

# Browser -> node events that the "serial_connect" switch governs.
# send_command is deliberately not here: required dynamic controls (sliders,
# buttons) write through it too and must keep working while students are
# locked out of connecting/disconnecting ports themselves.
SERIAL_CONNECT_EVENTS = ("connect_serial", "disconnect_serial", "reset_serial")

LOCKED_MESSAGE = "Disabled by lab administrator"


def plotter_ports(cfg):
    """Ports the student's plotter dropdown offers - mirrors the filter on
    #chartPortSelect in templates/portal/lab.html."""
    return [p for p in cfg.get("serial_ports") or []
            if p.get("student_visible") or p.get("plotter_visible")]


def can_ever_connect(port, controls):
    """Whether anything can ever open this port: the node auto-connects a
    fixed-path profile itself, otherwise a student has to see it and be
    allowed to press Connect."""
    if port.get("port") and port.get("auto_connect"):
        return True
    return bool(port.get("student_visible") and controls.get("serial_connect"))


def problems(cfg):
    """Human-readable warnings about settings that contradict each other.
    Empty list when everything is consistent."""
    controls = cfg.get("controls") or {}
    defaults = cfg.get("defaults") or {}
    ports = cfg.get("serial_ports") or []
    out = []

    for p in ports:
        if can_ever_connect(p, controls):
            continue
        label = p.get("label") or p.get("id")
        if not p.get("student_visible") and not p.get("port"):
            why = "it's hidden from students but has no fixed path, so nobody can pick one for it"
        elif not p.get("student_visible"):
            why = "it's hidden from students and Auto-connect is off"
        elif not p.get("port"):
            why = "students pick its path, but Serial connect is off so they can't press Connect"
        else:
            why = "Auto-connect is off and Serial connect is off"
        out.append(f'Port "{label}" will never connect: {why}.')

    primaries = [p for p in ports if p.get("is_primary_target")]
    if ports and not primaries:
        out.append("No port is the primary target, so slider/button commands and "
                   "firmware flashing fall back to whichever port is listed first.")
    elif len(primaries) > 1:
        names = ", ".join(f'"{p.get("label")}"' for p in primaries)
        out.append(f"More than one port is marked primary target ({names}); only the "
                   "first is used. Pick one and save.")

    default_port = defaults.get("serial_plotter_default_port_id") or ""
    if default_port and default_port not in {p.get("id") for p in plotter_ports(cfg)}:
        out.append("The default plotter port is hidden from students, so their plotter "
                   "opens on the first visible port instead.")

    view = defaults.get("main_view")
    if view == "plotter" and not controls.get("serial_plotter") and controls.get("oscilloscope"):
        out.append("Default view is Serial Plotter but the plotter is turned off - "
                   "students start on the Oscilloscope.")
    elif view == "oscilloscope" and not controls.get("oscilloscope") and controls.get("serial_plotter"):
        out.append("Default view is Oscilloscope but the oscilloscope is turned off - "
                   "students start on the Serial Plotter.")

    return out


def blocked_reason(event, data, cfg):
    """Why the node's settings forbid this browser event, or None if allowed."""
    if event not in SERIAL_CONNECT_EVENTS:
        return None
    controls = cfg.get("controls") or {}
    if not controls.get("serial_connect"):
        return LOCKED_MESSAGE
    if event == "disconnect_serial":
        conn_id = (data or {}).get("conn_id")
        port = next((p for p in cfg.get("serial_ports") or [] if p.get("id") == conn_id), None)
        if port is not None and not port.get("allow_disconnect", True):
            return "Disconnect locked by lab administrator"
    return None
