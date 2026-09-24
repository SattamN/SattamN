from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload, selectinload

from ..config import get_settings
from ..database import get_db
from ..models import CourseEnrollment, Grade, Student, Submission, User
from ..security import check_course_access, faculty_user
from ..services.audit import audit
from ..schemas import GradePatch, GradeRunRequest, ReviewOut, SubmissionOut, grade_out
from ..services.ai_grader import (
    AIError,
    AIGrader,
    GradingConflict,
    ensure_gradable,
    get_grader,
    grade_submission,
    regrade_question,
    run_exam_grading,
)
from ..services.storage import resolve, save_upload
from .exams import get_exam

router = APIRouter(tags=["grading"])

PENDING_STATES = ("uploaded", "failed")


def _get_sub(db: Session, submission_id: int, user: User) -> Submission:
    s = (
        db.query(Submission)
        .options(selectinload(Submission.grades), joinedload(Submission.student))
        .filter(Submission.id == submission_id)
        .first()
    )
    if not s:
        raise HTTPException(404, "Submission not found")
    check_course_access(s.exam.course, user)
    return s


def _get_grade(db: Session, grade_id: int, user: User) -> Grade:
    g = db.get(Grade, grade_id)
    if not g:
        raise HTTPException(404, "Grade not found")
    check_course_access(g.submission.exam.course, user)
    return g


def _sub_out(s: Submission) -> SubmissionOut:
    vals = [g.final_mark if g.final_mark is not None else g.ai_mark for g in s.grades]
    vals = [v for v in vals if v is not None]
    return SubmissionOut(
        id=s.id, exam_id=s.exam_id, student_id=s.student_id, student_name=s.student.name,
        university_id=s.student.university_id, status=s.status, error=s.error,
        n_files=len(s.file_paths or []), n_graded=len(s.grades),
        n_pending_review=sum(1 for g in s.grades if g.needs_review and g.decision == "pending"),
        total=round(sum(vals), 4) if vals else None,
    )


def _require_ai(grader: Optional[AIGrader] = None) -> None:
    injected = grader is not None and grader.ai._client is not None  # e.g. a test double
    if not injected and not get_settings().anthropic_api_key:
        raise HTTPException(503, "ANTHROPIC_API_KEY is not configured on the server (see .env.example)")


# ------------------------------------------------------------------ uploading papers


def _new_submission(db: Session, exam, name: str, university_id: str, files: list[UploadFile]) -> Submission:
    exam_id = exam.id
    uid = university_id.strip()
    student = db.query(Student).filter(Student.university_id == uid).first()
    if student is None:
        student = Student(name=name.strip() or uid, university_id=uid)
        db.add(student)
        db.flush()
    if db.query(Submission.id).filter_by(exam_id=exam_id, student_id=student.id).first():
        raise HTTPException(409, f"A submission for student {uid} already exists in this exam")
    if not db.query(CourseEnrollment.id).filter_by(course_id=exam.course_id, student_id=student.id).first():
        db.add(CourseEnrollment(course_id=exam.course_id, student_id=student.id))  # convenience: auto-enroll
    paths = [save_upload(f, f"exam_{exam_id}") for f in files]
    sub = Submission(exam_id=exam_id, student_id=student.id, file_paths=paths, status="uploaded")
    db.add(sub)
    db.flush()
    return sub


@router.post("/exams/{exam_id}/submissions", response_model=SubmissionOut, status_code=201)
def upload_submission(
    exam_id: int,
    student_name: str = Form(""),
    university_id: str = Form(...),
    files: list[UploadFile] = File(...),
    db: Session = Depends(get_db), user: User = Depends(faculty_user),
):
    exam = get_exam(db, exam_id, user)
    if not university_id.strip():
        raise HTTPException(400, "university_id is required")
    sub = _new_submission(db, exam, student_name, university_id, files)
    db.commit()
    return _sub_out(_get_sub(db, sub.id, user))


@router.post("/exams/{exam_id}/submissions/bulk")
def upload_bulk(exam_id: int, files: list[UploadFile] = File(...), db: Session = Depends(get_db), user: User = Depends(faculty_user)):
    """One file per student; the file name (without extension) is the university ID, e.g. 4412345.pdf."""
    exam = get_exam(db, exam_id, user)
    created, skipped = [], []
    for f in files:
        uid = Path(f.filename or "").stem.strip()
        if not uid:
            skipped.append({"file": f.filename, "reason": "no student id in file name"})
            continue
        try:
            sub = _new_submission(db, exam, "", uid, [f])
            db.commit()
            created.append(uid)
        except HTTPException as e:
            db.rollback()
            skipped.append({"file": f.filename, "reason": e.detail})
    return {"created": created, "skipped": skipped}


@router.get("/exams/{exam_id}/submissions", response_model=list[SubmissionOut])
def list_submissions(exam_id: int, db: Session = Depends(get_db), user: User = Depends(faculty_user)):
    get_exam(db, exam_id, user)
    subs = (
        db.query(Submission)
        .options(selectinload(Submission.grades), joinedload(Submission.student))
        .filter(Submission.exam_id == exam_id)
        .all()
    )
    return [_sub_out(s) for s in sorted(subs, key=lambda s: s.student.university_id)]


@router.patch("/students/{student_id}")
def rename_student(student_id: int, name: str = Form(...), db: Session = Depends(get_db), user: User = Depends(faculty_user)):
    st = db.get(Student, student_id)
    from ..models import Course
    mine = db.query(CourseEnrollment.id).join(Course, CourseEnrollment.course_id == Course.id).filter(
        CourseEnrollment.student_id == student_id, *([] if user.role == "admin" else [Course.instructor_id == user.id])).first()
    if not st or not mine:
        raise HTTPException(404, "Student not found")
    st.name = name.strip()
    db.commit()
    return {"id": st.id, "name": st.name}


@router.delete("/submissions/{submission_id}", status_code=204)
def delete_submission(submission_id: int, db: Session = Depends(get_db), user: User = Depends(faculty_user)):
    s = _get_sub(db, submission_id, user)
    audit(db, user, "submission_deleted", "submission", s.id, {"status": s.status})
    db.delete(s)
    db.commit()


@router.get("/submissions/{submission_id}/file/{index}")
def get_submission_file(submission_id: int, index: int, db: Session = Depends(get_db), user: User = Depends(faculty_user)):
    s = _get_sub(db, submission_id, user)
    if index < 0 or index >= len(s.file_paths or []):
        raise HTTPException(404, "File not found")
    return FileResponse(resolve(s.file_paths[index]))


# ------------------------------------------------------------------ running the AI


@router.post("/exams/{exam_id}/grade", status_code=202)
def grade_exam(
    exam_id: int,
    background: BackgroundTasks,
    body: Optional[GradeRunRequest] = None,
    db: Session = Depends(get_db), user: User = Depends(faculty_user),
    grader: AIGrader = Depends(get_grader),
):
    """Queue AI grading for pending papers (or the given ids). Poll /grading-status."""
    exam = get_exam(db, exam_id, user)
    body = body or GradeRunRequest()
    try:
        ensure_gradable(exam)
    except ValueError as e:
        raise HTTPException(409, str(e))
    _require_ai(grader)

    q = db.query(Submission).filter(Submission.exam_id == exam_id, Submission.status != "approved",
                                    Submission.status != "grading", Submission.status != "queued")
    if body.submission_ids:
        q = q.filter(Submission.id.in_(body.submission_ids))
    elif not body.force:
        q = q.filter(Submission.status.in_(PENDING_STATES))
    subs = q.all()
    for s in subs:
        s.status = "queued"
    db.commit()
    ids = [s.id for s in subs]
    if ids:
        background.add_task(run_exam_grading, ids, force=body.force, grader=grader)
    return {"queued": len(ids)}


@router.get("/exams/{exam_id}/grading-status")
def grading_status(exam_id: int, db: Session = Depends(get_db), user: User = Depends(faculty_user)):
    get_exam(db, exam_id, user)
    rows = db.query(Submission.status, func.count()).filter(Submission.exam_id == exam_id).group_by(Submission.status).all()
    counts = {k: v for k, v in rows}
    total = sum(counts.values())
    busy = counts.get("queued", 0) + counts.get("grading", 0)
    return {"total": total, "counts": counts, "in_progress": busy > 0}


@router.post("/submissions/{submission_id}/grade", response_model=SubmissionOut)
def grade_one(submission_id: int, force: bool = False, db: Session = Depends(get_db), user: User = Depends(faculty_user),
              grader: AIGrader = Depends(get_grader)):
    s = _get_sub(db, submission_id, user)
    if s.status in ("queued", "grading"):
        raise HTTPException(409, "Submission is already being graded")
    _require_ai(grader)
    try:
        grade_submission(db, s, grader=grader, force=force)
    except ValueError as e:
        raise HTTPException(409, str(e))
    except GradingConflict as e:
        raise HTTPException(409, str(e))
    except AIError as e:
        raise HTTPException(502, str(e))
    return _sub_out(_get_sub(db, submission_id, user))


# ------------------------------------------------------------------ review


def _review(s: Submission) -> ReviewOut:
    q_by_id = {g.question_id: g.question for g in s.grades}
    grades = sorted(s.grades, key=lambda g: q_by_id[g.question_id].number)
    vals = [g.final_mark if g.final_mark is not None else g.ai_mark for g in grades]
    vals = [v for v in vals if v is not None]
    return ReviewOut(
        submission_id=s.id, exam_id=s.exam_id, student_name=s.student.name, university_id=s.student.university_id,
        status=s.status, n_files=len(s.file_paths or []),
        total_max=round(sum(q.max_mark for q in s.exam.questions), 4),
        total_current=round(sum(vals), 4) if vals else None,
        grades=[grade_out(g) for g in grades],
    )


@router.get("/submissions/{submission_id}/review", response_model=ReviewOut)
def review_submission(submission_id: int, db: Session = Depends(get_db), user: User = Depends(faculty_user)):
    return _review(_get_sub(db, submission_id, user))


def _editable(g: Grade) -> None:
    if g.submission.status == "approved":
        raise HTTPException(409, "Submission is approved. Reopen it to edit marks.")


@router.patch("/grades/{grade_id}")
def set_final_mark(grade_id: int, body: GradePatch, db: Session = Depends(get_db), user: User = Depends(faculty_user)):
    g = _get_grade(db, grade_id, user)
    _editable(g)
    mx = g.question.max_mark
    before = g.final_mark
    if body.final_mark < 0 or body.final_mark > mx:
        raise HTTPException(400, f"final_mark must be between 0 and {mx:g}")
    g.final_mark = round(body.final_mark, 4)
    g.reviewer_note = body.reviewer_note
    g.decision = "accepted_ai" if (g.ai_mark is not None and abs(g.ai_mark - g.final_mark) < 1e-6) else "edited"
    audit(db, user, "grade_set", "grade", g.id, {"submission_id": g.submission_id, "question": g.question.number,
                                                "ai_mark": g.ai_mark, "before": before, "after": g.final_mark,
                                                "decision": g.decision, "note": body.reviewer_note})
    db.commit()
    return grade_out(g)


@router.post("/grades/{grade_id}/accept-ai")
def accept_ai_mark(grade_id: int, db: Session = Depends(get_db), user: User = Depends(faculty_user)):
    g = _get_grade(db, grade_id, user)
    _editable(g)
    if g.ai_mark is None:
        raise HTTPException(400, "The AI gave no mark for this question; enter a mark manually")
    before = g.final_mark
    g.final_mark = g.ai_mark
    g.decision = "accepted_ai"
    audit(db, user, "grade_set", "grade", g.id, {"submission_id": g.submission_id, "question": g.question.number,
                                                "ai_mark": g.ai_mark, "before": before, "after": g.final_mark,
                                                "decision": "accepted_ai"})
    db.commit()
    return grade_out(g)


@router.post("/grades/{grade_id}/regrade")
def regrade(grade_id: int, force: bool = False, db: Session = Depends(get_db), user: User = Depends(faculty_user), grader: AIGrader = Depends(get_grader)):
    g = _get_grade(db, grade_id, user)
    _require_ai(grader)
    try:
        audit(db, user, "grade_regraded", "grade", g.id, {"question": g.question.number, "force": force,
                                                         "previous_final": g.final_mark})
        regrade_question(db, g, grader=grader, force=force)
    except GradingConflict as e:
        raise HTTPException(409, str(e))
    except AIError as e:
        raise HTTPException(502, str(e))
    return grade_out(g)


# ------------------------------------------------------------------ approval


def approve_submission(db: Session, s: Submission, source: str = "auto_approved") -> None:
    """Approve = instructor signs off the whole paper. Flagged questions must have been reviewed first;
    unflagged ones are accepted at their AI mark (recorded as `auto_approved`)."""
    if s.status == "approved":
        return
    if s.status != "graded":
        raise HTTPException(409, f"Only graded papers can be approved (status: {s.status})")
    n_questions = len(s.exam.questions)
    if len(s.grades) != n_questions:
        raise HTTPException(409, "Paper is not fully graded")
    blocked = sorted(g.question.number for g in s.grades if g.needs_review and g.decision == "pending")
    if blocked:
        raise HTTPException(409, f"Review flagged question(s) {blocked} before approving")
    for g in s.grades:
        if g.final_mark is None:
            if g.ai_mark is None:
                raise HTTPException(409, f"Question {g.question.number} has no mark")
            g.final_mark = g.ai_mark
            g.decision = source
    s.status = "approved"
    s.approved_at = datetime.now(timezone.utc)


@router.post("/submissions/{submission_id}/approve", response_model=SubmissionOut)
def approve(submission_id: int, db: Session = Depends(get_db), user: User = Depends(faculty_user)):
    s = _get_sub(db, submission_id, user)
    approve_submission(db, s)
    audit(db, user, "submission_approved", "submission", s.id, {"total": sum(g.final_mark or 0 for g in s.grades)})
    db.commit()
    return _sub_out(_get_sub(db, submission_id, user))


@router.post("/submissions/{submission_id}/reopen", response_model=SubmissionOut)
def reopen(submission_id: int, db: Session = Depends(get_db), user: User = Depends(faculty_user)):
    s = _get_sub(db, submission_id, user)
    if s.status != "approved":
        raise HTTPException(409, "Submission is not approved")
    s.status, s.approved_at = "graded", None
    audit(db, user, "submission_reopened", "submission", s.id)
    db.commit()
    return _sub_out(_get_sub(db, submission_id, user))


@router.post("/exams/{exam_id}/approve-unflagged")
def approve_unflagged(exam_id: int, db: Session = Depends(get_db), user: User = Depends(faculty_user)):
    """Bulk-approve every graded paper that has NO question needing review."""
    get_exam(db, exam_id, user)
    subs = (
        db.query(Submission)
        .options(selectinload(Submission.grades))
        .filter(Submission.exam_id == exam_id, Submission.status == "graded")
        .all()
    )
    approved, held = 0, 0
    for s in subs:
        if any(g.needs_review and g.decision == "pending" for g in s.grades):
            held += 1
            continue
        try:
            approve_submission(db, s, source="auto_approved")
            approved += 1
        except HTTPException:
            held += 1
    audit(db, user, "bulk_approved_unflagged", "exam", exam_id, {"approved": approved, "held": held})
    db.commit()
    return {"approved": approved, "held_for_review": held}
