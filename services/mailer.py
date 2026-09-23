"""Outbound email.

If MAIL_SERVER is not configured the message is logged instead of sent, so a
fresh install works end to end before SMTP credentials exist. Sending never
raises into a request handler - a failed invitation must not roll back the
user account it was announcing.
"""
import logging
import uuid

from flask import current_app, render_template

from services import timeutil
from flask_mail import Mail, Message

log = logging.getLogger(__name__)
mail = Mail()


def _send(subject, recipients, html, body):
    """Send a message, or record that it could not be sent.

    The body is never logged. It used to be written out in full when
    MAIL_SERVER was unset, which put temporary passwords and single-use
    password-reset links into the systemd journal in clear text, readable by
    anyone in systemd-journal or adm and preserved in any log backup.
    """
    app = current_app._get_current_object()
    ref = uuid.uuid4().hex[:8]
    if not app.config.get("MAIL_SERVER"):
        log.error("[mail:not-configured] ref=%s to=%s subject=%r NOT SENT "
                  "(configure MAIL_SERVER)", ref, recipients, subject)
        return False
    try:
        msg = Message(subject=subject, recipients=recipients, html=html, body=body)
        mail.send(msg)
        return True
    except Exception:
        log.exception("Failed to send mail to %s", recipients)
        return False


def send_invitation(user, temp_password):
    """Sent when an admin creates a user, individually or by bulk upload."""
    url = current_app.config["PORTAL_BASE_URL"].rstrip("/") + "/login"
    body = (
        f"Hello {user.full_name},\n\n"
        f"An account has been created for you on the Remote Lab portal.\n\n"
        f"  Portal:            {url}\n"
        f"  Email:             {user.email}\n"
        f"  Temporary password: {temp_password}\n\n"
        f"You will be asked to choose a new password the first time you sign in.\n"
        f"Your experiments appear on the dashboard once you sign in.\n"
    )
    html = render_template("email/invitation.html", user=user,
                           temp_password=temp_password, url=url)
    return _send("Your Remote Lab account", [user.email], html, body)


def send_booking_confirmation(booking):
    b = booking
    zone = timeutil.tz_label()
    starts = timeutil.fmt(b.start_time, "%Y-%m-%d %H:%M")
    ends = timeutil.fmt(b.end_time, "%Y-%m-%d %H:%M")
    body = (
        f"Hello {b.user.full_name},\n\n"
        f"Your slot is confirmed.\n\n"
        f"  Booking ID: {b.code}\n"
        f"  Experiment: {b.experiment.name}\n"
        f"  Starts:     {starts} {zone}\n"
        f"  Ends:       {ends} {zone}\n"
        f"  Duration:   {b.duration_min} minutes\n\n"
        f"Open My Bookings at your slot time and choose Go to lab.\n"
    )
    html = render_template("email/booking.html", b=b)
    return _send(f"Slot confirmed - {b.experiment.name}", [b.user.email], html, body)


def send_password_reset(user, token):
    url = f"{current_app.config['PORTAL_BASE_URL'].rstrip('/')}/reset-password/{token}"
    body = (f"Hello {user.full_name},\n\n"
            f"Reset your Remote Lab password here (valid 24 hours):\n{url}\n\n"
            f"If you did not request this, ignore this message.\n")
    html = render_template("email/reset.html", user=user, url=url)
    return _send("Reset your Remote Lab password", [user.email], html, body)
