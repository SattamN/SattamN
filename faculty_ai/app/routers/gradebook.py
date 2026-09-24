from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Submission, User
from ..security import faculty_user
from ..services import bulk_grades
from ..services.audit import audit
from ..services.exporter import build_editable_xlsx, build_gradebook
from .exams import get_exam

router = APIRouter(tags=["gradebook"])

MAX_IMPORT_MB = 10


@router.get("/exams/{exam_id}/gradebook")
def gradebook(exam_id: int, include_unapproved: bool = False, db: Session = Depends(get_db),
              user: User = Depends(faculty_user)):
    return build_gradebook(db, get_exam(db, exam_id, user), include_unapproved)


@router.get("/exams/{exam_id}/gradebook/editable-xlsx")
def download_editable_gradebook(exam_id: int, db: Session = Depends(get_db), user: User = Depends(faculty_user)):
    """A re-uploadable Excel version of the gradebook (current marks, one plain cell per question,
    sheet-protected so only marks can be edited). Includes ungraded/unapproved papers too, since the
    point is to let the instructor fix any mark, not only approved ones."""
    exam = get_exam(db, exam_id, user)
    gb = build_gradebook(db, exam, include_unapproved=True)
    if not gb["rows"]:
        raise HTTPException(409, "No submissions to export yet.")
    path = build_editable_xlsx(gb)
    return FileResponse(path, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        filename=path.name)


@router.post("/exams/{exam_id}/gradebook/import")
def import_gradebook(exam_id: int, commit: bool = False, file: UploadFile = File(...),
                     db: Session = Depends(get_db), user: User = Depends(faculty_user)):
    """Preview (commit=false, default) or apply (commit=true) mark changes from a re-uploaded Excel
    file. Always inspect the preview first: nothing is written until commit=true is sent, and a second
    call is required to actually commit (the frontend should show the diff and ask for confirmation
    before re-calling with commit=true)."""
    exam = get_exam(db, exam_id, user)
    data = file.file.read(MAX_IMPORT_MB * 1024 * 1024 + 1)
    if len(data) > MAX_IMPORT_MB * 1024 * 1024:
        raise HTTPException(413, f"File exceeds {MAX_IMPORT_MB} MB")
    try:
        parsed = bulk_grades.parse_editable_xlsx(data)
    except ValueError as e:
        raise HTTPException(400, str(e))
    diff = bulk_grades.compute_diff(db, exam, parsed)

    if not commit:
        return {"committed": False, **diff}

    if not diff["changes"]:
        return {"committed": True, "applied": 0, "reopened_submissions": 0, **diff}
    result = bulk_grades.apply_diff(db, exam, diff, user)
    return {"committed": True, **result, **diff}


def _approved_count(db: Session, exam_id: int) -> int:
    return db.query(Submission.id).filter(Submission.exam_id == exam_id, Submission.status == "approved").count()


@router.post("/exams/{exam_id}/publish")
def publish_grades(exam_id: int, appeal_days: int = 7, db: Session = Depends(get_db),
                   user: User = Depends(faculty_user)):
    """Make APPROVED final marks visible to the students concerned. Unapproved papers stay hidden.
    `appeal_days` = how long students may request a review (0 = no review requests)."""
    if not 0 <= appeal_days <= 60:
        raise HTTPException(400, "appeal_days must be between 0 and 60")
    exam = get_exam(db, exam_id, user)
    now = datetime.now(timezone.utc)
    exam.grades_published_at = now
    exam.appeals_deadline = now + timedelta(days=appeal_days) if appeal_days else None
    visible = _approved_count(db, exam_id)
    audit(db, user, "grades_published", "exam", exam.id, {"visible_to_students": visible, "appeal_days": appeal_days})
    db.commit()
    return {"published": True, "visible_to_students": visible,
            "still_hidden": db.query(Submission.id).filter(Submission.exam_id == exam_id).count() - visible}


@router.post("/exams/{exam_id}/unpublish")
def unpublish_grades(exam_id: int, db: Session = Depends(get_db), user: User = Depends(faculty_user)):
    exam = get_exam(db, exam_id, user)
    exam.grades_published_at = None
    exam.appeals_deadline = None
    audit(db, user, "grades_unpublished", "exam", exam.id)
    db.commit()
    return {"published": False}
