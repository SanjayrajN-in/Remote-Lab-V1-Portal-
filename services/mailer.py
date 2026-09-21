"""Outbound email.

If MAIL_SERVER is not configured the message is logged instead of sent, so a
fresh install works end to end before SMTP credentials exist. Sending never
raises into a request handler - a failed invitation must not roll back the
user account it was announcing.
"""
import logging

from flask import current_app, render_template

from services import timeutil
from flask_mail import Mail, Message

log = logging.getLogger(__name__)
mail = Mail()


def _send(subject, recipients, html, body):
    app = current_app._get_current_object()
    if not app.config.get("MAIL_SERVER"):
        log.info("[mail:not-configured] to=%s subject=%s\n%s",
                 recipients, subject, body)
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
