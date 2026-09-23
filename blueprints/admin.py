"""Admin panel.

Covers the tabs shown in api_workflow.docx: Dashboard, Users (with CSV/Excel
bulk upload), Courses, Experiments, Devices (added by IP and identified by
querying the node), Bookings (with a user-detail view), Sessions, and Logs.
"""
import re
from datetime import timedelta
from functools import wraps

from flask import (Blueprint, abort, current_app, flash, redirect,
                   render_template, request, url_for)
from flask_login import current_user, login_required
from werkzeug.utils import secure_filename
import magic

from models import (Booking, Course, Experiment, LabPi, LabPiHeartbeat,
                    Session, SystemLog, User, db, utcnow)
from services.netguard import UnsafeNodeAddress, validate_address, validate_port
import sockets
from services import importer, mailer, nodes, timeutil, validators

bp = Blueprint("admin", __name__, url_prefix="/admin")


def admin_required(f):
    @wraps(f)
    @login_required
    def wrapper(*args, **kwargs):
        if not current_user.is_admin:
            abort(403)
        return f(*args, **kwargs)
    return wrapper


def _slugify(text):
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-") or "experiment"


# --------------------------------------------------------------------------

def _looks_like_pdf(fileobj):
    head = fileobj.read(2048)
    fileobj.seek(0)
    if not head.startswith(b'%PDF-'):
        return False
    try:
        return magic.from_buffer(head, mime=True) == 'application/pdf'
    except Exception:
        return head.startswith(b'%PDF-')


@bp.route("/")
@admin_required
def dashboard():
    now = utcnow()
    nodes_all = LabPi.query.all()
    active_sessions = (Session.query
                       .filter(Session.status == "active", Session.expires_at > now)
                       .order_by(Session.created_at.desc()).all())
    recent = Booking.query.order_by(Booking.created_at.desc()).limit(10).all()
    return render_template(
        "admin/dashboard.html",
        stats={
            "users": User.query.filter_by(role="user").count(),
            "experiments": Experiment.query.filter_by(is_active=True).count(),
            "bookings": Booking.query.count(),
            "nodes_online": sum(1 for n in nodes_all if n.is_online),
            "nodes_total": len(nodes_all),
        },
        active_sessions=active_sessions, recent_bookings=recent, nodes=nodes_all,
    )


# --------------------------------------------------------------------------
# Users
# --------------------------------------------------------------------------

@bp.route("/users")
@admin_required
def users():
    q = (request.args.get("q") or "").strip()
    query = User.query
    if q:
        like = f"%{q}%"
        query = query.filter(db.or_(User.full_name.ilike(like),
                                    User.email.ilike(like),
                                    User.roll_number.ilike(like)))
    return render_template("admin/users.html",
                           users=query.order_by(User.created_at.desc()).all(),
                           q=q, courses=Course.query.order_by(Course.code).all())


@bp.route("/users/new", methods=["POST"])
@admin_required
def create_user():
    ok, name = validators.name(request.form.get("full_name"), "Full name")
    if not ok:
        flash(name, "error"); return redirect(url_for("admin.users"))
    ok, email = validators.email(request.form.get("email"))
    if not ok:
        flash(email, "error"); return redirect(url_for("admin.users"))
    ok, roll = validators.roll_number(request.form.get("roll_number"))
    if not ok:
        flash(roll, "error"); return redirect(url_for("admin.users"))
    if User.query.filter_by(email=email).first():
        flash(f"{email} already has an account.", "error")
        return redirect(url_for("admin.users"))
    pw = importer.temp_password()
    user = User(full_name=name, email=email, roll_number=roll,
                role="admin" if request.form.get("role") == "admin" else "user",
                must_change_password=True)
    user.set_password(pw)
    cid = request.form.get("course")
    if cid and cid.isdigit():
        c = Course.query.get(int(cid))
        if c:
            user.courses = [c]
    db.session.add(user)
    db.session.commit()

    sent = mailer.send_invitation(user, pw)
    SystemLog.write(f"Admin created user {email}", category="admin", user_id=current_user.id)
    db.session.commit()
    # The password is deliberately not shown. A flash message is stored in the
    # signed session cookie and rendered into the page, so putting a working
    # credential there spreads it further than the mailbox it was meant for.
    flash(f"{name} added." + (" Invitation emailed." if sent else
          " Email is not configured, so no invitation was sent - configure "
          "SMTP and use Reset password to issue one."), "success")
    return redirect(url_for("admin.users"))


@bp.route("/users/sample-csv")
@admin_required
def sample_csv():
    """A downloadable example file for the bulk-upload format."""
    from flask import Response
    csv = ("full_name,email,courses\n"
           "Asha Rao,asha@iisc.ac.in,E3-241\n"
           "Vikram N,vikram@iisc.ac.in,E3-241;E9-201\n")
    return Response(csv, mimetype="text/csv",
                    headers={"Content-Disposition":
                             "attachment; filename=iken_users_sample.csv"})


@bp.route("/users/bulk-upload", methods=["POST"])
@admin_required
def bulk_upload():
    """Add users from a CSV or Excel file; each one is emailed an invitation."""
    storage = request.files.get("file")
    if not storage or not storage.filename:
        flash("Choose a CSV or Excel file to upload.", "error")
        return redirect(url_for("admin.users"))

    rows, err = importer.read_rows(storage)
    if err:
        flash(err, "error")
        return redirect(url_for("admin.users"))
    if not rows:
        flash("That file has a header but no data rows.", "error")
        return redirect(url_for("admin.users"))

    clean, problems = importer.validate(rows)
    if not clean:
        flash("Nothing was imported. " + (problems[0] if problems else
              "No valid rows found."), "error")
        for p in problems[1:6]:
            flash(p, "error")
        return redirect(url_for("admin.users"))

    created = importer.create_users(clean)
    SystemLog.write(f"Bulk upload created {len(created)} users",
                    category="admin", user_id=current_user.id)
    db.session.commit()

    sent = sum(1 for user, pw in created if mailer.send_invitation(user, pw))
    flash(f"Added {len(created)} users; {sent} invitation emails sent.", "success")
    if sent < len(created):
        flash("Email is not fully configured, so some invitations were not "
              "sent. Configure SMTP and use Reset password for those accounts.",
              "info")
    for p in problems[:8]:
        flash(p, "info")
    return redirect(url_for("admin.users"))


@bp.route("/users/<int:user_id>")
@admin_required
def user_detail(user_id):
    user = User.query.get_or_404(user_id)
    return render_template(
        "admin/user_detail.html", user=user,
        bookings=Booking.query.filter_by(user_id=user.id)
                 .order_by(Booking.start_time.desc()).all(),
        courses=Course.query.order_by(Course.code).all())


@bp.route("/users/<int:user_id>/edit", methods=["POST"])
@admin_required
def edit_user(user_id):
    user = User.query.get_or_404(user_id)
    user.full_name = (request.form.get("full_name") or user.full_name).strip()
    user.roll_number = (request.form.get("roll_number") or "").strip() or None
    user.role = "admin" if request.form.get("role") == "admin" else "user"
    user.is_active_flag = request.form.get("is_active") == "on"
    cid = request.form.get("course")
    user.courses = [Course.query.get(int(cid))] if cid and cid.isdigit() else []
    SystemLog.write(f"Admin updated {user.email}", category="admin", user_id=current_user.id)
    db.session.commit()
    flash("Changes saved.", "success")
    return redirect(url_for("admin.user_detail", user_id=user.id))


@bp.route("/users/<int:user_id>/reset-password", methods=["POST"])
@admin_required
def reset_user_password(user_id):
    user = User.query.get_or_404(user_id)
    pw = importer.temp_password()
    user.set_password(pw)
    user.must_change_password = True
    db.session.commit()
    sent = mailer.send_invitation(user, pw)
    flash(f"Password reset for {user.email}." +
          ("" if sent else " Email is not configured, so it could not be sent - "
                          "configure SMTP and reset again."), "success")
    return redirect(url_for("admin.user_detail", user_id=user.id))


@bp.route("/users/<int:user_id>/delete", methods=["POST"])
@admin_required
def delete_user(user_id):
    user = User.query.get_or_404(user_id)
    if user.id == current_user.id:
        flash("You can't delete the account you're signed in with.", "error")
        return redirect(url_for("admin.users"))
    email = user.email
    db.session.delete(user)
    SystemLog.write(f"Admin deleted user {email}", level="warning",
                    category="admin", user_id=current_user.id)
    db.session.commit()
    flash(f"{email} removed.", "success")
    return redirect(url_for("admin.users"))


# --------------------------------------------------------------------------
# Courses
# --------------------------------------------------------------------------

@bp.route("/courses", methods=["GET", "POST"])
@admin_required
def courses():
    if request.method == "POST":
        code = (request.form.get("code") or "").strip().upper()
        name = (request.form.get("name") or "").strip()
        if not code or not name:
            flash("A course needs both a code and a name.", "error")
        elif Course.query.filter_by(code=code).first():
            flash(f"Course {code} already exists.", "error")
        else:
            db.session.add(Course(code=code, name=name,
                                  description=request.form.get("description")))
            db.session.commit()
            flash(f"Course {code} added.", "success")
        return redirect(url_for("admin.courses"))
    return render_template("admin/courses.html",
                           courses=Course.query.order_by(Course.code).all())


@bp.route("/courses/<int:course_id>/delete", methods=["POST"])
@admin_required
def delete_course(course_id):
    course = Course.query.get_or_404(course_id)
    if course.experiments:
        flash("Move or delete this course's experiments first.", "error")
        return redirect(url_for("admin.courses"))
    db.session.delete(course)
    db.session.commit()
    flash("Course deleted.", "success")
    return redirect(url_for("admin.courses"))


# --------------------------------------------------------------------------
# Experiments
# --------------------------------------------------------------------------

@bp.route("/experiments", methods=["GET", "POST"])
@admin_required
def experiments():
    if request.method == "POST":
        ok, name = validators.label(request.form.get("name"), "Experiment name")
        if not ok:
            flash(name, "error")
            return redirect(url_for("admin.experiments"))

        slug = _slugify(request.form.get("slug") or name)
        if Experiment.query.filter_by(slug=slug).first():
            slug = f"{slug}-{utcnow():%H%M%S}"

        exp = Experiment(
            name=name, slug=slug,
            summary=request.form.get("summary"),
            description=request.form.get("description"),
            objectives=request.form.get("objectives"),
            apparatus=request.form.get("apparatus"),
            max_duration_min=int(request.form.get("max_duration_min") or 60),
            course_id=request.form.get("course_id") or None,
        )
        pdf = request.files.get("sop_pdf")
        if pdf and pdf.filename:
            if not pdf.filename.lower().endswith(".pdf") or not _looks_like_pdf(pdf.stream):
                flash("The manual must be a valid PDF.", "error")
                return redirect(url_for("admin.experiments"))
            fname = secure_filename(f"{slug}.pdf")
            current_app.config["SOP_FOLDER"].mkdir(parents=True, exist_ok=True)
            pdf.save(current_app.config["SOP_FOLDER"] / fname)
            exp.sop_pdf = fname

        db.session.add(exp)
        db.session.commit()
        flash(f"{name} added.", "success")
        return redirect(url_for("admin.experiments"))

    return render_template("admin/experiments.html",
                           experiments=Experiment.query.order_by(Experiment.id).all(),
                           courses=Course.query.order_by(Course.code).all(),
                           nodes=LabPi.query.order_by(LabPi.node_id).all())


@bp.route("/experiments/<int:experiment_id>/edit", methods=["POST"])
@admin_required
def edit_experiment(experiment_id):
    exp = Experiment.query.get_or_404(experiment_id)
    exp.name = (request.form.get("name") or exp.name).strip()
    exp.summary = request.form.get("summary")
    exp.description = request.form.get("description")
    exp.objectives = request.form.get("objectives")
    exp.apparatus = request.form.get("apparatus")
    exp.max_duration_min = int(request.form.get("max_duration_min") or exp.max_duration_min)
    exp.course_id = request.form.get("course_id") or None
    exp.is_active = request.form.get("is_active") == "on"

    pdf = request.files.get("sop_pdf")
    if pdf and pdf.filename and pdf.filename.lower().endswith(".pdf") and _looks_like_pdf(pdf.stream):
        fname = secure_filename(f"{exp.slug}.pdf")
        current_app.config["SOP_FOLDER"].mkdir(parents=True, exist_ok=True)
        pdf.save(current_app.config["SOP_FOLDER"] / fname)
        exp.sop_pdf = fname

    db.session.commit()
    flash("Changes saved.", "success")
    return redirect(url_for("admin.experiments"))


@bp.route("/experiments/<int:experiment_id>/delete", methods=["POST"])
@admin_required
def delete_experiment(experiment_id):
    exp = Experiment.query.get_or_404(experiment_id)
    if Booking.query.filter_by(experiment_id=exp.id).count():
        exp.is_active = False
        db.session.commit()
        flash("This experiment has bookings, so it was deactivated rather than "
              "deleted. It no longer appears to students.", "info")
        return redirect(url_for("admin.experiments"))
    db.session.delete(exp)
    db.session.commit()
    flash("Experiment deleted.", "success")
    return redirect(url_for("admin.experiments"))


# --------------------------------------------------------------------------
# Devices - a node is added by IP and identifies itself
# --------------------------------------------------------------------------

@bp.route("/devices")
@admin_required
def devices():
    return render_template("admin/devices.html",
                           nodes=LabPi.query.order_by(LabPi.node_id).all(),
                           experiments=Experiment.query.order_by(Experiment.name).all(),
                           board_type_choices=nodes.BOARD_TYPE_CHOICES)


@bp.route("/devices/add", methods=["POST"])
@admin_required
def add_device():
    """Ask the IP for its details rather than making the admin retype them."""
    ip = (request.form.get("ip_address") or "").strip()
    if not ip:
        flash("Enter the node's IP address.", "error")
        return redirect(url_for("admin.devices"))
    # Validated before probe(), because probe() is itself an outbound request
    # to whatever is typed here.
    try:
        ip = validate_address(ip)
        port = validate_port(request.form.get("port") or 5000)
    except UnsafeNodeAddress as e:
        flash(f"That is not a usable bench address: {e}", "error")
        return redirect(url_for("admin.devices"))

    manual_id = (request.form.get("node_id") or "").strip()
    if manual_id and not re.match(r"^[A-Za-z0-9._\-]+$", manual_id):
        flash("Pi username may only contain letters, numbers, . - _", "error")
        return redirect(url_for("admin.devices"))
    info, err = nodes.probe(ip, port)

    if err and not manual_id:
        flash(err + " If the node is offline, enter its Lab Pi ID to add it "
                    "anyway - it will come online by itself once it starts "
                    "sending heartbeats.", "error")
        return redirect(url_for("admin.devices"))

    if err:
        # Unreachable but the admin supplied an ID, so record it and let the
        # node's own heartbeat fill in the rest.
        info = {"node_id": manual_id, "name": manual_id}
        flash(f"{ip} did not answer, so {manual_id} was added from what you "
              f"entered. It will show online once it heartbeats.", "info")

    node_id = info.get("node_id") or manual_id
    if not node_id:
        flash(f"{ip} answered but did not give a Lab Pi ID. Enter the node's "
              f"ID (its LAB_PI_ID) and try again.", "error")
        return redirect(url_for("admin.devices"))
    node = LabPi.query.filter_by(node_id=node_id).first()
    if node:
        node.ip_address, node.port = ip, port
        node.name = info.get("name") or node.name
        node.board = info.get("board") or node.board
        flash(f"{node_id} already existed - its address was updated to {ip}.", "info")
    else:
        node = LabPi(node_id=node_id, name=info.get("name") or node_id,
                     ip_address=ip, port=port, board=info.get("board", "arduino"),
                     location=info.get("location"))
        db.session.add(node)
        flash(f"{node.name} added.", "success")

    slug = info.get("experiment_slug")
    if slug:
        exp = Experiment.query.filter_by(slug=slug).first()
        if exp:
            node.experiment_id = exp.id
        else:
            flash(f"The node reports experiment '{slug}', which doesn't exist "
                  f"here yet. Assign one below.", "info")

    node.last_seen = utcnow()
    SystemLog.write(f"Node {node_id} registered from admin panel at {ip}",
                    category="device", user_id=current_user.id)
    db.session.commit()
    return redirect(url_for("admin.devices"))


@bp.route("/devices/<int:node_id>/edit", methods=["POST"])
@admin_required
def edit_device(node_id):
    node = LabPi.query.get_or_404(node_id)
    node.name = (request.form.get("name") or node.name).strip()
    try:
        node.ip_address = validate_address(
            (request.form.get("ip_address") or node.ip_address).strip())
        node.port = validate_port(request.form.get("port") or node.port)
    except UnsafeNodeAddress as e:
        flash(f"That is not a usable bench address: {e}", "error")
        return redirect(url_for("admin.devices"))
    node.location = request.form.get("location")
    node.experiment_id = request.form.get("experiment_id") or None
    node.is_active = request.form.get("is_active") == "on"
    # Overrides what the node last reported. Needed because the node's own
    # heartbeat/register payload otherwise clobbers this on every check-in -
    # see nodes.DEBUGGABLE_BOARD_TYPES, which is the one thing this field
    # actually gates (the lab page's Debug/GDB tab).
    board = request.form.get("board")
    if board:
        node.board = board
    db.session.commit()
    flash("Node updated.", "success")
    return redirect(url_for("admin.devices"))


@bp.route("/devices/<int:node_id>/refresh", methods=["POST"])
@admin_required
def refresh_device(node_id):
    node = LabPi.query.get_or_404(node_id)
    info, err = nodes.probe(node.ip_address, node.port)
    if err:
        flash(err, "error")
    else:
        node.board = info.get("board") or node.board
        node.last_seen = utcnow()
        db.session.commit()
        flash(f"{node.name} responded.", "success")
    return redirect(url_for("admin.devices"))


@bp.route("/devices/<int:node_id>/delete", methods=["POST"])
@admin_required
def delete_device(node_id):
    node = LabPi.query.get_or_404(node_id)
    name = node.name
    db.session.delete(node)
    db.session.commit()
    flash(f"{name} removed.", "success")
    return redirect(url_for("admin.devices"))


# --------------------------------------------------------------------------
# Node UI/layout settings - edited here, pushed to the node's own
# /api/admin/* API, ported from remote_lab_admin's admin_lab_pi_ui_settings
# and friends. The portal stores none of this itself; every read and write
# round-trips live to the node. As of 2026-08-31 no registered node answers
# /api/admin/* yet (404 on both <bench-1> and .112) - the node firmware
# needs it added before this page does anything but show that error.
# --------------------------------------------------------------------------

def _require_online_node(node_id):
    """Shared guard for every ui-settings route below: 404 if the node
    doesn't exist, flash+None if it isn't online (nothing to reach),
    otherwise the LabPi row."""
    node = LabPi.query.get_or_404(node_id)
    if not node.ip_address or not node.is_online:
        flash(f'"{node.name}" is not online - cannot reach it to edit its UI settings.', "error")
        return None
    return node


def _remap_port_id_by_label(port_id, source_ports, target_ports):
    """A serial-port-profile id only means something on the node that minted
    it - each node generates its own ids independently, and the actual
    device path behind a given label is different hardware per physical
    node. So when copying a config that references a port (a required
    control's portId, the default plotter port) to a different node,
    re-resolve the equivalent port there by matching label, never by
    copying the id as-is. Returns (new_id_or_empty_string, warning_or_None)."""
    if not port_id:
        return "", None
    source_label = next((p["label"] for p in source_ports if p["id"] == port_id), None)
    if source_label is None:
        return "", None
    target_id = next((p["id"] for p in target_ports if p["label"] == source_label), None)
    if target_id is None:
        return "", f'no port labeled "{source_label}" on the target node - cleared, set it manually'
    return target_id, None


def _control_to_rc_form(control):
    """Translate a required-control object as returned by GET
    /api/admin/ui-config (keys: type, label, portId, min, max, ...) into the
    rc_* field names POST/PUT /api/admin/controls expects - the stored
    shape and the write API's input shape differ."""
    form = {
        "rc_type": control.get("type", ""),
        "rc_label": control.get("label", ""),
        "rc_port_id": control.get("portId", ""),
    }
    if control.get("type") == "slider":
        form.update({
            "rc_min": control.get("min", 0),
            "rc_max": control.get("max", 1023),
            "rc_precision": control.get("precision", 0),
            "rc_cmd_format": control.get("cmdFormat", "{value}"),
        })
    elif control.get("type") == "button":
        form.update({"rc_on_cmd": control.get("onCmd", "1"), "rc_off_cmd": control.get("offCmd", "0")})
    elif control.get("type") == "readout":
        form.update({
            "rc_data_key": control.get("dataKey", ""),
            "rc_unit": control.get("unit", ""),
            "rc_decimals": control.get("decimals", ""),
        })
    return form


def _ui_config_body_from_form(form):
    """Build the POST /api/admin/ui-config body from the shared settings
    form's fields. Shared by the plain Save action and by Copy-to (which
    saves the source node's current on-screen edits before copying them
    onward - otherwise Copy would silently ship whatever was last saved to
    disk, not whatever's checked in the browser right now)."""
    all_keys = [k for k in (form.get("all_control_keys") or "").split(",") if k]
    controls = {key: (form.get(f"control_{key}") == "on") for key in all_keys}
    required_prefixes = [
        kw.strip() for kw in (form.get("serial_plotter_required_prefixes") or "").split(",")
        if kw.strip()
    ]
    return {
        "controls": controls,
        "defaults": {
            "main_view": form.get("main_view"),
            "dynamic_controls_visible": form.get("dynamic_controls_visible") == "on",
            "serial_plotter_allow_port_switch": form.get("serial_plotter_allow_port_switch") == "on",
            "serial_plotter_default_port_id": form.get("serial_plotter_default_port_id") or "",
            "serial_plotter_required_prefixes": required_prefixes,
            "debug_board_id": form.get("debug_board_id") or "",
            "debug_port": form.get("debug_port") or "",
            "osc_board_id": form.get("osc_board_id") or "",
            "osc_port": form.get("osc_port") or "",
        },
        "experiment_name": form.get("experiment_name") or "",
    }


# Fallback only - lab_pi_ui_settings() prefers the live control_keys a node
# sends back from GET /api/admin/ui-config (the node is the authority on
# what it supports), and uses this list only for a node too old to send one.
CONTROL_KEYS = [
    ("board_select", "Board selector"),
    ("flash_firmware", "Flash firmware"),
    ("factory_reset", "Factory reset"),
    ("power_supply", "Power supply toggle"),
    ("serial_connect", "Serial connect/disconnect"),
    ("serial_monitor_section", "Serial monitor section"),
    ("serial_plotter", "Serial plotter view"),
    ("oscilloscope", "Oscilloscope view"),
    ("student_controls_addition", "Students can add their own dynamic controls"),
]


@bp.route("/lab-pi/<int:node_id>/ui-settings", methods=["GET", "POST"])
@admin_required
def lab_pi_ui_settings(node_id):
    node = _require_online_node(node_id)
    if node is None:
        return redirect(url_for("admin.devices"))

    if request.method == "POST":
        body = _ui_config_body_from_form(request.form)
        ok, result = nodes.admin_api(node, "POST", "/api/admin/ui-config", body)
        flash("UI settings saved and pushed to the node." if ok else result,
              "success" if ok else "error")
        return redirect(url_for("admin.lab_pi_ui_settings", node_id=node_id))

    ok, cfg = nodes.admin_api(node, "GET", "/api/admin/ui-config")
    if not ok:
        flash(cfg, "error")
        return redirect(url_for("admin.devices"))

    # Other nodes serving the same experiment - offered as "copy to" targets
    # further down the page so settings don't have to be retyped by hand on
    # every physical board handling this experiment.
    sibling_nodes = []
    if node.experiment_id:
        sibling_nodes = LabPi.query.filter(
            LabPi.experiment_id == node.experiment_id, LabPi.id != node.id
        ).all()

    # The node itself is the authority on which controls it supports and
    # what to call them - not a static guess kept in sync by hand. Falls
    # back to CONTROL_KEYS only if an older node doesn't send this yet.
    live_keys = [(c["key"], c["label"]) for c in cfg.get("control_keys", [])]
    return render_template("admin/lab_pi_ui_settings.html", device=node, cfg=cfg,
                           control_keys=live_keys or CONTROL_KEYS,
                           available_ports=cfg.get("available_ports", []),
                           osc_available_ports=cfg.get("osc_available_ports", []),
                           debug_boards=nodes.DEBUG_BOARDS,
                           board_type_choices=nodes.BOARD_TYPE_CHOICES,
                           sibling_nodes=sibling_nodes)


@bp.route("/lab-pi/<int:node_id>/ui-settings/controls/add", methods=["POST"])
@admin_required
def lab_pi_ui_control_add(node_id):
    node = _require_online_node(node_id)
    if node is not None:
        ok, result = nodes.admin_api(node, "POST", "/api/admin/controls", dict(request.form))
        flash("Control added." if ok else result, "success" if ok else "error")
    return redirect(url_for("admin.lab_pi_ui_settings", node_id=node_id))


@bp.route("/lab-pi/<int:node_id>/ui-settings/controls/<control_id>/edit", methods=["POST"])
@admin_required
def lab_pi_ui_control_edit(node_id, control_id):
    node = _require_online_node(node_id)
    if node is not None:
        ok, result = nodes.admin_api(node, "PUT", f"/api/admin/controls/{control_id}", dict(request.form))
        flash("Control updated." if ok else result, "success" if ok else "error")
    return redirect(url_for("admin.lab_pi_ui_settings", node_id=node_id))


@bp.route("/lab-pi/<int:node_id>/ui-settings/controls/<control_id>/delete", methods=["POST"])
@admin_required
def lab_pi_ui_control_delete(node_id, control_id):
    node = _require_online_node(node_id)
    if node is not None:
        ok, result = nodes.admin_api(node, "DELETE", f"/api/admin/controls/{control_id}")
        flash("Control removed." if ok else result, "success" if ok else "error")
    return redirect(url_for("admin.lab_pi_ui_settings", node_id=node_id))


@bp.route("/lab-pi/<int:node_id>/ui-settings/ports/add", methods=["POST"])
@admin_required
def lab_pi_ui_port_add(node_id):
    node = _require_online_node(node_id)
    if node is not None:
        ok, result = nodes.admin_api(node, "POST", "/api/admin/ports", dict(request.form))
        flash("Serial port added." if ok else result, "success" if ok else "error")
    return redirect(url_for("admin.lab_pi_ui_settings", node_id=node_id))


@bp.route("/lab-pi/<int:node_id>/ui-settings/ports/<port_id>/edit", methods=["POST"])
@admin_required
def lab_pi_ui_port_edit(node_id, port_id):
    node = _require_online_node(node_id)
    if node is not None:
        ok, result = nodes.admin_api(node, "PUT", f"/api/admin/ports/{port_id}", dict(request.form))
        flash("Serial port updated." if ok else result, "success" if ok else "error")
    return redirect(url_for("admin.lab_pi_ui_settings", node_id=node_id))


@bp.route("/lab-pi/<int:node_id>/ui-settings/ports/<port_id>/delete", methods=["POST"])
@admin_required
def lab_pi_ui_port_delete(node_id, port_id):
    node = _require_online_node(node_id)
    if node is not None:
        ok, result = nodes.admin_api(node, "DELETE", f"/api/admin/ports/{port_id}")
        flash("Serial port removed." if ok else result, "success" if ok else "error")
    return redirect(url_for("admin.lab_pi_ui_settings", node_id=node_id))


@bp.route("/lab-pi/<int:node_id>/ui-settings/copy-to", methods=["POST"])
@admin_required
def lab_pi_ui_copy_to(node_id):
    """Copy this node's control toggles, defaults, experiment name, and
    required dynamic controls onto another node serving the same
    experiment - for when several physical boards run the same experiment
    and shouldn't need retyping the same settings on each one by hand.
    Serial port profiles are never touched here: device paths (and the ids
    that reference them) are specific to each physical node's attached
    hardware, so copying them verbatim would silently point at the wrong
    port on the target (see _remap_port_id_by_label)."""
    source = _require_online_node(node_id)
    if source is None:
        return redirect(url_for("admin.lab_pi_ui_settings", node_id=node_id))

    target_id = request.form.get("target_lab_pi_id", type=int)
    target = LabPi.query.get(target_id) if target_id else None
    if target is None or target.experiment_id != source.experiment_id:
        flash("Pick a valid node that serves the same experiment.", "error")
        return redirect(url_for("admin.lab_pi_ui_settings", node_id=node_id))
    if not target.ip_address or not target.is_online:
        flash(f'"{target.name}" is not online - cannot copy to it right now.', "error")
        return redirect(url_for("admin.lab_pi_ui_settings", node_id=node_id))

    # The Copy button lives inside the same form as every settings checkbox
    # (form="mainSettingsForm" in the template), so this request carries
    # whatever's currently on screen - save it to the source node first,
    # then copy that just-saved state onward. Without this, Copy would read
    # back whatever was last actually saved to disk, silently ignoring any
    # edit made since the last "Save settings" click.
    ok, result = nodes.admin_api(source, "POST", "/api/admin/ui-config", _ui_config_body_from_form(request.form))
    if not ok:
        flash(f'Could not save current settings to "{source.name}" before copying: {result}', "error")
        return redirect(url_for("admin.lab_pi_ui_settings", node_id=node_id))

    ok, source_cfg = nodes.admin_api(source, "GET", "/api/admin/ui-config")
    if not ok:
        flash(source_cfg, "error")
        return redirect(url_for("admin.lab_pi_ui_settings", node_id=node_id))
    ok, target_cfg = nodes.admin_api(target, "GET", "/api/admin/ui-config")
    if not ok:
        flash(target_cfg, "error")
        return redirect(url_for("admin.lab_pi_ui_settings", node_id=node_id))

    source_ports = source_cfg.get("serial_ports", [])
    target_ports = target_cfg.get("serial_ports", [])
    warnings = []

    default_port_id, warn = _remap_port_id_by_label(
        source_cfg.get("defaults", {}).get("serial_plotter_default_port_id"), source_ports, target_ports)
    if warn:
        warnings.append(f"Default plotter port: {warn}")

    # debug_port/osc_port are /dev/serial/by-id/... paths, exactly as
    # per-node-specific as serial_ports' own device paths above - never
    # carry them to another node's hardware. debug_board_id/osc_board_id
    # are plain MCU identifiers (not paths), safe to copy as-is.
    body = {
        "controls": source_cfg.get("controls", {}),
        "defaults": {
            **source_cfg.get("defaults", {}),
            "serial_plotter_default_port_id": default_port_id,
            "debug_port": "",
            "osc_port": "",
        },
        "experiment_name": source_cfg.get("experiment_name", ""),
    }
    if source_cfg.get("defaults", {}).get("debug_port") or source_cfg.get("defaults", {}).get("osc_port"):
        warnings.append("Debug probe / Oscilloscope port overrides are per-node device paths "
                        "and were not copied - set them on the target node if needed.")
    ok, result = nodes.admin_api(target, "POST", "/api/admin/ui-config", body)
    if not ok:
        flash(f"Copy failed: {result}", "error")
        return redirect(url_for("admin.lab_pi_ui_settings", node_id=node_id))

    # Required dynamic controls: update ones that already exist on the target
    # (matched by label+type, since ids are per-node - see
    # _remap_port_id_by_label), add ones that don't. Never deletes a control
    # the target has that the source doesn't, so a copy can't destroy
    # target-only setup.
    added, updated = 0, 0
    target_existing = target_cfg.get("required_controls", [])
    for control in source_cfg.get("required_controls", []):
        portId, warn = _remap_port_id_by_label(control.get("portId"), source_ports, target_ports)
        if warn:
            warnings.append(f'Required control "{control.get("label")}": {warn}')
        payload = _control_to_rc_form({**control, "portId": portId})
        existing = next(
            (c for c in target_existing
             if c.get("label") == control.get("label") and c.get("type") == control.get("type")),
            None
        )
        if existing:
            ok, result = nodes.admin_api(target, "PUT", f'/api/admin/controls/{existing["id"]}', payload)
            if ok:
                updated += 1
            else:
                warnings.append(f'Required control "{control.get("label")}": {result}')
        else:
            ok, result = nodes.admin_api(target, "POST", "/api/admin/controls", payload)
            if ok:
                added += 1
            else:
                warnings.append(f'Required control "{control.get("label")}": {result}')

    flash(
        f'Copied to "{target.name}": controls, defaults, and experiment name applied; '
        f"{added} required control(s) added, {updated} updated. Serial port profiles were "
        f"not touched - those stay per-node since each board's device paths differ.",
        "success",
    )
    for w in warnings:
        flash(w, "warning")
    return redirect(url_for("admin.lab_pi_ui_settings", node_id=node_id))


# --------------------------------------------------------------------------
# Bookings and sessions
# --------------------------------------------------------------------------

@bp.route("/bookings")
@admin_required
def bookings():
    query = Booking.query
    status = request.args.get("status") or ""
    user_id = request.args.get("user_id") or ""
    exp_id = request.args.get("experiment_id") or ""
    on_date = request.args.get("date") or ""

    if user_id:
        query = query.filter(Booking.user_id == int(user_id))
    if exp_id:
        query = query.filter(Booking.experiment_id == int(exp_id))
    if on_date:
        try:
            from datetime import date as _d, datetime as _dt
            d = _d.fromisoformat(on_date)
            # The admin picks an IST date; bookings are stored in UTC.
            start = timeutil.from_local(_dt.combine(d, _dt.min.time()))
            end = timeutil.from_local(_dt.combine(d, _dt.min.time()) + timedelta(days=1))
            query = query.filter(Booking.start_time >= start, Booking.start_time < end)
        except ValueError:
            flash("That date wasn't recognised.", "error")

    rows = query.order_by(Booking.start_time.desc()).all()
    if status:
        rows = [b for b in rows if b.live_status == status]

    return render_template("admin/bookings.html", bookings=rows,
                           users=User.query.order_by(User.full_name).all(),
                           experiments=Experiment.query.order_by(Experiment.name).all(),
                           f={"status": status, "user_id": user_id,
                              "experiment_id": exp_id, "date": on_date})


@bp.route("/bookings/<code>")
@admin_required
def booking_detail(code):
    """The View button - full booking record plus who made it."""
    return render_template("admin/booking_detail.html",
                           b=Booking.query.filter_by(code=code).first_or_404())


@bp.route("/bookings/<code>/delete", methods=["POST"])
@admin_required
def delete_booking(code):
    b = Booking.query.filter_by(code=code).first_or_404()
    if b.session and b.session.is_live:
        nodes.revoke(b.session)
        sockets.disconnect_relays(b.session.session_key)
    db.session.delete(b)
    SystemLog.write(f"Admin deleted booking {code}", level="warning",
                    category="admin", user_id=current_user.id)
    db.session.commit()
    flash(f"Booking {code} deleted.", "success")
    return redirect(url_for("admin.bookings"))


@bp.route("/sessions")
@admin_required
def sessions():
    return render_template(
        "admin/sessions.html",
        sessions=Session.query.order_by(Session.created_at.desc()).limit(200).all())


@bp.route("/sessions/<int:session_id>/end", methods=["POST"])
@admin_required
def end_session(session_id):
    s = Session.query.get_or_404(session_id)
    s.status = "revoked"
    s.ended_at = utcnow()
    if s.booking:
        s.booking.status = "completed"
        s.booking.completed_at = utcnow()
    ok, err = nodes.revoke(s)
    sockets.disconnect_relays(s.session_key)
    SystemLog.write(f"Admin ended session {s.session_key}", level="warning",
                    category="session", user_id=current_user.id)
    db.session.commit()
    flash(f"Session {s.session_key} ended." if ok else
          f"Session marked ended, but the node didn't confirm: {err}",
          "success" if ok else "info")
    return redirect(url_for("admin.sessions"))


@bp.route("/logs")
@admin_required
def logs():
    level = request.args.get("level") or ""
    q = SystemLog.query
    if level:
        q = q.filter(SystemLog.level == level)
    return render_template("admin/logs.html", level=level,
                           logs=q.order_by(SystemLog.created_at.desc()).limit(300).all())
