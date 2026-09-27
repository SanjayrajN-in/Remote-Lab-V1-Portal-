"""The student-facing half of the portal.

Flow, as specified in api_workflow.docx:

  dashboard  -> only experiments from the user's enrolled courses
             -> card click opens a detail popup with a downloadable SOP PDF
             -> "Book a slot" opens the slot picker
  book       -> 24 hourly slots per day, taken ones disabled
             -> on success, redirect to My bookings
  bookings   -> "Start" before the slot is live, "Go to lab" once it is
  start      -> mint a Session, push the key to the node, redirect to the node
"""
from datetime import date, datetime, time, timedelta, timezone

from flask import (Blueprint, Response, abort, current_app, flash, jsonify,
                   redirect, render_template, request, send_from_directory,
                   url_for)
from flask_login import current_user, login_required

import sockets
from models import (Booking, Experiment, LabPi, Session, SystemLog, db, utcnow)
from sqlalchemy.exc import IntegrityError
from services import mailer, nodes, timeutil

bp = Blueprint("portal", __name__)


# --------------------------------------------------------------------------
# Public
# --------------------------------------------------------------------------

@bp.route("/")
def home():
    if current_user.is_authenticated and current_user.is_admin:
        return redirect(url_for("admin.dashboard"))
    return render_template("home.html")


# --------------------------------------------------------------------------
# Dashboard and experiment details
# --------------------------------------------------------------------------

@bp.route("/dashboard")
@login_required
def dashboard():
    experiments = current_user.visible_experiments()
    now = utcnow()
    upcoming = (Booking.query
                .filter(Booking.user_id == current_user.id,
                        Booking.status.notin_(["cancelled", "completed"]),
                        Booking.end_time > now)
                .order_by(Booking.start_time)
                .all())
    # A bench that cannot heartbeat to this portal still looks offline by
    # telemetry age, so probe each experiment's benches live - reachable-now
    # is what tells a student whether they can actually use it.
    ready = _experiments_with_live_bench(experiments)
    return render_template("portal/dashboard.html",
                           experiments=experiments, upcoming=upcoming,
                           ready=ready)


def _experiments_with_live_bench(experiments, timeout=1.0):
    """Return the set of experiment ids with at least one reachable bench."""
    import socket
    from concurrent.futures import ThreadPoolExecutor

    benches = [(exp.id, node) for exp in experiments for node in exp.nodes]

    def alive(item):
        _, node = item
        try:
            with socket.create_connection((node.ip_address, node.port), timeout):
                return True
        except Exception:
            return False

    if not benches:
        return set()
    with ThreadPoolExecutor(max_workers=min(8, len(benches))) as pool:
        results = list(pool.map(alive, benches))
    return {exp_id for (exp_id, _), ok in zip(benches, results) if ok}


def _visible_or_404(experiment_id):
    """Guard every experiment route: enrolment is the access rule."""
    exp = Experiment.query.get_or_404(experiment_id)
    if not current_user.is_admin:
        allowed = {e.id for e in current_user.visible_experiments()}
        if exp.id not in allowed:
            abort(404)
    return exp


@bp.route("/experiment/<int:experiment_id>/details")
@login_required
def experiment_details(experiment_id):
    """JSON for the detail popup opened from an experiment card."""
    exp = _visible_or_404(experiment_id)
    return jsonify({
        "id": exp.id,
        "name": exp.name,
        "course": exp.course.name if exp.course else None,
        "course_code": exp.course.code if exp.course else None,
        "summary": exp.summary,
        "description": exp.description,
        "objectives": [o for o in (exp.objectives or "").splitlines() if o.strip()],
        "apparatus": [a for a in (exp.apparatus or "").splitlines() if a.strip()],
        "max_duration_min": exp.max_duration_min,
        "has_manual": bool(exp.sop_pdf),
        "manual_url": url_for("portal.experiment_manual", experiment_id=exp.id)
                      if exp.sop_pdf else None,
        "book_url": url_for("portal.book", experiment_id=exp.id),
        "nodes_online": len(exp.online_nodes),
        "nodes_total": len(exp.nodes),
    })


@bp.route("/experiment/<int:experiment_id>/manual")
@login_required
def experiment_manual(experiment_id):
    """The View button in the popup downloads this."""
    exp = _visible_or_404(experiment_id)
    if not exp.sop_pdf:
        abort(404)
    return send_from_directory(
        current_app.config["SOP_FOLDER"], exp.sop_pdf,
        as_attachment=True,
        download_name=f"{exp.slug}-manual.pdf",
    )


# --------------------------------------------------------------------------
# Booking
# --------------------------------------------------------------------------

def _unfinished_booking(experiment_id, user_id):
    """This user's still-open booking on this experiment, if there is one."""
    return (Booking.query
            .filter(Booking.experiment_id == experiment_id,
                    Booking.user_id == user_id,
                    Booking.status.notin_(["cancelled", "completed"]),
                    Booking.end_time > utcnow())
            .order_by(Booking.start_time)
            .first())


def _slots_for(experiment, on_date):
    """24 hourly slots for one IST day, each marked with its availability.

    The grid is a wall clock: the labels, and the value posted back, are IST.
    Everything that touches the database is converted to UTC first, because
    that is what is stored. Mixing the two is what previously made slots look
    five and a half hours in the past.
    """
    slot_len = current_app.config["SLOT_MINUTES"]
    per_hour = max(1, 60 // slot_len)
    now_utc = utcnow()

    local_day_start = datetime.combine(on_date, time.min)
    day_start_utc = timeutil.from_local(local_day_start)
    day_end_utc = timeutil.from_local(local_day_start + timedelta(days=1))

    taken = (Booking.query
             .filter(Booking.experiment_id == experiment.id,
                     Booking.status != "cancelled",
                     Booking.start_time < day_end_utc,
                     Booking.end_time > day_start_utc)
             .all())
    mine = {b.start_time for b in taken if b.user_id == current_user.id}
    busy = {b.start_time for b in taken}

    slots = []
    for hour in range(24):
        for part in range(per_hour):
            local_start = local_day_start + timedelta(hours=hour, minutes=part * slot_len)
            local_end = local_start + timedelta(minutes=slot_len)
            start_utc = timeutil.from_local(local_start)
            end_utc = timeutil.from_local(local_end)

            if start_utc in mine:
                state = "yours"
            elif start_utc in busy:
                state = "taken"
            elif end_utc <= now_utc:
                state = "past"
            else:
                state = "free"

            slots.append({
                "start": local_start,
                "end": local_end,
                "value": local_start.strftime("%Y-%m-%dT%H:%M"),   # IST, as typed
                "label": f"{local_start:%H:%M} - {local_end:%H:%M}",
                "state": state,
            })
    return slots


@bp.route("/experiment/<int:experiment_id>/book", methods=["GET", "POST"])
@login_required
def book(experiment_id):
    exp = _visible_or_404(experiment_id)
    held = _unfinished_booking(exp.id, current_user.id)
    max_days = current_app.config["MAX_ADVANCE_DAYS"]
    today = timeutil.local_today()

    raw_date = request.values.get("date") or today.isoformat()
    try:
        on_date = date.fromisoformat(raw_date)
    except ValueError:
        on_date = today
    on_date = min(max(on_date, today), today + timedelta(days=max_days))

    if request.method == "POST":
        chosen = request.form.get("slot")
        if not chosen:
            flash("Pick a time slot first.", "error")
            return redirect(url_for("portal.book", experiment_id=exp.id,
                                    date=on_date.isoformat()))
        try:
            local_start = datetime.strptime(chosen, "%Y-%m-%dT%H:%M")
        except ValueError:
            flash("That time slot wasn't recognised. Try again.", "error")
            return redirect(url_for("portal.book", experiment_id=exp.id))

        # The form posts IST; storage is UTC.
        local_end = local_start + timedelta(
            minutes=min(current_app.config["SLOT_MINUTES"], exp.max_duration_min))
        start = timeutil.from_local(local_start)
        end = timeutil.from_local(local_end)
        if end <= utcnow():
            flash("That slot has already passed. Choose a later one.", "error")
            return redirect(url_for("portal.book", experiment_id=exp.id,
                                    date=on_date.isoformat()))
        current_app.logger.info("held=%s for exp=%s user=%s",
                                held.code if held else None, exp.id, current_user.id)
        if held:
            held_local = timeutil.to_local(held.start_time)
            flash(f"You already have {exp.name} booked for "
                  f"{held_local:%d %b at %H:%M} ({held.code}). Finish that "
                  f"session, or cancel it, before booking this experiment again.",
                  "error")
            return redirect(url_for("portal.bookings"))

        clash = (Booking.query
                 .filter(Booking.experiment_id == exp.id,
                         Booking.status != "cancelled",
                         Booking.start_time < end, Booking.end_time > start)
                 .first())
        if clash:
            flash("Someone booked that slot a moment ago. Pick another.", "error")
            return redirect(url_for("portal.book", experiment_id=exp.id,
                                    date=on_date.isoformat()))

        overlap_own = (Booking.query
                       .filter(Booking.user_id == current_user.id,
                               Booking.status != "cancelled",
                               Booking.start_time < end, Booking.end_time > start)
                       .first())
        if overlap_own:
            flash("You already have a booking that overlaps this time.", "error")
            return redirect(url_for("portal.bookings"))

        open_count = (Booking.query
                      .filter(Booking.user_id == current_user.id,
                              Booking.status.notin_(["cancelled", "completed"]),
                              Booking.end_time > utcnow())
                      .count())
        limit = current_app.config["MAX_OPEN_BOOKINGS_PER_USER"]
        if open_count >= limit:
            flash(f"You can hold {limit} upcoming bookings at a time. "
                  f"Cancel one to book another.", "error")
            return redirect(url_for("portal.bookings"))

        node = next((n for n in exp.nodes if n.is_active), None)
        booking = Booking(user_id=current_user.id, experiment_id=exp.id,
                          lab_pi_id=node.id if node else None,
                          start_time=start, end_time=end, status="upcoming")
        db.session.add(booking)
        SystemLog.write(f"{current_user.email} booked {exp.name} at {start:%Y-%m-%d %H:%M}",
                        category="booking", user_id=current_user.id)
        try:
            db.session.commit()
        except IntegrityError:
            # F-16: another request took this exact slot between our free-check
            # and our commit. The partial unique index is the real guard; this
            # turns the race loss into a clean message instead of a 500.
            db.session.rollback()
            flash("Someone just booked that slot. Please pick another.", "error")
            return redirect(url_for("portal.book", experiment_id=exp.id,
                                    date=on_date.isoformat()))

        mailer.send_booking_confirmation(booking)
        flash("Booking confirmed. Check your email for the details.", "success")
        return redirect(url_for("portal.bookings"))

    return render_template("portal/book.html", exp=exp,
                           slots=_slots_for(exp, on_date), on_date=on_date,
                           today=today, max_date=today + timedelta(days=max_days))


@bp.route("/my-bookings")
@bp.route("/my_bookings")   # remote_lab_pi links students back to this spelling
@login_required
def bookings():
    all_bookings = (Booking.query
                    .filter_by(user_id=current_user.id)
                    .order_by(Booking.start_time.desc())
                    .all())
    live = [b for b in all_bookings if b.live_status in ("upcoming", "active")]
    live.sort(key=lambda b: b.start_time)
    return render_template(
        "portal/bookings.html",
        upcoming=live,
        completed=[b for b in all_bookings if b.live_status == "completed"],
        cancelled=[b for b in all_bookings if b.live_status == "cancelled"],
    )


@bp.route("/booking/<code>/cancel", methods=["POST"])
@login_required
def cancel_booking(code):
    b = Booking.query.filter_by(code=code, user_id=current_user.id).first_or_404()
    if b.live_status == "completed":
        flash("That session has already finished.", "error")
        return redirect(url_for("portal.bookings"))

    b.status = "cancelled"
    b.cancelled_at = utcnow()
    if b.session and b.session.is_live:
        b.session.status = "revoked"
        b.session.ended_at = utcnow()
        nodes.revoke(b.session)
        sockets.disconnect_relays(b.session.session_key)
    SystemLog.write(f"{current_user.email} cancelled booking {b.code}",
                    category="booking", user_id=current_user.id)
    db.session.commit()
    flash(f"Booking {b.code} cancelled.", "success")
    return redirect(url_for("portal.bookings"))


# --------------------------------------------------------------------------
# Launching a session on the node
# --------------------------------------------------------------------------

@bp.route("/booking/<code>/start")
@login_required
def start_session(code):
    """Mint a session key, hand it to the node, send the browser there."""
    b = Booking.query.filter_by(code=code, user_id=current_user.id).first_or_404()

    if b.live_status == "cancelled":
        flash("That booking was cancelled.", "error")
        return redirect(url_for("portal.bookings"))
    if b.live_status == "completed":
        flash("That session has already finished.", "error")
        return redirect(url_for("portal.bookings"))
    if not b.can_start:
        flash(f"This session opens at {b.start_time:%H:%M} on {b.start_time:%d %b}. "
              f"Come back then.", "error")
        return redirect(url_for("portal.bookings"))

    node = b.node or next((n for n in b.experiment.nodes if n.is_active), None)
    if not node:
        flash("No lab node is assigned to this experiment yet. "
              "Contact the lab administrator.", "error")
        return redirect(url_for("portal.bookings"))
    # Ask the node directly rather than trusting telemetry that may never
    # arrive - a node that cannot heartbeat to this portal is still usable.
    if not node.is_online:
        import socket
        try:
            with socket.create_connection((node.ip_address, node.port), 2):
                node.last_seen = utcnow()
        except Exception:
            flash(f"{node.name} isn't responding at {node.ip_address}:{node.port}. "
                  f"Your slot is still held - try again shortly, or contact the "
                  f"lab administrator.", "error")
            return redirect(url_for("portal.bookings"))

    b.lab_pi_id = node.id

    sess = b.session
    if sess and sess.is_live:
        return redirect(url_for("portal.lab", code=b.code))

    now = utcnow()
    sess = Session(booking_id=b.id, user_id=current_user.id,
                   experiment_id=b.experiment_id, lab_pi_id=node.id,
                   starts_at=now, expires_at=b.end_time, status="active")
    db.session.add(sess)
    b.status = "active"
    db.session.flush()

    ok, err = nodes.push_session(sess)
    sess.pushed_to_node = ok
    SystemLog.write(
        f"Session {sess.session_key} issued to {current_user.email} on {node.node_id}"
        + ("" if ok else f" (direct push failed: {err}; node will poll)"),
        level="info" if ok else "warning",
        category="session", user_id=current_user.id,
    )
    db.session.commit()

    if not ok:
        flash("The node is picking up your session - if the lab page asks you "
              "to wait, refresh in a few seconds.", "info")
    return redirect(url_for("portal.lab", code=b.code))


def _live_session_for(code):
    """Booking + session for `code`, if the current user owns it and it's
    still live. Session is None (booking is still returned, 404 on a code
    that isn't the user's at all) when there's nothing live to show -
    callers flash and redirect to bookings in that case."""
    b = Booking.query.filter_by(code=code, user_id=current_user.id).first_or_404()
    sess = b.session
    return b, (sess if sess and sess.is_live else None)


def _owned_live_session(session_key):
    """A live Session by key, only if it belongs to the current user - for
    the sub-routes (chart/oscilloscope/camera/audio) that a session_key alone
    identifies, opened as their own tab rather than under a booking code."""
    sess = Session.query.filter_by(session_key=session_key).first()
    if not sess or sess.user_id != current_user.id or not sess.is_live:
        return None
    return sess


@bp.route("/lab/<code>")
@login_required
def lab(code):
    """The portal-hosted experiment page.

    The student reaches the live rig through here, not by a raw link to the
    node. This page owns who is allowed in and firmware upload (scanned
    before it ever reaches the bench). Serial control, the live sensor chart,
    oscilloscope and camera/audio all relay through the portal too (see
    sockets.py, services/pi_relay.py, services/audio_relay.py) rather than
    sending the browser to the node's own address - NATIVE_LAB_UI is an
    escape hatch back to the old iframe-to-the-node page if that ever needs
    to be rolled back quickly.

    Renders portal/lab.html - the fully adapted copy of remote_lab_admin's
    experiment page (portal/experiment_ui.html is a newer, still-in-progress
    copy someone else started on; it isn't wired to the portal's routes yet,
    so this points at the tested one until that's finished and merged in).
    """
    b, sess = _live_session_for(code)
    if not sess:
        flash("That session has ended. Book another slot to run the experiment "
              "again.", "error")
        return redirect(url_for("portal.bookings"))

    node = sess.node
    if not current_app.config.get("NATIVE_LAB_UI", True):
        return render_template(
            "portal/lab_iframe.html",
            booking=b, session=sess, node=node, experiment=b.experiment,
            pi_experiment_url=nodes.experiment_url(sess),
            seconds_left=sess.seconds_left,
            upload_url=url_for("firmware.upload", key=sess.session_key),
        )

    duration_min = int((sess.expires_at - sess.starts_at).total_seconds() // 60)
    end_time_ms = int(sess.expires_at.replace(tzinfo=timezone.utc).timestamp() * 1000)
    board_type = (node.board if node else None) or "arduino"

    return render_template(
        "portal/lab.html",
        booking=b, session=sess, node=node, experiment=b.experiment,
        seconds_left=sess.seconds_left,
        session_duration=duration_min,
        session_end_time=end_time_ms,
        board_type=board_type,
        booking_page_url=url_for("portal.bookings"),
        upload_url=url_for("firmware.upload", key=sess.session_key),
        manual_url=(url_for("portal.experiment_manual", experiment_id=b.experiment.id)
                   if b.experiment.sop_pdf else None),
        ui_config=nodes.ui_config(node) if node else nodes.FALLBACK_UI_CONFIG,
        debuggable=board_type in nodes.DEBUGGABLE_BOARD_TYPES,
    )

@bp.route("/chart")
@login_required
def lab_chart():
    """Opened as /chart?key=<session_key> - the lab page's "expand" button
    pops this out in a new tab, matching remote_lab_admin's own /chart route."""
    sess = _owned_live_session(request.args.get("key"))
    if not sess:
        return redirect(url_for("portal.bookings"))
    return render_template("portal/chart.html", session_key=sess.session_key)


@bp.route("/oscilloscope")
@login_required
def lab_oscilloscope():
    sess = _owned_live_session(request.args.get("key"))
    if not sess:
        return redirect(url_for("portal.bookings"))
    return render_template("portal/oscilloscope.html", session_key=sess.session_key)


@bp.route("/camera")
@login_required
def lab_camera():
    sess = _owned_live_session(request.args.get("key"))
    if not sess:
        return redirect(url_for("portal.bookings"))
    return render_template("portal/camera.html", session_key=sess.session_key)


@bp.route("/lab/relay", methods=["POST"])
@login_required
def lab_relay():
    """The physical relay/power toggle button on the lab page. Takes the
    session_key from the POST body (matching the ported lab.html's request
    shape) rather than a booking code in the URL."""
    data = request.get_json(silent=True) or {}
    sess = _owned_live_session(data.get("session_key"))
    if not sess:
        return jsonify({"status": "error", "message": "Invalid or expired session"}), 400
    ok, result = nodes.toggle_relay(sess, data.get("state"))
    if ok:
        return jsonify(result)
    return jsonify({"status": "error", "message": result})


@bp.route("/lab/factory-reset", methods=["POST"])
@login_required
def lab_factory_reset():
    """Flash the bench's default firmware for its board type."""
    data = request.get_json(silent=True) or {}
    sess = _owned_live_session(data.get("session_key"))
    if not sess:
        return jsonify({"error": "Invalid or expired session"}), 400
    ok, result = nodes.factory_reset(sess, data.get("board"), data.get("port"))
    if ok:
        return jsonify(result)
    return jsonify({"error": result}), 502


@bp.route("/lab/<session_key>/camera-stream")
@login_required
def camera_stream(session_key):
    """MJPEG proxy: the browser hits this portal route, the portal streams it
    from the session's node's ustreamer and pipes the bytes straight through.
    The browser's <img> tag never points at a node address."""
    sess = _owned_live_session(session_key)
    if not sess or not sess.node:
        return jsonify({"error": "Not your session, or no bench assigned"}), 403

    try:
        upstream = nodes.open_camera_stream(sess.node)
    except Exception as e:
        return jsonify({"error": f"Camera stream unavailable: {e}"}), 502

    def relay_chunks():
        try:
            for chunk in upstream.iter_content(chunk_size=4096):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    return Response(relay_chunks(),
                    content_type=upstream.headers.get("Content-Type", "multipart/x-mixed-replace"))


@bp.route("/lab/<session_key>/audio-offer", methods=["POST"])
@login_required
def audio_offer(session_key):
    """WebRTC signaling endpoint for the browser's audio peer connection -
    see services/audio_relay.py for the two-peer-connection relay this
    negotiates. The browser's SDP offer never goes anywhere near the node's
    own address."""
    sess = _owned_live_session(session_key)
    if not sess or not sess.node:
        return jsonify({"error": "Not your session, or no bench assigned"}), 403

    data = request.get_json(force=True) or {}
    sdp = data.get("sdp")
    type_ = data.get("type", "offer")
    if not sdp:
        return jsonify({"error": "Missing SDP"}), 400

    audio_relay = current_app.extensions["audio_relay"]
    try:
        answer_sdp, answer_type = audio_relay.handle_offer(
            session_key, nodes.audio_offer_url(sess.node), sdp, type_)
    except Exception as e:
        current_app.logger.warning("[AudioRelay] Offer handling failed for %s: %s", session_key, e)
        return jsonify({"error": str(e)}), 502

    return jsonify({"sdp": answer_sdp, "type": answer_type})


@bp.route("/lab/session-end", methods=["POST"])
@login_required
def lab_session_end():
    """The client-side countdown hitting zero pings this so the node and the
    relay get torn down right away, instead of waiting on the socket's own
    disconnect to notice. Not load-bearing for security - the session is
    independently enforced server-side by its own expires_at - just tidier.
    Deliberately doesn't require is_live: this fires exactly when a session
    has *just* expired, which _owned_live_session's is_live check would
    reject."""
    data = request.get_json(silent=True) or {}
    sess = Session.query.filter_by(session_key=data.get("session_key")).first()
    if not sess or sess.user_id != current_user.id:
        return jsonify({"ok": False}), 404

    if sess.status == "active":
        sess.status = "expired"
        sess.ended_at = utcnow()
        db.session.commit()
    nodes.revoke(sess)
    sockets.disconnect_relays(sess.session_key)
    return jsonify({"ok": True})
