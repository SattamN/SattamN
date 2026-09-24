from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import User
from ..security import faculty_user
from ..services.audit import audit
from ..services import exporter
from .exams import get_exam

router = APIRouter(tags=["export"])

_MEDIA = {
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "csv": "text/csv",
    "pdf": "application/pdf",
}


@router.get("/exams/{exam_id}/export/{fmt}")
def export_gradebook(exam_id: int, fmt: str, include_unapproved: bool = False, db: Session = Depends(get_db), user: User = Depends(faculty_user)):
    if fmt not in _MEDIA:
        raise HTTPException(400, "Format must be xlsx, csv or pdf")
    gb = exporter.build_gradebook(db, get_exam(db, exam_id, user), include_unapproved)
    if not gb["rows"]:
        raise HTTPException(409, "Nothing to export yet: no approved papers (use include_unapproved=true for a draft).")
    path = getattr(exporter, f"export_{fmt}")(gb)
    audit(db, user, "gradebook_exported", "exam", exam_id, {"format": fmt, "include_unapproved": include_unapproved})
    db.commit()
    return FileResponse(path, media_type=_MEDIA[fmt], filename=path.name)
