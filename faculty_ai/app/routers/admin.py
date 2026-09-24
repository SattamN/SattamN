from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Student, User
from ..security import admin_user, hash_password, new_temp_password
from ..services.audit import audit

router = APIRouter(prefix="/admin", tags=["admin"])


class UserIn(BaseModel):
    university_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    role: Literal["faculty", "admin", "student"]
    email: Optional[str] = None


@router.post("/users", status_code=201)
def create_user(body: UserIn, db: Session = Depends(get_db), admin: User = Depends(admin_user)):
    uid = body.university_id.strip()
    if db.query(User.id).filter(User.university_id == uid).first():
        raise HTTPException(409, "A user with this ID already exists")
    student_id = None
    if body.role == "student":
        st = db.query(Student).filter(Student.university_id == uid).first() or Student(name=body.name, university_id=uid)
        db.add(st)
        db.flush()
        student_id = st.id
    temp = new_temp_password()
    u = User(university_id=uid, name=body.name.strip(), email=body.email, role=body.role,
             password_hash=hash_password(temp), must_change_password=True, student_id=student_id)
    db.add(u)
    db.flush()
    audit(db, admin, "user_created", "user", u.id, {"role": body.role})
    db.commit()
    return {"id": u.id, "university_id": uid, "role": u.role, "temporary_password": temp}


@router.get("/users")
def list_users(db: Session = Depends(get_db), admin: User = Depends(admin_user)):
    return [{"id": u.id, "university_id": u.university_id, "name": u.name, "role": u.role, "active": u.is_active,
             "last_login_at": u.last_login_at} for u in db.query(User).order_by(User.role, User.university_id).all()]


@router.post("/users/{user_id}/reset-password")
def reset_password(user_id: int, db: Session = Depends(get_db), admin: User = Depends(admin_user)):
    u = db.get(User, user_id)
    if not u:
        raise HTTPException(404, "User not found")
    temp = new_temp_password()
    u.password_hash, u.must_change_password = hash_password(temp), True
    audit(db, admin, "password_reset", "user", u.id)
    db.commit()
    return {"university_id": u.university_id, "temporary_password": temp}


@router.post("/users/{user_id}/active")
def set_active(user_id: int, active: bool, db: Session = Depends(get_db), admin: User = Depends(admin_user)):
    u = db.get(User, user_id)
    if not u:
        raise HTTPException(404, "User not found")
    if u.id == admin.id and not active:
        raise HTTPException(400, "You cannot deactivate yourself")
    u.is_active = active
    audit(db, admin, "user_activated" if active else "user_deactivated", "user", u.id)
    db.commit()
    return {"id": u.id, "active": u.is_active}


@router.get("/audit")
def audit_log(limit: int = 200, action: Optional[str] = None, db: Session = Depends(get_db), admin: User = Depends(admin_user)):
    from ..models import AuditLog

    q = db.query(AuditLog).order_by(AuditLog.id.desc())
    if action:
        q = q.filter(AuditLog.action == action)
    return [{"id": a.id, "at": a.created_at, "actor": a.actor, "action": a.action, "entity_type": a.entity_type,
             "entity_id": a.entity_id, "details": a.details} for a in q.limit(min(limit, 1000)).all()]
