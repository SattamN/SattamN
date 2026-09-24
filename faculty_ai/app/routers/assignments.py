"""Assignments: a lighter-weight sibling of exams for ongoing coursework.

Same discipline as exams throughout: AI proposes (ai_mark/ai_reason/ai_rubric_breakdown), the rule
engine decides what needs review, and only the instructor ever writes final_mark. Students can only
ever see their own, approved, released mark.
"""
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload, selectinload

from ..config import get_settings
from ..database import get_db
from ..models import Assignment, AssignmentSubmission, CourseEnrollment, Student, User
from ..schemas import (
    AssignmentIn, AssignmentOut, AssignmentReviewOut, AssignmentSubmissionOut, AssignmentUpdate,
    GradePatch, RubricIn, assignment_out, asub_out, asub_review_out,
)
from ..security import check_course_access, faculty_user, student_user
from ..services import rubric as rubric_svc
from ..services.ai_grader import (
    AIClient, AIError, AIGrader, GradingConflict, ensure_assignment_gradable, get_grader,
    grade_assignment_submission, make_logger, run_assignment_grading,
)
from ..services.audit import audit
from ..services.storage import resolve, save_upload
from .courses import get_course

router = APIRouter(tags=["assignments"])
student_router = APIRouter(prefix="/me", tags=["student: assignments"])

PENDING_STATES = ("uploaded", "failed")


def get_assignment(db: Session, assignment_id: int, user: User) -> Assignment:
    a = db.get(Assignment, assignment_id)
    if not a:
        raise HTTPException(404, "Assignment not found")
    check_course_access(a.course, user)
    return a


def _get_asub(db: Session, submission_id: int, user: User) -> AssignmentSubmission:
    s = (
        db.query(AssignmentSubmission)
        .options(selectinload(AssignmentSubmission.student))
        .filter(AssignmentSubmission.id == submission_id)
        .first()
    )
    if not s:
        raise HTTPException(404, "Submission not found")
    check_course_access(s.assignment.course, user)
    return s


def _locked(db: Session, assignment_id: int) -> None:
    """Once any submission has moved past 'uploaded', the assignment's rubric/max_mark are frozen —
    same rule exam questions follow once any grade exists."""
    graded = (
        db.query(AssignmentSubmission.id)
        .filter(AssignmentSubmission.assignment_id == assignment_id, AssignmentSubmission.status != "uploaded")
        .first()
    )
    if graded:
        raise HTTPException(409, "This assignment already has graded submissions; rubric and max mark are locked.")


def _require_ai(grader: Optional[AIGrader] = None) -> None:
    injected = grader is not None and grader.ai._client is not None
    if not injected and not get_settings().anthropic_api_key:
        raise HTTPException(503, "ANTHROPIC_API_KEY is not configured on the server (see .env.example)")


# ------------------------------------------------------------------ faculty: CRUD + rubric


@router.post("/courses/{course_id}/assignments", response_model=AssignmentOut, status_code=201)
def create_assignment(course_id: int, body: AssignmentIn, db: Session = Depends(get_db),
                      user: User = Depends(faculty_user)):
    get_course(db, course_id, user)
    a = Assignment(course_id=course_id, title=body.title.strip(), description=body.description,
                   max_mark=body.max_mark, model_answer=body.model_answer, due_at=body.due_at,
                   allow_late=body.allow_late)
    if body.rubric:
        try:
            rub = rubric_svc.normalize_rubric(body.rubric.model_dump())
            rubric_svc.validate_rubric_total(rub, body.max_mark)
        except ValueError as e:
            raise HTTPException(400, str(e))
        a.rubric_json, a.rubric_approved = rub, False
    db.add(a)
    db.flush()
    audit(db, user, "assignment_created", "assignment", a.id, {"title": a.title, "course_id": course_id})
    db.commit()
    db.refresh(a)
    return assignment_out(a)


@router.get("/courses/{course_id}/assignments", response_model=list[AssignmentOut])
def list_assignments(course_id: int, db: Session = Depends(get_db), user: User = Depends(faculty_user)):
    get_course(db, course_id, user)
    rows = db.query(Assignment).filter(Assignment.course_id == course_id).order_by(Assignment.created_at.desc()).all()
    return [assignment_out(a) for a in rows]


@router.get("/assignments/{assignment_id}", response_model=AssignmentOut)
def read_assignment(assignment_id: int, db: Session = Depends(get_db), user: User = Depends(faculty_user)):
    return assignment_out(get_assignment(db, assignment_id, user))


@router.put("/assignments/{assignment_id}", response_model=AssignmentOut)
def update_assignment(assignment_id: int, body: AssignmentUpdate, db: Session = Depends(get_db),
                      user: User = Depends(faculty_user)):
    a = get_assignment(db, assignment_id, user)
    data = body.model_dump(exclude_unset=True)
    if "max_mark" in data:
        _locked(db, assignment_id)
        if a.rubric_json:
            try:
                rubric_svc.validate_rubric_total(a.rubric_json, data["max_mark"])
            except ValueError:
                a.rubric_approved = False
    for k, v in data.items():
        setattr(a, k, v)
    db.commit()
    db.refresh(a)
    return assignment_out(a)


@router.delete("/assignments/{assignment_id}", status_code=204)
def delete_assignment(assignment_id: int, db: Session = Depends(get_db), user: User = Depends(faculty_user)):
    a = get_assignment(db, assignment_id, user)
    audit(db, user, "assignment_deleted", "assignment", a.id, {"title": a.title})
    db.delete(a)
    db.commit()


@router.post("/assignments/{assignment_id}/rubric/generate", response_model=AssignmentOut)
def generate_assignment_rubric(assignment_id: int, mode: str = "ai", db: Session = Depends(get_db),
                               user: User = Depends(faculty_user)):
    a = get_assignment(db, assignment_id, user)
    _locked(db, assignment_id)
    if mode == "default":
        rub = rubric_svc.default_rubric(a.max_mark)
    elif mode == "ai":
        log = make_logger(db)

        class _Q:  # minimal shim: rubric_svc.generate_rubric only reads these three fields
            text, model_answer, max_mark = a.description, a.model_answer, a.max_mark

        try:
            rub = rubric_svc.generate_rubric(_Q(), AIClient(), on_call=log)
        except AIError as e:
            db.commit()
            raise HTTPException(502, str(e))
    else:
        raise HTTPException(400, "mode must be 'ai' or 'default'")
    a.rubric_json, a.rubric_approved = rub, False
    db.commit()
    db.refresh(a)
    return assignment_out(a)


@router.put("/assignments/{assignment_id}/rubric", response_model=AssignmentOut)
def save_and_approve_assignment_rubric(assignment_id: int, body: RubricIn, db: Session = Depends(get_db),
                                       user: User = Depends(faculty_user)):
    a = get_assignment(db, assignment_id, user)
    _locked(db, assignment_id)
    try:
        rub = rubric_svc.normalize_rubric(body.model_dump())
        rubric_svc.validate_rubric_total(rub, a.max_mark)
    except ValueError as e:
        raise HTTPException(400, str(e))
    a.rubric_json, a.rubric_approved = rub, True
    db.commit()
    db.refresh(a)
    return assignment_out(a)


# ------------------------------------------------------------------ student: submit


@student_router.post("/assignments/{assignment_id}/submit", response_model=AssignmentSubmissionOut, status_code=201)
def submit_assignment(assignment_id: int, files: list[UploadFile] = File(...), db: Session = Depends(get_db),
                      user: User = Depends(student_user)):
    a = db.get(Assignment, assignment_id)
    enrolled = a and db.query(CourseEnrollment.id).filter_by(course_id=a.course_id, student_id=user.student_id).first()
    if not a or not enrolled:
        raise HTTPException(404, "Not found")
    if not files:
        raise HTTPException(400, "Attach at least one file")

    now = datetime.now(timezone.utc)
    late = False
    if a.due_at is not None:
        due = a.due_at if a.due_at.tzinfo else a.due_at.replace(tzinfo=timezone.utc)
        if now > due:
            if not a.allow_late:
                raise HTTPException(409, "The due date for this assignment has passed")
            late = True

    sub = db.query(AssignmentSubmission).filter_by(assignment_id=a.id, student_id=user.student_id).first()
    if sub is not None and sub.status != "uploaded":
        raise HTTPException(409, "You have already submitted this assignment. Ask your instructor to reopen it to resubmit.")

    paths = [save_upload(f, f"assignment_{a.id}") for f in files]
    if sub is None:
        sub = AssignmentSubmission(assignment_id=a.id, student_id=user.student_id)
        db.add(sub)
    sub.file_paths, sub.submitted_at, sub.late, sub.status, sub.error = paths, now, late, "uploaded", None
    db.flush()
    audit(db, user, "assignment_submitted", "assignment_submission", sub.id, {"assignment_id": a.id, "late": late})
    db.commit()
    db.refresh(sub)
    return asub_out(sub)


@student_router.get("/courses/{course_id}/assignments")
def my_assignments(course_id: int, db: Session = Depends(get_db), user: User = Depends(student_user)):
    if not db.query(CourseEnrollment.id).filter_by(course_id=course_id, student_id=user.student_id).first():
        raise HTTPException(404, "Not found")
    rows = db.query(Assignment).filter(Assignment.course_id == course_id).order_by(Assignment.due_at).all()
    out = []
    for a in rows:
        sub = db.query(AssignmentSubmission).filter_by(assignment_id=a.id, student_id=user.student_id).first()
        released = a.results_released_at is not None
        mark = sub.final_mark if (sub and released and sub.status == "approved") else None
        out.append({
            "id": a.id, "title": a.title, "max_mark": a.max_mark, "due_at": a.due_at, "allow_late": a.allow_late,
            "submitted": sub is not None, "submitted_at": sub.submitted_at if sub else None,
            "late": bool(sub.late) if sub else False,
            "status": sub.status if sub else "not_submitted",
            "mark": mark,
        })
    return out


# ------------------------------------------------------------------ faculty: submissions & grading


def _sub_out_full(s: AssignmentSubmission) -> AssignmentSubmissionOut:
    return asub_out(s)


@router.get("/assignments/{assignment_id}/submissions", response_model=list[AssignmentSubmissionOut])
def list_assignment_submissions(assignment_id: int, db: Session = Depends(get_db), user: User = Depends(faculty_user)):
    get_assignment(db, assignment_id, user)
    rows = (
        db.query(AssignmentSubmission)
        .options(joinedload(AssignmentSubmission.student))
        .filter(AssignmentSubmission.assignment_id == assignment_id)
        .all()
    )
    return [asub_out(s) for s in sorted(rows, key=lambda s: s.student.university_id)]


@router.get("/assignment-submissions/{submission_id}/file/{index}")
def get_assignment_file(submission_id: int, index: int, db: Session = Depends(get_db), user: User = Depends(faculty_user)):
    s = _get_asub(db, submission_id, user)
    if index < 0 or index >= len(s.file_paths or []):
        raise HTTPException(404, "File not found")
    return FileResponse(resolve(s.file_paths[index]))


@router.post("/assignments/{assignment_id}/grade", status_code=202)
def grade_assignment_all(assignment_id: int, background: BackgroundTasks, force: bool = False,
                         db: Session = Depends(get_db), user: User = Depends(faculty_user),
                         grader: AIGrader = Depends(get_grader)):
    a = get_assignment(db, assignment_id, user)
    try:
        ensure_assignment_gradable(a)
    except ValueError as e:
        raise HTTPException(409, str(e))
    _require_ai(grader)

    q = db.query(AssignmentSubmission).filter(
        AssignmentSubmission.assignment_id == assignment_id,
        AssignmentSubmission.status.notin_(["queued", "grading", "approved"]),
    )
    if not force:
        q = q.filter(AssignmentSubmission.status.in_(PENDING_STATES))
    subs = q.all()
    for s in subs:
        s.status = "queued"
    db.commit()
    ids = [s.id for s in subs]
    if ids:
        background.add_task(run_assignment_grading, ids, force=force, grader=grader)
    return {"queued": len(ids)}


@router.get("/assignments/{assignment_id}/grading-status")
def assignment_grading_status(assignment_id: int, db: Session = Depends(get_db), user: User = Depends(faculty_user)):
    get_assignment(db, assignment_id, user)
    rows = (
        db.query(AssignmentSubmission.status, func.count())
        .filter(AssignmentSubmission.assignment_id == assignment_id)
        .group_by(AssignmentSubmission.status)
        .all()
    )
    counts = {k: v for k, v in rows}
    total = sum(counts.values())
    busy = counts.get("queued", 0) + counts.get("grading", 0)
    return {"total": total, "counts": counts, "in_progress": busy > 0}


@router.post("/assignment-submissions/{submission_id}/grade", response_model=AssignmentSubmissionOut)
def grade_one_assignment(submission_id: int, force: bool = False, db: Session = Depends(get_db),
                         user: User = Depends(faculty_user), grader: AIGrader = Depends(get_grader)):
    s = _get_asub(db, submission_id, user)
    if s.status in ("queued", "grading"):
        raise HTTPException(409, "Submission is already being graded")
    _require_ai(grader)
    try:
        grade_assignment_submission(db, s, grader=grader, force=force)
    except ValueError as e:
        raise HTTPException(409, str(e))
    except GradingConflict as e:
        raise HTTPException(409, str(e))
    except AIError as e:
        raise HTTPException(502, str(e))
    return asub_out(_get_asub(db, submission_id, user))


@router.get("/assignment-submissions/{submission_id}/review", response_model=AssignmentReviewOut)
def review_assignment_submission(submission_id: int, db: Session = Depends(get_db), user: User = Depends(faculty_user)):
    return asub_review_out(_get_asub(db, submission_id, user))


def _editable(s: AssignmentSubmission) -> None:
    if s.status == "approved":
        raise HTTPException(409, "Submission is approved. Reopen it to edit the mark.")


@router.patch("/assignment-submissions/{submission_id}")
def set_assignment_final_mark(submission_id: int, body: GradePatch, db: Session = Depends(get_db),
                              user: User = Depends(faculty_user)):
    s = _get_asub(db, submission_id, user)
    _editable(s)
    mx = s.assignment.max_mark
    if body.final_mark < 0 or body.final_mark > mx:
        raise HTTPException(400, f"final_mark must be between 0 and {mx:g}")
    before = s.final_mark
    s.final_mark = round(body.final_mark, 4)
    s.reviewer_note = body.reviewer_note
    s.decision = "accepted_ai" if (s.ai_mark is not None and abs(s.ai_mark - s.final_mark) < 1e-6) else "edited"
    audit(db, user, "assignment_grade_set", "assignment_submission", s.id,
          {"ai_mark": s.ai_mark, "before": before, "after": s.final_mark, "decision": s.decision})
    db.commit()
    return asub_review_out(s)


@router.post("/assignment-submissions/{submission_id}/accept-ai")
def accept_assignment_ai_mark(submission_id: int, db: Session = Depends(get_db), user: User = Depends(faculty_user)):
    s = _get_asub(db, submission_id, user)
    _editable(s)
    if s.ai_mark is None:
        raise HTTPException(400, "The AI gave no mark for this submission; enter a mark manually")
    before = s.final_mark
    s.final_mark, s.decision = s.ai_mark, "accepted_ai"
    audit(db, user, "assignment_grade_set", "assignment_submission", s.id,
          {"ai_mark": s.ai_mark, "before": before, "after": s.final_mark, "decision": "accepted_ai"})
    db.commit()
    return asub_review_out(s)


@router.post("/assignment-submissions/{submission_id}/regrade")
def regrade_assignment(submission_id: int, force: bool = False, db: Session = Depends(get_db),
                       user: User = Depends(faculty_user), grader: AIGrader = Depends(get_grader)):
    s = _get_asub(db, submission_id, user)
    if s.status == "approved":
        raise HTTPException(409, "Submission is approved. Reopen it before regrading.")
    _require_ai(grader)
    try:
        grade_assignment_submission(db, s, grader=grader, force=True if force else (s.decision == "pending"))
    except GradingConflict as e:
        raise HTTPException(409, str(e))
    except AIError as e:
        raise HTTPException(502, str(e))
    audit(db, user, "assignment_regraded", "assignment_submission", s.id, {"force": force})
    db.commit()
    return asub_review_out(s)


def _approve_asub(s: AssignmentSubmission, source: str = "auto_approved") -> None:
    if s.status == "approved":
        return
    if s.status != "graded":
        raise HTTPException(409, f"Only graded submissions can be approved (status: {s.status})")
    if s.needs_review and s.decision == "pending":
        raise HTTPException(409, "Review the flagged submission before approving")
    if s.final_mark is None:
        if s.ai_mark is None:
            raise HTTPException(409, "No mark available")
        s.final_mark, s.decision = s.ai_mark, source
    s.status = "approved"
    s.approved_at = datetime.now(timezone.utc)


@router.post("/assignment-submissions/{submission_id}/approve", response_model=AssignmentSubmissionOut)
def approve_assignment_submission(submission_id: int, db: Session = Depends(get_db), user: User = Depends(faculty_user)):
    s = _get_asub(db, submission_id, user)
    _approve_asub(s)
    audit(db, user, "assignment_submission_approved", "assignment_submission", s.id, {"final_mark": s.final_mark})
    db.commit()
    return asub_out(_get_asub(db, submission_id, user))


@router.post("/assignment-submissions/{submission_id}/reopen", response_model=AssignmentSubmissionOut)
def reopen_assignment_submission(submission_id: int, db: Session = Depends(get_db), user: User = Depends(faculty_user)):
    s = _get_asub(db, submission_id, user)
    if s.status != "approved":
        raise HTTPException(409, "Submission is not approved")
    s.status, s.approved_at = "graded", None
    audit(db, user, "assignment_submission_reopened", "assignment_submission", s.id)
    db.commit()
    return asub_out(_get_asub(db, submission_id, user))


@router.post("/assignments/{assignment_id}/approve-unflagged")
def approve_unflagged_assignments(assignment_id: int, db: Session = Depends(get_db), user: User = Depends(faculty_user)):
    get_assignment(db, assignment_id, user)
    rows = db.query(AssignmentSubmission).filter(
        AssignmentSubmission.assignment_id == assignment_id, AssignmentSubmission.status == "graded"
    ).all()
    approved, held = 0, 0
    for s in rows:
        if s.needs_review and s.decision == "pending":
            held += 1
            continue
        try:
            _approve_asub(s, source="auto_approved")
            approved += 1
        except HTTPException:
            held += 1
    audit(db, user, "assignment_bulk_approved_unflagged", "assignment", assignment_id, {"approved": approved, "held": held})
    db.commit()
    return {"approved": approved, "held_for_review": held}


@router.delete("/assignment-submissions/{submission_id}", status_code=204)
def delete_assignment_submission(submission_id: int, db: Session = Depends(get_db), user: User = Depends(faculty_user)):
    s = _get_asub(db, submission_id, user)
    audit(db, user, "assignment_submission_deleted", "assignment_submission", s.id, {"status": s.status})
    db.delete(s)
    db.commit()


# ------------------------------------------------------------------ release (publish) marks


@router.post("/assignments/{assignment_id}/release", response_model=AssignmentOut)
def release_assignment_results(assignment_id: int, db: Session = Depends(get_db), user: User = Depends(faculty_user)):
    a = get_assignment(db, assignment_id, user)
    a.results_released_at = datetime.now(timezone.utc)
    visible = db.query(AssignmentSubmission.id).filter_by(assignment_id=a.id, status="approved").count()
    audit(db, user, "assignment_results_released", "assignment", a.id, {"visible_to_students": visible})
    db.commit()
    db.refresh(a)
    return assignment_out(a)


@router.post("/assignments/{assignment_id}/unrelease", response_model=AssignmentOut)
def unrelease_assignment_results(assignment_id: int, db: Session = Depends(get_db), user: User = Depends(faculty_user)):
    a = get_assignment(db, assignment_id, user)
    a.results_released_at = None
    audit(db, user, "assignment_results_unreleased", "assignment", a.id)
    db.commit()
    db.refresh(a)
    return assignment_out(a)
