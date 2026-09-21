"""Sign-in, sign-out, first-login password change, password reset."""
from flask import (Blueprint, flash, redirect, render_template, request,
                   session as flask_session, url_for)
from flask_login import current_user, login_required, login_user, logout_user

from models import PasswordResetToken, SystemLog, User, db, utcnow
from services import mailer

bp = Blueprint("auth", __name__)


@bp.route("/login", methods=["GET", "POST"])
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
        user = User.query.filter_by(email=email).first()

        def _fail(message, code):
            flash(message, "error")
            if from_home:
                return render_template("home.html", email=email), code
            return render_template("login.html", email=email), code

        if user and user.is_locked:
            return _fail("Too many failed attempts. This account is locked for "
                         "15 minutes. Try again or contact the lab admin.", 403)
        if not user or not user.check_password(password):
            if user:
                user.register_failed_login(max_attempts=5, lock_minutes=15)
                db.session.commit()
            return _fail("That email and password don't match an account.", 401)
        if not user.is_active:
            return _fail("This account has been deactivated. "
                         "Contact the lab administrator.", 403)
#        if not user or not user.check_password(password):
#            return _fail("That email and password don't match an account.", 401)
#        if not user.is_active:
#            return _fail("This account has been deactivated. "
#                         "Contact the lab administrator.", 403)

#        login_user(user, remember=bool(request.form.get("remember")))
#       user.last_login = utcnow()

        login_user(user, remember=bool(request.form.get("remember")))
        user.register_successful_login()
        SystemLog.write(f"{user.email} signed in", category="auth", user_id=user.id)
        db.session.commit()

        if user.must_change_password:
            return redirect(url_for("auth.change_password"))
        nxt = request.args.get("next")
        if nxt and nxt.startswith("/"):
            return redirect(nxt)
        return redirect(url_for("admin.dashboard") if user.is_admin
                        else url_for("portal.dashboard"))

    return render_template("login.html", email="")


@bp.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    """A separate door for staff.

    Functionally this authenticates the same way /login does - the difference
    is that it states plainly which side of the system you are entering, and
    refuses a student account instead of silently dropping them somewhere they
    have no business being. Anyone can still sign in at /login; this exists so
    administrators never wonder which portal they landed on.
    """
    if current_user.is_authenticated and current_user.is_admin:
        return redirect(url_for("admin.dashboard"))

    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        user = User.query.filter_by(email=email).first()

        if not user or not user.check_password(password):
            flash("That email and password don't match an account.", "error")
            return render_template("admin_login.html", email=email), 401
        if not user.is_active:
            flash("This account has been deactivated.", "error")
            return render_template("admin_login.html", email=email), 403
        if not user.is_admin:
            flash("That account isn't an administrator. Use the student sign-in.", "error")
            return render_template("admin_login.html", email=email), 403

        login_user(user, remember=bool(request.form.get("remember")))
        user.last_login = utcnow()
        SystemLog.write(f"{user.email} signed in to the admin panel",
                        category="auth", user_id=user.id)
        db.session.commit()

        if user.must_change_password:
            return redirect(url_for("auth.change_password"))
        return redirect(url_for("admin.dashboard"))

    return render_template("admin_login.html", email="")


@bp.route("/logout")
@login_required
def logout():
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
def forgot_password():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        user = User.query.filter_by(email=email).first()
        if user and user.is_active:
            token = PasswordResetToken(user_id=user.id)
            db.session.add(token)
            db.session.commit()
            mailer.send_password_reset(user, token.token)
        # Same reply either way - the form must not reveal who has an account.
        flash("If that address has an account, a reset link is on its way.", "success")
    return render_template("forgot_password.html")


@bp.route("/reset-password/<token>", methods=["GET", "POST"])
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
