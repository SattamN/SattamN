from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Notification, User
from ..security import current_user

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("")
def list_notifications(unread: bool = False, limit: int = 50, db: Session = Depends(get_db),
                       user: User = Depends(current_user)):
    q = db.query(Notification).filter(Notification.user_id == user.id)
    if unread:
        q = q.filter(Notification.read_at.is_(None))
    rows = q.order_by(Notification.id.desc()).limit(min(limit, 200)).all()
    unread_count = db.query(Notification.id).filter(Notification.user_id == user.id, Notification.read_at.is_(None)).count()
    return {"unread": unread_count, "items": [
        {"id": n.id, "kind": n.kind, "title": n.title, "body": n.body, "entity_type": n.entity_type,
         "entity_id": n.entity_id, "created_at": n.created_at, "read": n.read_at is not None} for n in rows]}


@router.post("/{notification_id}/read")
def mark_read(notification_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    n = db.query(Notification).filter(Notification.id == notification_id, Notification.user_id == user.id).first()
    if n is None:
        raise HTTPException(404, "Not found")
    n.read_at = n.read_at or datetime.now(timezone.utc)
    db.commit()
    return {"id": n.id, "read": True}


@router.post("/read-all")
def mark_all_read(db: Session = Depends(get_db), user: User = Depends(current_user)):
    now = datetime.now(timezone.utc)
    n = db.query(Notification).filter(Notification.user_id == user.id, Notification.read_at.is_(None)).update({"read_at": now})
    db.commit()
    return {"marked": n}
