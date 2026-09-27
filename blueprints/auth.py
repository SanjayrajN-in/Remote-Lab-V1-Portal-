"""Sign-in, sign-out, first-login password change, password reset."""
from flask import (Blueprint, current_app, flash, redirect, render_template,
                   request, session as flask_session, url_for)
from flask_login import current_user, login_required, login_user, logout_user

from models import PasswordResetToken, SystemLog, User, db, utcnow
from services import mailer
from app import limiter
from werkzeug.security import check_password_hash, generate_password_hash
import secrets
from urllib.parse import urlparse

bp = Blueprint("auth", __name__)

def _is_safe_next(target):
    """True only for a single-slash absolute path on this host.

    The previous test was nxt.startswith("/"), which accepts
    "//attacker.example" - a protocol-relative URL that browsers resolve to
    an external host while still passing the check.

    Deliberately stricter than resolving the URL and comparing hosts:
    browsers treat a backslash as a path separator, so a form like
    "http:/\\/\\host" reaches an external site even though urljoin()
    resolves it to a local path. Where the parser and the browser disagree,
    the browser wins, so only a plain "/path" form is accepted.
    """
    if not target or not target.startswith("/"):
        return False
    if target.startswith("//") or "\\" in target:
        return False
    parsed = urlparse(target)
    return not parsed.scheme and not parsed.netloc



# One message for every failure mode. /admin/login used to answer 401 for a
# wrong password, 403 for a deactivated account and a different 403 for a
# valid non-admin - so it confirmed working credentials for any account in
# the system, and applied no lockout while doing it.
GENERIC_SIGNIN_ERROR = "That email and password don't match an account."

# Compared against when no user matches, so that a missing account costs
# roughly the same time as a present one.
_DUMMY_HASH = generate_password_hash("not-a-real-password")


def _authenticate(email, password):
    """The single authentication path, shared by both sign-in doors.

    Returns (user, error_message). Lockout, failure counting and the success
    handler live here so the two routes cannot drift apart again.
    """
    user = User.query.filter_by(email=email).first()
    if user is None:
        check_password_hash(_DUMMY_HASH, password)
        return None, GENERIC_SIGNIN_ERROR

    if user.is_locked:
        check_password_hash(_DUMMY_HASH, password)
        SystemLog.write(f"Sign-in attempt on locked account {user.email}",
                        level="warning", category="auth", user_id=user.id)
        db.session.commit()
        return None, GENERIC_SIGNIN_ERROR

    if not user.check_password(password):
        user.register_failed_login(
            max_attempts=current_app.config["LOGIN_MAX_ATTEMPTS"],
            lock_minutes=current_app.config["LOGIN_LOCK_MINUTES"])
        level = "warning" if user.is_locked else "info"
        SystemLog.write(
            f"Failed sign-in for {user.email}"
            + (" - account now locked" if user.is_locked else ""),
            level=level, category="auth", user_id=user.id)
        db.session.commit()
        return None, GENERIC_SIGNIN_ERROR

    if not user.is_active:
        return None, GENERIC_SIGNIN_ERROR

    return user, None


def _establish_session(user, remember, where):
    """Common post-authentication bookkeeping for both doors."""
    login_user(user, remember=remember)
    user.register_successful_login()
    user.session_token = secrets.token_hex(32)
    flask_session["stok"] = user.session_token
    SystemLog.write(f"{user.email} signed in{where}", category="auth", user_id=user.id)
    db.session.commit()



@bp.route("/login", methods=["GET", "POST"])
@limiter.limit("5 per minute", methods=["POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("portal.dashboard"))

    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        # The landing page carries its own sign-in form; a failure there should
        # return to the landing page rather than bounce the person to a
        # different-looking page they did not ask for.
        from_home = request.form.get("from") == "home"
        def _fail(message, code):
            flash(message, "error")
            if from_home:
                return render_template("home.html", email=email), code
            return render_template("login.html", email=email), code

        user, err = _authenticate(email, password)
        if err:
            return _fail(err, 401)

        _establish_session(user, bool(request.form.get("remember")), "")

        if user.must_change_password:
            return redirect(url_for("auth.change_password"))
        nxt = request.args.get("next")
        if _is_safe_next(nxt):
            return redirect(nxt)
        return redirect(url_for("admin.dashboard") if user.is_admin
                        else url_for("portal.dashboard"))

    return render_template("login.html", email="")


@bp.route("/admin/login", methods=["GET", "POST"])
@limiter.limit("5 per minute", methods=["POST"])
def admin_login():
    """A separate door for staff.

    Functionally this authenticates the same way /login does - it shares
    _authenticate() with it - and exists so administrators know which side of
    the system they are entering. It no longer *refuses* a valid student
    account: telling an attacker "those credentials work, but that is not an
    administrator" confirmed working credentials for any account in the
    system. A student who signs in here is simply redirected to their own
    dashboard, after the session exists.
    """
    if current_user.is_authenticated and current_user.is_admin:
        return redirect(url_for("admin.dashboard"))

    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        user, err = _authenticate(email, password)
        if err:
            flash(err, "error")
            return render_template("admin_login.html", email=email), 401

        _establish_session(user, bool(request.form.get("remember")),
                           " to the admin panel")

        if user.must_change_password:
            return redirect(url_for("auth.change_password"))
        # Role is decided once the session exists, and acted on by redirecting.
        # Refusing a valid non-admin here told an attacker they had found
        # working credentials, which is the oracle half of F-06.
        return redirect(url_for("admin.dashboard") if user.is_admin
                        else url_for("portal.dashboard"))

    return render_template("admin_login.html", email="")


@bp.route("/logout", methods=["POST"])
@login_required
def logout():
    """POST only. As a GET it could be triggered by any page that could make a
    browser fetch a URL, which is a nuisance-level forced sign-out."""
    # Rotate the token *before* dropping the login. logout_user() makes
    # current_user anonymous, so doing this afterwards silently no-ops and
    # other sessions for this account stay valid.
    user = current_user._get_current_object()
    user.session_token = secrets.token_hex(32)
    db.session.commit()
    logout_user()
    flask_session.clear()
    from flask import make_response
    resp = make_response(redirect(url_for("portal.home")))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    resp.delete_cookie("session")
    flash("You're signed out.", "success")
    return resp


@bp.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    """Shown on first sign-in after an invitation, and available any time."""
    if request.method == "POST":
        current = request.form.get("current_password") or ""
        new = request.form.get("new_password") or ""
        confirm = request.form.get("confirm_password") or ""

        if not current_user.must_change_password and not current_user.check_password(current):
            flash("Your current password is incorrect.", "error")
        elif len(new) < 8:
            flash("Use at least 8 characters for the new password.", "error")
        elif new != confirm:
            flash("The two new passwords don't match.", "error")
        elif current_user.check_password(new):
            flash("Choose a password you haven't used here before.", "error")
        else:
            current_user.set_password(new)
            current_user.must_change_password = False
            SystemLog.write(f"{current_user.email} changed password",
                            category="auth", user_id=current_user.id)
            db.session.commit()
            flash("Password updated.", "success")
            return redirect(url_for("admin.dashboard") if current_user.is_admin
                            else url_for("portal.dashboard"))

    return render_template("change_password.html",
                           forced=current_user.must_change_password)


@bp.route("/forgot-password", methods=["GET", "POST"])
@limiter.limit("5 per minute", methods=["POST"])
def forgot_password():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        user = User.query.filter_by(email=email).first()
        if user and user.is_active:
            # Any earlier link for this account stops working the moment a new
            # one is issued, so a leaked older link cannot be used later.
            PasswordResetToken.query.filter_by(user_id=user.id, used_at=None).update(
                {"expires_at": utcnow()})
            token = PasswordResetToken(user_id=user.id)
            db.session.add(token)
            db.session.commit()
            mailer.send_password_reset(user, token.token)
        # Same reply either way - the form must not reveal who has an account.
        flash("If that address has an account, a reset link is on its way.", "success")
    return render_template("forgot_password.html")


@bp.route("/reset-password/<token>", methods=["GET", "POST"])
@limiter.limit("5 per minute", methods=["POST"])
def reset_password(token):
    record = PasswordResetToken.query.filter_by(token=token).first()
    if not record or not record.is_valid:
        flash("That reset link has expired. Request a new one.", "error")
        return redirect(url_for("auth.forgot_password"))

    if request.method == "POST":
        new = request.form.get("new_password") or ""
        confirm = request.form.get("confirm_password") or ""
        if len(new) < 8:
            flash("Use at least 8 characters.", "error")
        elif new != confirm:
            flash("The two passwords don't match.", "error")
        else:
            record.user.set_password(new)
            record.user.must_change_password = False
            record.used_at = utcnow()
            SystemLog.write(f"{record.user.email} reset password", category="auth")
            db.session.commit()
            flash("Password reset. You can sign in now.", "success")
            return redirect(url_for("portal.home"))

    return render_template("reset_password.html", token=token)
