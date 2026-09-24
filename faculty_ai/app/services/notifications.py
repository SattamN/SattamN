"""In-app notifications (added to the caller's transaction)."""
from typing import Optional

from sqlalchemy.orm import Session

from ..models import Notification


def notify(db: Session, user_id: int, kind: str, title: str, body: str = "",
           entity_type: Optional[str] = None, entity_id: Optional[int] = None) -> None:
    db.add(Notification(user_id=user_id, kind=kind, title=title[:200], body=body,
                        entity_type=entity_type, entity_id=entity_id))
