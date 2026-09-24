"""Student portal API. A student can only ever see their OWN, faculty-APPROVED, PUBLISHED final marks."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session, selectinload

from ..database import get_db
from ..models import CourseEnrollment, Exam, ReviewRequest, Submission, User
from ..security import student_user
from .appeals import appeal_window_open, _aware

router = APIRouter(prefix="/me", tags=["student"])


def _enrollment(db: Session, user: User, course_id: int) -> CourseEnrollment:
    e = (
        db.query(CourseEnrollment)
        .filter(CourseEnrollment.course_id == course_id, CourseEnrollment.student_id == user.student_id)
        .first()
    )
    if e is None:
        raise HTTPException(404, "Not found")
    return e


@router.get("/courses")
def my_courses(db: Session = Depends(get_db), user: User = Depends(student_user)):
    rows = db.query(CourseEnrollment).filter(CourseEnrollment.student_id == user.student_id).all()
    return [{"id": e.course.id, "name": e.course.name, "code": e.course.code, "section": e.section,
             "instructor": e.course.instructor.name} for e in rows]


@router.get("/courses/{course_id}/grades")
def my_grades(course_id: int, db: Session = Depends(get_db), user: User = Depends(student_user)):
    _enrollment(db, user, course_id)
    exams = (
        db.query(Exam)
        .filter(Exam.course_id == course_id, Exam.grades_published_at.isnot(None))
        .order_by(Exam.created_at)
        .all()
    )
    out = []
    for ex in exams:
        sub = (
            db.query(Submission)
            .options(selectinload(Submission.grades))
            .filter(Submission.exam_id == ex.id, Submission.student_id == user.student_id, Submission.status == "approved")
            .first()
        )
        if sub is None:
            continue  # not approved yet -> invisible (never show AI marks)
        by_q = {g.question_id: g for g in sub.grades}
        qs = [{"number": q.number, "max_mark": q.max_mark, "mark": by_q[q.id].final_mark if q.id in by_q else None}
              for q in ex.questions]
        total = round(sum(q["mark"] or 0 for q in qs), 4)
        total_max = ex.total_marks
        rr = db.query(ReviewRequest).filter(ReviewRequest.submission_id == sub.id).first()
        out.append({"exam_id": ex.id, "title": ex.title, "published_at": ex.grades_published_at, "questions": qs,
                    "total": total, "total_max": total_max,
                    "percent": round(total / total_max * 100, 2) if total_max else None,
                    "review": {"can_request": rr is None and appeal_window_open(ex),
                               "window_closes_at": _aware(ex.appeals_deadline) if appeal_window_open(ex) else None,
                               "request_id": rr.id if rr else None, "request_status": rr.status if rr else None}})
    return out
