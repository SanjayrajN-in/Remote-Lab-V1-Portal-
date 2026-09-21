"""Bulk user import from CSV or Excel.

Accepted headers (case-insensitive, order does not matter):

    full_name, email, roll_number, department, courses, role

`courses` is a semicolon- or comma-separated list of course codes; unknown
codes are reported rather than silently dropped. Every row is validated
before anything is written, and the whole import runs in one transaction, so
a bad file leaves no half-created users behind.
"""
import csv
import io
import re
import secrets

from models import Course, Department, User, db

REQUIRED = {"full_name", "email"}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PW_ALPHABET = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def temp_password(n=10):
    return "".join(secrets.choice(_PW_ALPHABET) for _ in range(n))


def _norm(name):
    return re.sub(r"[^a-z0-9]+", "_", (name or "").strip().lower()).strip("_")


def read_rows(storage):
    """Return (rows, error). Rows are dicts with normalised keys."""
    filename = (storage.filename or "").lower()
    raw = storage.read()
    if not raw:
        return [], "The file is empty."

    if filename.endswith((".xlsx", ".xlsm")):
        try:
            from openpyxl import load_workbook
        except ImportError:
            return [], "Excel support needs openpyxl. Install it, or upload a CSV."
        try:
            wb = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        except Exception:
            return [], "That file could not be read as an Excel workbook."
        ws = wb.active
        rows_iter = ws.iter_rows(values_only=True)
        try:
            header = [_norm(str(c)) for c in next(rows_iter)]
        except StopIteration:
            return [], "The sheet has no header row."
        rows = []
        for values in rows_iter:
            if values is None or all(v is None or str(v).strip() == "" for v in values):
                continue
            rows.append({h: ("" if v is None else str(v).strip())
                         for h, v in zip(header, values)})
        return rows, None

    # CSV / TSV
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    if not reader.fieldnames:
        return [], "The file has no header row."
    rows = []
    for r in reader:
        row = {_norm(k): (v or "").strip() for k, v in r.items() if k}
        if any(row.values()):
            rows.append(row)
    return rows, None


def validate(rows):
    """Return (clean, problems). Never touches the database."""
    header = set(rows[0].keys()) if rows else set()
    missing = REQUIRED - header
    if missing:
        return [], [f"Missing required column(s): {', '.join(sorted(missing))}"]

    known_courses = {c.code.lower(): c for c in Course.query.all()}
    existing = {e.lower() for (e,) in db.session.query(User.email).all()}

    clean, problems, seen = [], [], set()
    for i, row in enumerate(rows, start=2):  # row 1 is the header
        name = (row.get("full_name") or "").strip()
        email = (row.get("email") or "").strip().lower()

        if not name:
            problems.append(f"Row {i}: full_name is blank.")
            continue
        if not EMAIL_RE.match(email):
            problems.append(f"Row {i}: '{email or 'blank'}' is not a valid email.")
            continue
        if email in existing:
            problems.append(f"Row {i}: {email} already has an account - skipped.")
            continue
        if email in seen:
            problems.append(f"Row {i}: {email} appears twice in this file - skipped.")
            continue
        seen.add(email)

        codes = [c.strip() for c in re.split(r"[;,]", row.get("courses", "")) if c.strip()]
        courses, unknown = [], []
        for c in codes:
            found = known_courses.get(c.lower())
            (courses.append(found) if found else unknown.append(c))
        if unknown:
            problems.append(f"Row {i}: unknown course code(s) {', '.join(unknown)} - ignored.")

        role = (row.get("role") or "user").strip().lower()
        clean.append({
            "full_name": name,
            "email": email,
            "roll_number": row.get("roll_number") or None,
            "department": row.get("department") or None,
            "courses": courses,
            "role": "admin" if role == "admin" else "user",
        })
    return clean, problems


def create_users(clean):
    """Create the validated users. Returns [(user, temp_password), ...]."""
    dept_cache = {d.name.lower(): d for d in Department.query.all()}
    created = []
    for item in clean:
        dept = None
        if item["department"]:
            key = item["department"].lower()
            dept = dept_cache.get(key)
            if not dept:
                dept = Department(name=item["department"])
                db.session.add(dept)
                dept_cache[key] = dept

        pw = temp_password()
        user = User(
            full_name=item["full_name"],
            email=item["email"],
            roll_number=item["roll_number"],
            role=item["role"],
            department=dept,
            must_change_password=True,
        )
        user.set_password(pw)
        user.courses = item["courses"]
        db.session.add(user)
        created.append((user, pw))
    return created
