from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..config import get_settings
from ..database import get_db
from ..models import PasswordResetToken, User
from ..schemas import ForgotPasswordIn, RegisterFacultyIn, ResetPasswordIn
from ..security import (
    DUMMY_HASH, create_token, current_user, hash_password, hash_reset_token, new_reset_token,
    validate_new_password, verify_password,
)
from ..services.audit import audit
from ..services.mailer import send_email

router = APIRouter(prefix="/auth", tags=["auth"])

_failures: dict[str, tuple[int, datetime]] = {}  # university_id -> (count, locked_until); in-memory, per process


class LoginIn(BaseModel):
    university_id: str
    password: str


class ChangePasswordIn(BaseModel):
    current_password: str
    new_password: str


def _session(user: User) -> dict:
    return {"access_token": create_token(user), "token_type": "bearer", "role": user.role, "name": user.name,
            "university_id": user.university_id, "must_change_password": user.must_change_password}


@router.post("/login")
def login(body: LoginIn, db: Session = Depends(get_db)):
    s = get_settings()
    uid = body.university_id.strip()
    now = datetime.now(timezone.utc)
    count, locked_until = _failures.get(uid, (0, now))
    if count >= s.login_max_failures and locked_until > now:
        raise HTTPException(429, f"Too many failed attempts. Try again in {s.login_lock_minutes} minutes.")

    user = db.query(User).filter(User.university_id == uid).first()
    ok = verify_password(body.password, user.password_hash if user else DUMMY_HASH)
    if not (ok and user and user.is_active):
        _failures[uid] = (count + 1, now + timedelta(minutes=s.login_lock_minutes))
        audit(db, None, "login_failed", actor=uid[:50])
        db.commit()
        raise HTTPException(401, "Invalid university ID or password")

    _failures.pop(uid, None)
    user.last_login_at = now
    audit(db, user, "login")
    db.commit()
    return _session(user)


@router.get("/me")
def me(user: User = Depends(current_user)):
    return {"id": user.id, "university_id": user.university_id, "name": user.name, "role": user.role,
            "must_change_password": user.must_change_password}


@router.post("/change-password")
def change_password(body: ChangePasswordIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    if not verify_password(body.current_password, user.password_hash):
        raise HTTPException(400, "Current password is incorrect")
    validate_new_password(body.new_password)
    if body.new_password == body.current_password:
        raise HTTPException(400, "Choose a different password")
    user.password_hash = hash_password(body.new_password)
    user.must_change_password = False
    audit(db, user, "password_changed")
    db.commit()
    return _session(user)  # fresh token; older ones stop working


# ------------------------------------------------------------------ self-service: register, forgot/reset password


@router.post("/register", status_code=201)
def register_faculty(body: RegisterFacultyIn, db: Session = Depends(get_db)):
    """Open self-service faculty sign-up: no admin action required. A registered account can only
    ever see the courses it creates itself (Section 6 ownership rule) — this does not grant access
    to any other instructor's data."""
    uid = body.university_id.strip()
    if db.query(User.id).filter(User.university_id == uid).first():
        raise HTTPException(409, "This university ID is already registered")
    if db.query(User.id).filter(User.email == body.email).first():
        raise HTTPException(409, "This email is already registered")
    validate_new_password(body.password)
    u = User(university_id=uid, name=body.name.strip(), email=body.email, role="faculty",
             password_hash=hash_password(body.password), must_change_password=False)
    db.add(u)
    db.flush()
    audit(db, u, "faculty_self_registered", "user", u.id, {"email": body.email})
    u.last_login_at = datetime.now(timezone.utc)
    db.commit()
    return _session(u)  # log them straight in


@router.post("/forgot-password")
def forgot_password(body: ForgotPasswordIn, db: Session = Depends(get_db)):
    """Always returns the same generic message, whether or not the account exists or has no email
    on file — this prevents an attacker from using this endpoint to discover valid university IDs
    or email addresses. If a matching account with an email is found, a reset link is sent to it."""
    generic = {"message": "If an account with that information exists, a reset link has been sent to its email."}
    q = db.query(User)
    user = None
    if body.university_id:
        user = q.filter(User.university_id == body.university_id.strip()).first()
    elif body.email:
        user = q.filter(User.email == body.email).first()
    if user is None or not user.email or not user.is_active:
        return generic

    raw, token_hash = new_reset_token()
    settings = get_settings()
    db.add(PasswordResetToken(
        user_id=user.id, token_hash=token_hash,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=settings.password_reset_ttl_minutes),
    ))
    audit(db, user, "password_reset_requested", "user", user.id)
    db.commit()

    link = f"{settings.app_base_url}/?reset_token={raw}"
    send_email(
        user.email, "Reset your NBU AI password",
        f"Hi {user.name},\n\nA password reset was requested for your account ({user.university_id}).\n"
        f"Open this link within {settings.password_reset_ttl_minutes} minutes to set a new password:\n\n{link}\n\n"
        "If you did not request this, you can ignore this email — your password will not change.",
    )
    return generic


@router.post("/reset-password")
def reset_password(body: ResetPasswordIn, db: Session = Depends(get_db)):
    validate_new_password(body.new_password)
    token_hash = hash_reset_token(body.token)
    row = db.query(PasswordResetToken).filter(PasswordResetToken.token_hash == token_hash).first()
    now = datetime.now(timezone.utc)
    expires_at = row.expires_at if row and row.expires_at.tzinfo else (row.expires_at.replace(tzinfo=timezone.utc) if row else None)
    if row is None or row.used_at is not None or expires_at < now:
        raise HTTPException(400, "This reset link is invalid or has expired. Request a new one.")
    user = db.get(User, row.user_id)
    if user is None or not user.is_active:
        raise HTTPException(400, "This reset link is invalid or has expired. Request a new one.")

    user.password_hash = hash_password(body.new_password)
    user.must_change_password = False
    row.used_at = now
    audit(db, user, "password_reset_completed", "user", user.id)
    db.commit()
    return _session(user)  # log them straight in with the new password
