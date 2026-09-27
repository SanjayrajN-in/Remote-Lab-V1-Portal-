"""Database models for the Remote Lab portal.

Mirrors the schema of remote_lab_admin (User, Experiment, Booking, Session,
Device/LabPi, DeviceMetric/LabPiHeartbeat, SystemLog, OTAUpdate,
PasswordResetToken, Department) and adds Course + Enrollment, which are what
make "a user only sees experiments from courses they are enrolled in"
expressible in the schema rather than in ad-hoc filtering.
"""
import secrets
from datetime import datetime, timedelta, timezone

from flask_login import UserMixin
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash

db = SQLAlchemy()


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _code(n=8):
    """Human-readable random code, no ambiguous characters."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(n))


# --------------------------------------------------------------------------
# People
# --------------------------------------------------------------------------

enrollments = db.Table(
    "enrollments",
    db.Column("user_id", db.Integer, db.ForeignKey("users.id"), primary_key=True),
    db.Column("course_id", db.Integer, db.ForeignKey("courses.id"), primary_key=True),
    db.Column("enrolled_at", db.DateTime, default=utcnow),
)


class Department(db.Model):
    __tablename__ = "departments"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), unique=True, nullable=False)
    code = db.Column(db.String(20), unique=True)
    users = db.relationship("User", back_populates="department")

    def __repr__(self):
        return f"<Department {self.code}>"


class User(UserMixin, db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    full_name = db.Column(db.String(160), nullable=False)
    email = db.Column(db.String(200), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(300))
    role = db.Column(db.String(20), default="user", nullable=False)  # user | admin
    is_active_flag = db.Column("is_active", db.Boolean, default=True, nullable=False)
    must_change_password = db.Column(db.Boolean, default=False, nullable=False)
    roll_number = db.Column(db.String(50))
    department_id = db.Column(db.Integer, db.ForeignKey("departments.id"))
    created_at = db.Column(db.DateTime, default=utcnow)
    last_login = db.Column(db.DateTime)
    # --- security: brute-force lockout + password-change throttle ---
    failed_logins = db.Column(db.Integer, default=0, nullable=False)
    locked_until = db.Column(db.DateTime)
    password_changed_at = db.Column(db.DateTime)
    session_token = db.Column(db.String(64))

    department = db.relationship("Department", back_populates="users")
    courses = db.relationship("Course", secondary=enrollments, back_populates="students")
    bookings = db.relationship("Booking", back_populates="user", cascade="all, delete-orphan")

    # Flask-Login checks `is_active`; we store the column under a different
    # attribute name so this property can stay the single source of truth.
    @property
    def is_active(self):
        return bool(self.is_active_flag)

    @property
    def is_admin(self):
        return self.role == "admin"

    def set_password(self, raw):
        self.password_hash = generate_password_hash(raw)

    def check_password(self, raw):
        return bool(self.password_hash) and check_password_hash(self.password_hash, raw)

    @property
    def is_locked(self):
        return bool(self.locked_until and self.locked_until > utcnow())

    def register_failed_login(self, max_attempts=5, lock_minutes=15):
        from datetime import timedelta
        self.failed_logins = (self.failed_logins or 0) + 1
        if self.failed_logins >= max_attempts:
            self.locked_until = utcnow() + timedelta(minutes=lock_minutes)
            self.failed_logins = 0

    def register_successful_login(self):
        self.failed_logins = 0
        self.locked_until = None
        self.last_login = utcnow()

    def password_change_allowed(self, min_minutes=5):
        from datetime import timedelta
        if not self.password_changed_at:
            return True
        return utcnow() >= self.password_changed_at + timedelta(minutes=min_minutes)

    def visible_experiments(self):
        """Experiments belonging to the courses this user is enrolled in.

        Admins see everything. Everyone else sees only their courses'
        experiments - never the full catalogue.
        """
        if self.is_admin:
            return Experiment.query.filter_by(is_active=True).order_by(Experiment.id).all()
        course_ids = [c.id for c in self.courses]
        if not course_ids:
            return []
        return (
            Experiment.query.filter(
                Experiment.course_id.in_(course_ids), Experiment.is_active.is_(True)
            )
            .order_by(Experiment.id)
            .all()
        )

    def __repr__(self):
        return f"<User {self.email}>"


class PasswordResetToken(db.Model):
    __tablename__ = "password_reset_tokens"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    token = db.Column(db.String(120), unique=True, nullable=False,
                      default=lambda: secrets.token_urlsafe(32))
    # One hour, not twenty-four. A reset link is a live credential; the longer
    # it is valid the longer a copy of it - in a mailbox, a log, a backup - is
    # worth stealing.
    expires_at = db.Column(db.DateTime, default=lambda: utcnow() + timedelta(hours=1))
    used_at = db.Column(db.DateTime)
    user = db.relationship("User")

    @property
    def is_valid(self):
        return self.used_at is None and utcnow() < self.expires_at


# --------------------------------------------------------------------------
# Curriculum
# --------------------------------------------------------------------------

class Course(db.Model):
    __tablename__ = "courses"
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(30), unique=True, nullable=False)
    name = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)
    is_active = db.Column(db.Boolean, default=True, nullable=False)

    students = db.relationship("User", secondary=enrollments, back_populates="courses")
    experiments = db.relationship("Experiment", back_populates="course")

    def __repr__(self):
        return f"<Course {self.code}>"


class Experiment(db.Model):
    __tablename__ = "experiments"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    slug = db.Column(db.String(80), unique=True, nullable=False)
    summary = db.Column(db.Text)          # short text shown on the card
    description = db.Column(db.Text)      # long text shown in the detail popup
    objectives = db.Column(db.Text)       # newline-separated, rendered as a list
    apparatus = db.Column(db.Text)
    max_duration_min = db.Column(db.Integer, default=60, nullable=False)
    sop_pdf = db.Column(db.String(300))   # filename under SOP_FOLDER (data/sop/)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    course_id = db.Column(db.Integer, db.ForeignKey("courses.id"))
    created_at = db.Column(db.DateTime, default=utcnow)

    course = db.relationship("Course", back_populates="experiments")
    nodes = db.relationship("LabPi", back_populates="experiment")

    @property
    def online_nodes(self):
        return [n for n in self.nodes if n.is_online]

    def __repr__(self):
        return f"<Experiment {self.slug}>"


# --------------------------------------------------------------------------
# Hardware: one Lab Pi node per experiment station
# --------------------------------------------------------------------------

class LabPi(db.Model):
    """A Raspberry Pi running remote_lab_pi, wired to one experiment rig.

    Each experiment is a separate node with its own id, as specified. The
    master never talks to the hardware directly - it talks to this node's
    HTTP API, and the node owns the serial port, relays, camera and audio.
    """
    __tablename__ = "lab_pis"
    id = db.Column(db.Integer, primary_key=True)
    node_id = db.Column(db.String(60), unique=True, nullable=False, index=True)
    name = db.Column(db.String(160), nullable=False)
    ip_address = db.Column(db.String(60), nullable=False)
    port = db.Column(db.Integer, default=5000, nullable=False)
    board = db.Column(db.String(60), default="arduino")
    location = db.Column(db.String(160))
    api_token = db.Column(db.String(80), default=lambda: secrets.token_urlsafe(24))
    experiment_id = db.Column(db.Integer, db.ForeignKey("experiments.id"))
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    registered_at = db.Column(db.DateTime, default=utcnow)
    last_seen = db.Column(db.DateTime)

    experiment = db.relationship("Experiment", back_populates="nodes")
    heartbeats = db.relationship(
        "LabPiHeartbeat", back_populates="node",
        cascade="all, delete-orphan", order_by="LabPiHeartbeat.recorded_at.desc()",
    )

    OFFLINE_AFTER = timedelta(seconds=90)

    @property
    def base_url(self):
        return f"http://{self.ip_address}:{self.port}"

    @property
    def is_online(self):
        return bool(self.last_seen) and (utcnow() - self.last_seen) < self.OFFLINE_AFTER

    @property
    def latest(self):
        return self.heartbeats[0] if self.heartbeats else None

    def __repr__(self):
        return f"<LabPi {self.node_id} @ {self.ip_address}>"


class LabPiHeartbeat(db.Model):
    """Rolling telemetry from a node (CPU/RAM/temp/battery/uptime)."""
    __tablename__ = "lab_pi_heartbeats"
    id = db.Column(db.Integer, primary_key=True)
    lab_pi_id = db.Column(db.Integer, db.ForeignKey("lab_pis.id"), nullable=False)
    cpu_percent = db.Column(db.Float)
    ram_percent = db.Column(db.Float)
    temperature_c = db.Column(db.Float)
    battery_percent = db.Column(db.Float)
    battery_status = db.Column(db.String(20))  # AC | Battery
    uptime_seconds = db.Column(db.Integer)
    serial_connected = db.Column(db.Boolean, default=False)
    recorded_at = db.Column(db.DateTime, default=utcnow, index=True)

    node = db.relationship("LabPi", back_populates="heartbeats")


class OTAUpdate(db.Model):
    __tablename__ = "ota_updates"
    id = db.Column(db.Integer, primary_key=True)
    lab_pi_id = db.Column(db.Integer, db.ForeignKey("lab_pis.id"))
    firmware_file = db.Column(db.String(300))
    version = db.Column(db.String(40))
    status = db.Column(db.String(30), default="pending")
    created_at = db.Column(db.DateTime, default=utcnow)
    applied_at = db.Column(db.DateTime)
    node = db.relationship("LabPi")


# --------------------------------------------------------------------------
# Scheduling
# --------------------------------------------------------------------------

class Booking(db.Model):
    __tablename__ = "bookings"
    __table_args__ = (
        # F-16: no two live bookings may hold the same experiment + start time.
        # Partial index so cancelled/completed rows don't block rebooking.
        db.Index("uq_booking_live_slot", "experiment_id", "start_time",
                 unique=True,
                 sqlite_where=db.text("status NOT IN ('cancelled', 'completed')")),
    )
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(12), unique=True, nullable=False, default=_code)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    experiment_id = db.Column(db.Integer, db.ForeignKey("experiments.id"), nullable=False)
    lab_pi_id = db.Column(db.Integer, db.ForeignKey("lab_pis.id"))
    start_time = db.Column(db.DateTime, nullable=False, index=True)
    end_time = db.Column(db.DateTime, nullable=False)
    status = db.Column(db.String(20), default="upcoming", nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow)
    completed_at = db.Column(db.DateTime)
    cancelled_at = db.Column(db.DateTime)

    user = db.relationship("User", back_populates="bookings")
    experiment = db.relationship("Experiment")
    node = db.relationship("LabPi")
    session = db.relationship("Session", back_populates="booking",
                              uselist=False, cascade="all, delete-orphan")

    @property
    def duration_min(self):
        return int((self.end_time - self.start_time).total_seconds() // 60)

    @property
    def live_status(self):
        """Status derived from the clock, so it is correct without a cron job."""
        if self.status in ("cancelled", "completed"):
            return self.status
        now = utcnow()
        if now >= self.end_time:
            return "completed"
        if self.start_time <= now < self.end_time:
            return "active"
        return "upcoming"

    @property
    def can_start(self):
        """Startable from 5 minutes before the slot until it ends."""
        now = utcnow()
        return (
            self.status not in ("cancelled", "completed")
            and (self.start_time - timedelta(minutes=5)) <= now < self.end_time
        )

    def __repr__(self):
        return f"<Booking {self.code} {self.status}>"


class Session(db.Model):
    """An issued access grant. The key is what the node validates."""
    __tablename__ = "sessions"
    id = db.Column(db.Integer, primary_key=True)
    session_key = db.Column(db.String(24), unique=True, nullable=False,
                            default=lambda: _code(10), index=True)
    booking_id = db.Column(db.Integer, db.ForeignKey("bookings.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    experiment_id = db.Column(db.Integer, db.ForeignKey("experiments.id"), nullable=False)
    lab_pi_id = db.Column(db.Integer, db.ForeignKey("lab_pis.id"))
    starts_at = db.Column(db.DateTime, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    status = db.Column(db.String(20), default="active", nullable=False)
    pushed_to_node = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=utcnow)
    ended_at = db.Column(db.DateTime)

    booking = db.relationship("Booking", back_populates="session")
    user = db.relationship("User")
    experiment = db.relationship("Experiment")
    node = db.relationship("LabPi")

    @property
    def seconds_left(self):
        return max(0, int((self.expires_at - utcnow()).total_seconds()))

    @property
    def is_live(self):
        return self.status == "active" and self.seconds_left > 0


class SystemLog(db.Model):
    __tablename__ = "system_logs"
    id = db.Column(db.Integer, primary_key=True)
    level = db.Column(db.String(20), default="info")
    category = db.Column(db.String(40))
    message = db.Column(db.Text, nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=utcnow, index=True)
    user = db.relationship("User")

    @staticmethod
    def write(message, level="info", category="system", user_id=None):
        db.session.add(SystemLog(message=message, level=level,
                                 category=category, user_id=user_id))


class FirmwareUpload(db.Model):
    """Record of every firmware a student uploaded, kept whether or not the
    forward to the Pi succeeded - an audit trail of what was submitted, by
    whom, for which session, and why if it was rejected."""
    __tablename__ = "firmware_uploads"
    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey("sessions.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    lab_pi_id = db.Column(db.Integer, db.ForeignKey("lab_pis.id"))

    original_name = db.Column(db.String(300), nullable=False)
    stored_name = db.Column(db.String(300), nullable=False)
    board = db.Column(db.String(40))
    size_bytes = db.Column(db.Integer)
    sha256 = db.Column(db.String(64))

    status = db.Column(db.String(20), default="received")   # received|forwarded|rejected|failed
    reason = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=utcnow)
    forwarded_at = db.Column(db.DateTime)

    session = db.relationship("Session")
    user = db.relationship("User")
    node = db.relationship("LabPi")
