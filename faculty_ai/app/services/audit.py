"""Append-only audit trail."""
from typing import Optional

from sqlalchemy.orm import Session

from ..models import AuditLog, User


def audit(db: Session, user: Optional[User], action: str, entity_type: Optional[str] = None,
          entity_id: Optional[int] = None, details: Optional[dict] = None, actor: Optional[str] = None) -> None:
    """Adds a row to the caller's transaction (commit together with the change it describes)."""
    db.add(AuditLog(
        user_id=user.id if user else None,
        actor=actor or (user.university_id if user else None),
        action=action, entity_type=entity_type, entity_id=entity_id, details=details,
    ))
