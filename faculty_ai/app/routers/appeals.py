"""Review requests (appeals): student files -> instructor (optionally with an AI second opinion) decides."""
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session, selectinload

from ..database import get_db
from ..models import (
    Course, CourseEnrollment, Exam, Grade, Question, ReviewRequest, ReviewRequestItem, Submission, User,
)
from ..schemas import DecideIn, RejectIn, ReviewRequestIn
from ..security import check_course_access, faculty_user, student_user
from ..services.ai_grader import AIError, AIGrader, GradingConflict, get_grader, run_second_opinion
from ..services.audit import audit
from ..services.notifications import notify
from ..services.storage import resolve
from .grading import _require_ai

student_router = APIRouter(prefix="/me", tags=["student: review requests"])
router = APIRouter(prefix="/review-requests", tags=["review requests"])

TOL = 1e-6


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def appeal_window_open(exam: Exam) -> bool:
    d = _aware(exam.appeals_deadline)
    return d is not None and d > datetime.now(timezone.utc)


# ================================================================== student side


def _student_view(req: ReviewRequest) -> dict:
    """What the STUDENT may see: their own request and the instructor's decision. Never AI output."""
    decided = req.status != "pending"
    return {
        "id": req.id, "exam_id": req.exam_id, "exam_title": req.exam.title,
        "course": f"{req.exam.course.code or ''} {req.exam.course.name}".strip(),
        "status": req.status, "message": req.message, "created_at": req.created_at,
        "decided_at": req.decided_at, "response_message": req.response_message if decided else None,
        "paper_access": bool(req.paper_access and req.status == "decided"),
        "items": [{"number": i.question.number, "max_mark": i.question.max_mark, "original_mark": i.original_mark,
                   "decision": i.decision, "new_mark": i.new_mark if decided else None,
                   "note": i.note if decided else None} for i in sorted(req.items, key=lambda x: x.question.number)],
    }


@student_router.post("/exams/{exam_id}/review-requests", status_code=201)
def create_request(exam_id: int, body: ReviewRequestIn, db: Session = Depends(get_db),
                   user: User = Depends(student_user)):
    exam = db.get(Exam, exam_id)
    enrolled = exam and db.query(CourseEnrollment.id).filter_by(course_id=exam.course_id, student_id=user.student_id).first()
    if not exam or not enrolled or exam.grades_published_at is None:
        raise HTTPException(404, "Not found")
    sub = (db.query(Submission).options(selectinload(Submission.grades))
           .filter(Submission.exam_id == exam_id, Submission.student_id == user.student_id,
                   Submission.status == "approved").first())
    if sub is None:
        raise HTTPException(409, "Your result for this exam is not available yet")
    if not appeal_window_open(exam):
        raise HTTPException(409, "The review window for this exam is closed")
    if db.query(ReviewRequest.id).filter_by(submission_id=sub.id).first():
        raise HTTPException(409, "You have already submitted a review request for this exam")

    numbers = sorted(set(body.question_numbers))
    qs = {q.number: q for q in exam.questions}
    unknown = [n for n in numbers if n not in qs]
    if unknown:
        raise HTTPException(400, f"Unknown question number(s): {unknown}")
    by_q = {g.question_id: g for g in sub.grades}

    req = ReviewRequest(submission_id=sub.id, student_id=user.student_id, exam_id=exam.id, message=body.message.strip())
    for n in numbers:
        g = by_q.get(qs[n].id)
        req.items.append(ReviewRequestItem(question_id=qs[n].id, original_mark=g.final_mark if g else None))
    db.add(req)
    db.flush()
    notify(db, exam.course.instructor_id, "review_requested", f"New review request: {exam.title}",
           f"{user.name} ({user.university_id}) asked for a review of question(s) {numbers}.", "review_request", req.id)
    audit(db, user, "review_requested", "review_request", req.id, {"exam_id": exam.id, "questions": numbers})
    db.commit()
    return _student_view(req)


@student_router.get("/review-requests")
def my_requests(db: Session = Depends(get_db), user: User = Depends(student_user)):
    rows = (db.query(ReviewRequest).filter(ReviewRequest.student_id == user.student_id)
            .order_by(ReviewRequest.created_at.desc()).all())
    return [_student_view(r) for r in rows]


def _own_decided_with_access(db: Session, user: User, request_id: int) -> ReviewRequest:
    req = db.query(ReviewRequest).filter(ReviewRequest.id == request_id,
                                         ReviewRequest.student_id == user.student_id).first()
    if req is None:
        raise HTTPException(404, "Not found")
    if req.status != "decided" or not req.paper_access:
        raise HTTPException(403, "Your instructor has not released this paper for viewing")
    return req


@student_router.get("/review-requests/{request_id}/paper")
def my_paper(request_id: int, db: Session = Depends(get_db), user: User = Depends(student_user)):
    """The student's OWN paper + final marks, once the instructor has granted access. No AI content."""
    req = _own_decided_with_access(db, user, request_id)
    sub = req.submission
    by_q = {g.question_id: g for g in sub.grades}
    qs = [{"number": q.number, "text": q.text, "max_mark": q.max_mark,
           "mark": by_q[q.id].final_mark if q.id in by_q else None} for q in req.exam.questions]
    return {"exam_title": req.exam.title, "n_files": len(sub.file_paths or []), "questions": qs,
            "total": round(sum(q["mark"] or 0 for q in qs), 4), "total_max": req.exam.total_marks}


@student_router.get("/review-requests/{request_id}/file/{index}")
def my_paper_file(request_id: int, index: int, db: Session = Depends(get_db), user: User = Depends(student_user)):
    req = _own_decided_with_access(db, user, request_id)
    paths = req.submission.file_paths or []
    if index < 0 or index >= len(paths):
        raise HTTPException(404, "Not found")
    audit(db, user, "own_paper_viewed", "review_request", req.id, {"file": index})
    db.commit()
    return FileResponse(resolve(paths[index]))


# ================================================================== faculty side


def get_request(db: Session, request_id: int, user: User) -> ReviewRequest:
    req = db.get(ReviewRequest, request_id)
    if req is None:
        raise HTTPException(404, "Review request not found")
    check_course_access(req.exam.course, user)
    return req


def _detail(db: Session, req: ReviewRequest) -> dict:
    grades = {g.question_id: g for g in db.query(Grade).filter(Grade.submission_id == req.submission_id).all()}
    items = []
    for it in sorted(req.items, key=lambda x: x.question.number):
        q, g = it.question, grades.get(it.question_id)
        items.append({
            "question_id": q.id, "number": q.number, "text": q.text, "max_mark": q.max_mark, "clo": q.clo,
            "rubric": q.rubric_json, "original_mark": it.original_mark,
            "current_mark": g.final_mark if g else None,
            "student_answer": g.ai_answer_text if g else "", "original_breakdown": g.ai_rubric_breakdown if g else [],
            "original_reason": g.ai_reason if g else "",
            "second_opinion": None if it.ai_ran_at is None else {
                "mark": it.ai_mark, "confidence": it.ai_confidence, "reason": it.ai_reason,
                "breakdown": it.ai_breakdown, "flags": it.ai_flags,
                "difference": None if it.ai_mark is None or it.original_mark is None else round(it.ai_mark - it.original_mark, 4)},
            "decision": it.decision, "new_mark": it.new_mark, "note": it.note,
        })
    ex = req.exam
    return {"id": req.id, "status": req.status, "created_at": req.created_at, "message": req.message,
            "exam": {"id": ex.id, "title": ex.title}, "course": {"id": ex.course.id, "name": ex.course.name, "code": ex.course.code},
            "student": {"name": req.student.name, "university_id": req.student.university_id},
            "submission_id": req.submission_id, "n_files": len(req.submission.file_paths or []),
            "paper_access": req.paper_access, "response_message": req.response_message,
            "decided_at": req.decided_at, "items": items}


@router.get("")
def list_requests(status: Optional[str] = None, db: Session = Depends(get_db), user: User = Depends(faculty_user)):
    q = db.query(ReviewRequest).join(Exam, ReviewRequest.exam_id == Exam.id).join(Course, Exam.course_id == Course.id)
    if user.role != "admin":
        q = q.filter(Course.instructor_id == user.id)
    if status:
        q = q.filter(ReviewRequest.status == status)
    rows = q.order_by(ReviewRequest.created_at.desc()).all()
    rows.sort(key=lambda r: r.status != "pending")  # pending first (stable)
    return [{"id": r.id, "status": r.status, "created_at": r.created_at, "exam": r.exam.title,
             "course": f"{r.exam.course.code or ''} {r.exam.course.name}".strip(),
             "student": r.student.name, "university_id": r.student.university_id,
             "questions": sorted(i.question.number for i in r.items)} for r in rows]


@router.get("/{request_id}")
def read_request(request_id: int, db: Session = Depends(get_db), user: User = Depends(faculty_user)):
    return _detail(db, get_request(db, request_id, user))


@router.post("/{request_id}/ai-assist")
def ai_assist(request_id: int, db: Session = Depends(get_db), user: User = Depends(faculty_user),
              grader: AIGrader = Depends(get_grader)):
    """Independent AI second opinion on the requested questions. Advisory only: no mark changes."""
    req = get_request(db, request_id, user)
    if req.status != "pending":
        raise HTTPException(409, "This request has already been decided")
    _require_ai(grader)
    try:
        run_second_opinion(db, req, grader=grader)
    except (AIError, GradingConflict) as e:
        raise HTTPException(502, str(e))
    audit(db, user, "review_ai_assist", "review_request", req.id)
    db.commit()
    return _detail(db, req)


@router.post("/{request_id}/decide")
def decide(request_id: int, body: DecideIn, db: Session = Depends(get_db), user: User = Depends(faculty_user)):
    req = get_request(db, request_id, user)
    if req.status != "pending":
        raise HTTPException(409, "This request has already been decided")
    by_qid = {i.question_id: i for i in req.items}
    if {d.question_id for d in body.items} != set(by_qid) or len(body.items) != len(by_qid):
        raise HTTPException(400, "Decide every requested question exactly once")
    grades = {g.question_id: g for g in db.query(Grade).filter(Grade.submission_id == req.submission_id).all()}

    # validate first, then apply (all or nothing)
    plan = []
    for d in body.items:
        it, g = by_qid[d.question_id], grades.get(d.question_id)
        mx = it.question.max_mark
        if g is None:
            raise HTTPException(409, f"Question {it.question.number} has no grade")
        if d.decision == "change":
            if d.new_mark is None or d.new_mark < 0 or d.new_mark > mx:
                raise HTTPException(400, f"Question {it.question.number}: new_mark must be between 0 and {mx:g}")
            if g.final_mark is not None and abs(d.new_mark - g.final_mark) < TOL:
                raise HTTPException(400, f"Question {it.question.number}: new mark equals the current mark; choose 'keep'")
        plan.append((d, it, g))

    changed, kept, details = 0, 0, []
    for d, it, g in plan:
        it.note = d.note
        if d.decision == "change":
            before = g.final_mark
            g.final_mark = round(d.new_mark, 4)
            g.decision = "edited"
            tag = f"Review request #{req.id}"
            g.reviewer_note = f"{g.reviewer_note} | {tag}" if g.reviewer_note else tag
            it.decision, it.new_mark = "changed", g.final_mark
            changed += 1
            details.append({"question": it.question.number, "before": before, "after": g.final_mark,
                            "ai_second_opinion": it.ai_mark})
        else:
            it.decision, it.new_mark = "kept", None
            kept += 1
            details.append({"question": it.question.number, "kept": g.final_mark, "ai_second_opinion": it.ai_mark})
    req.status = "decided"
    req.response_message = body.response_message.strip()
    req.decided_by_id, req.decided_at = user.id, datetime.now(timezone.utc)
    req.paper_access = body.grant_paper_access
    audit(db, user, "review_decided", "review_request", req.id,
          {"changed": changed, "kept": kept, "paper_access": req.paper_access, "items": details})
    student_user_row = db.query(User).filter(User.student_id == req.student_id, User.role == "student").first()
    if student_user_row:
        notify(db, student_user_row.id, "review_decided", f"Your review request was decided: {req.exam.title}",
               f"{changed} mark(s) changed, {kept} kept." + (" You can now view your paper." if req.paper_access else ""),
               "review_request", req.id)
    db.commit()
    return _detail(db, req)


@router.post("/{request_id}/reject")
def reject(request_id: int, body: RejectIn, db: Session = Depends(get_db), user: User = Depends(faculty_user)):
    req = get_request(db, request_id, user)
    if req.status != "pending":
        raise HTTPException(409, "This request has already been decided")
    req.status, req.response_message = "rejected", body.response_message.strip()
    req.decided_by_id, req.decided_at, req.paper_access = user.id, datetime.now(timezone.utc), False
    audit(db, user, "review_rejected", "review_request", req.id)
    acct = db.query(User).filter(User.student_id == req.student_id, User.role == "student").first()
    if acct:
        notify(db, acct.id, "review_rejected", f"Your review request was not accepted: {req.exam.title}",
               "See your instructor's response.", "review_request", req.id)
    db.commit()
    return _detail(db, req)


@router.post("/{request_id}/paper-access")
def paper_access(request_id: int, grant: bool, db: Session = Depends(get_db), user: User = Depends(faculty_user)):
    """Grant or revoke the student's permission to view their own paper (only after a decision)."""
    req = get_request(db, request_id, user)
    if req.status != "decided":
        raise HTTPException(409, "Decide the request first")
    if req.paper_access != grant:
        req.paper_access = grant
        audit(db, user, "paper_access_granted" if grant else "paper_access_revoked", "review_request", req.id)
        if grant:
            acct = db.query(User).filter(User.student_id == req.student_id, User.role == "student").first()
            if acct:
                notify(db, acct.id, "paper_access_granted", f"You can now view your paper: {req.exam.title}", "",
                       "review_request", req.id)
        db.commit()
    return {"paper_access": req.paper_access}
