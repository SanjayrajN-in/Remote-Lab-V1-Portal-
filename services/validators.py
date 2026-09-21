"""Per-field input validation for admin-entered fields."""
import re

_DANGEROUS = re.compile(r"""[<>{}\[\]\\;`"']|--|/\*|\*/|script""", re.I)
_NAME_OK = re.compile(r"^[A-Za-z0-9 .\-']+$")
_ROLL_OK = re.compile(r"^[A-Za-z0-9\-/]+$")
_LABEL_OK = re.compile(r"^[A-Za-z0-9 .\-_,()]+$")
_EMAIL_OK = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


def name(value, field="Name"):
    v = (value or "").strip()
    if not v:
        return False, f"{field} is required."
    if len(v) > 160:
        return False, f"{field} is too long."
    if not _NAME_OK.match(v):
        return False, f"{field} may only contain letters, spaces, and . - '"
    return True, v


def email(value):
    v = (value or "").strip().lower()
    if not v:
        return False, "Email is required."
    if len(v) > 200 or not _EMAIL_OK.match(v):
        return False, "Enter a valid email address."
    return True, v


def roll_number(value):
    v = (value or "").strip()
    if not v:
        return True, None
    if not _ROLL_OK.match(v):
        return False, "Roll number may only contain letters, numbers, - and /."
    return True, v


def label(value, field="Name"):
    v = (value or "").strip()
    if not v:
        return False, f"{field} is required."
    if len(v) > 200:
        return False, f"{field} is too long."
    if _DANGEROUS.search(v) or not _LABEL_OK.match(v):
        return False, f"{field} contains characters that aren't allowed. Use letters, numbers, spaces and . - _ , ( )"
    return True, v
