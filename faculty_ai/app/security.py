"""Passwords, tokens and role-based access."""
import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from .config import get_settings
from .database import get_db
from .models import Course, User

_bearer = HTTPBearer(auto_error=False)
_ALPHABET = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no look-alike characters
MIN_PASSWORD_LEN = 8
PASSWORD_CHANGE_PATHS = {"/auth/change-password", "/auth/me"}


# ------------------------------------------------------------------ passwords (scrypt, stdlib)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(dk).decode()


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt_b64, dk_b64 = stored.split("$")
        salt, expected = base64.b64decode(salt_b64), base64.b64decode(dk_b64)
        dk = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
        return hmac.compare_digest(dk, expected)
    except Exception:
        return False


DUMMY_HASH = hash_password("dummy-password-for-timing")  # unknown IDs cost the same as wrong passwords


def new_temp_password(length: int = 10) -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(length))


def new_reset_token() -> tuple[str, str]:
    """Returns (raw_token_for_the_email_link, sha256_hash_to_store). The raw token is never stored."""
    raw = secrets.token_urlsafe(32)
    return raw, hashlib.sha256(raw.encode()).hexdigest()


def hash_reset_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def validate_new_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LEN:
        raise HTTPException(400, f"Password must be at least {MIN_PASSWORD_LEN} characters")


# ------------------------------------------------------------------ tokens


def _pv(user: User) -> str:
    """Fingerprint of the current password: changing/resetting it invalidates old tokens."""
    return hashlib.sha256(user.password_hash.encode()).hexdigest()[:12]


def create_token(user: User) -> str:
    s = get_settings()
    exp = datetime.now(timezone.utc) + timedelta(minutes=s.token_ttl_minutes)
    return jwt.encode({"sub": str(user.id), "role": user.role, "pv": _pv(user), "exp": exp}, s.secret_key, algorithm="HS256")


def current_user(
    request: Request,
    creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    db: Session = Depends(get_db),
) -> User:
    if creds is None:
        raise HTTPException(401, "Not authenticated", headers={"WWW-Authenticate": "Bearer"})
    try:
        payload = jwt.decode(creds.credentials, get_settings().secret_key, algorithms=["HS256"])
        user = db.get(User, int(payload["sub"]))
    except Exception:
        raise HTTPException(401, "Invalid or expired session", headers={"WWW-Authenticate": "Bearer"})
    if user is None or not user.is_active or payload.get("pv") != _pv(user):
        raise HTTPException(401, "Invalid or expired session", headers={"WWW-Authenticate": "Bearer"})
    if user.must_change_password and request.url.path not in PASSWORD_CHANGE_PATHS:
        raise HTTPException(403, "Password change required")
    return user


def require_role(*roles: str):
    def dep(user: User = Depends(current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(403, "You do not have access to this resource")
        return user

    return dep


faculty_user = require_role("faculty", "admin")
student_user = require_role("student")
admin_user = require_role("admin")


# ------------------------------------------------------------------ ownership


def check_course_access(course: Optional[Course], user: User) -> Course:
    """Faculty only see their own courses (admin sees all). Returns 404, not 403, so IDs can't be probed."""
    if course is None or (user.role != "admin" and course.instructor_id != user.id):
        raise HTTPException(404, "Not found")
    return course
