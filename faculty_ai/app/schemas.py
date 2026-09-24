"""Pydantic schemas: API payloads + the strict structure we require from the AI."""
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

# --------------------------------------------------------------------------- AI output


class RubricItemResult(BaseModel):
    criterion: str
    mark: float
    max_mark: float


class QuestionResult(BaseModel):
    """One graded question exactly as the model must return it."""

    question_number: int
    student_answer_transcription: str = ""
    answer_status: Literal["answered", "partial", "blank", "unclear"] = "answered"
    mark: float
    max_mark: float
    reason: str = ""
    confidence: float = 0.0
    needs_review: bool = False
    rubric_breakdown: list[RubricItemResult] = Field(default_factory=list)

    @field_validator("confidence", mode="before")
    @classmethod
    def _clamp_confidence(cls, v):
        try:
            return min(1.0, max(0.0, float(v)))
        except (TypeError, ValueError):
            return 0.0


class PaperResult(BaseModel):
    questions: list[QuestionResult]


# --------------------------------------------------------------------------- courses


class CLOItem(BaseModel):
    id: str
    description: str = ""


class CourseIn(BaseModel):
    name: str = Field(min_length=1)
    code: Optional[str] = None
    clos: list[CLOItem] = Field(default_factory=list)


class CourseOut(CourseIn):
    model_config = ConfigDict(from_attributes=True)
    id: int
    created_at: datetime


# --------------------------------------------------------------------------- exams / questions


class RubricCriterion(BaseModel):
    criterion: str = Field(min_length=1)
    max_mark: float = Field(gt=0)
    description: str = ""


class RubricIn(BaseModel):
    criteria: list[RubricCriterion] = Field(min_length=1)


class QuestionIn(BaseModel):
    number: int = Field(ge=1)
    text: str = ""
    max_mark: float = Field(gt=0)
    clo: Optional[str] = None
    model_answer: str = ""
    rubric: Optional[RubricIn] = None


class QuestionUpdate(BaseModel):
    text: Optional[str] = None
    max_mark: Optional[float] = Field(default=None, gt=0)
    clo: Optional[str] = None
    model_answer: Optional[str] = None


class QuestionOut(BaseModel):
    id: int
    exam_id: int
    number: int
    text: str
    max_mark: float
    clo: Optional[str]
    model_answer: str
    rubric: Optional[dict]
    rubric_approved: bool


def question_out(q) -> QuestionOut:
    return QuestionOut(
        id=q.id,
        exam_id=q.exam_id,
        number=q.number,
        text=q.text or "",
        max_mark=q.max_mark,
        clo=q.clo,
        model_answer=q.model_answer or "",
        rubric=q.rubric_json,
        rubric_approved=bool(q.rubric_approved),
    )


class ExamOut(BaseModel):
    id: int
    course_id: int
    title: str
    total_marks: float
    has_exam_file: bool
    has_model_answer_file: bool
    question_count: int
    rubrics_approved: bool
    published: bool
    appeals_open_until: Optional[datetime] = None
    created_at: datetime


def exam_out(e) -> ExamOut:
    qs = list(e.questions)
    return ExamOut(
        id=e.id,
        course_id=e.course_id,
        title=e.title,
        total_marks=e.total_marks,
        has_exam_file=bool(e.exam_file_path),
        has_model_answer_file=bool(e.model_answer_path),
        question_count=len(qs),
        rubrics_approved=bool(qs) and all(q.rubric_approved for q in qs),
        published=bool(e.grades_published_at),
        appeals_open_until=e.appeals_deadline,
        created_at=e.created_at,
    )


# --------------------------------------------------------------------------- submissions / grading


class SubmissionOut(BaseModel):
    id: int
    exam_id: int
    student_id: int
    student_name: str
    university_id: str
    status: str
    error: Optional[str]
    n_files: int
    n_graded: int
    n_pending_review: int
    total: Optional[float]


class GradeRunRequest(BaseModel):
    submission_ids: Optional[list[int]] = None
    force: bool = False


class GradePatch(BaseModel):
    final_mark: float
    reviewer_note: Optional[str] = None


class GradeOut(BaseModel):
    id: int
    question_id: int
    question_number: int
    question_text: str
    clo: Optional[str]
    max_mark: float
    rubric: Optional[dict]
    ai_mark: Optional[float]
    ai_reason: str
    ai_confidence: Optional[float]
    ai_needs_review: bool
    ai_rubric_breakdown: list
    ai_answer_text: str
    answer_status: str
    review_flags: list
    needs_review: bool
    final_mark: Optional[float]
    decision: str
    reviewer_note: Optional[str]


def grade_out(g) -> GradeOut:
    q = g.question
    return GradeOut(
        id=g.id,
        question_id=g.question_id,
        question_number=q.number,
        question_text=q.text or "",
        clo=q.clo,
        max_mark=q.max_mark,
        rubric=q.rubric_json,
        ai_mark=g.ai_mark,
        ai_reason=g.ai_reason or "",
        ai_confidence=g.ai_confidence,
        ai_needs_review=bool(g.ai_needs_review),
        ai_rubric_breakdown=g.ai_rubric_breakdown or [],
        ai_answer_text=g.ai_answer_text or "",
        answer_status=g.answer_status,
        review_flags=g.review_flags or [],
        needs_review=bool(g.needs_review),
        final_mark=g.final_mark,
        decision=g.decision,
        reviewer_note=g.reviewer_note,
    )


class ReviewOut(BaseModel):
    submission_id: int
    exam_id: int
    student_name: str
    university_id: str
    status: str
    n_files: int
    total_max: float
    total_current: Optional[float]
    grades: list[GradeOut]


# --------------------------------------------------------------------------- review requests


class ReviewRequestIn(BaseModel):
    question_numbers: list[int] = Field(min_length=1, max_length=50)
    message: str = Field(min_length=10, max_length=2000)


class ItemDecision(BaseModel):
    question_id: int
    decision: Literal["keep", "change"]
    new_mark: Optional[float] = None
    note: Optional[str] = Field(default=None, max_length=1000)


class DecideIn(BaseModel):
    items: list[ItemDecision] = Field(min_length=1)
    response_message: str = Field(min_length=1, max_length=2000)  # the student always gets an explanation
    grant_paper_access: bool = False


class RejectIn(BaseModel):
    response_message: str = Field(min_length=1, max_length=2000)


# --------------------------------------------------------------------------- assignments


class AssignmentIn(BaseModel):
    title: str = Field(min_length=1)
    description: str = ""
    max_mark: float = Field(gt=0)
    model_answer: str = ""
    due_at: Optional[datetime] = None
    allow_late: bool = False
    rubric: Optional[RubricIn] = None


class AssignmentUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    max_mark: Optional[float] = Field(default=None, gt=0)
    model_answer: Optional[str] = None
    due_at: Optional[datetime] = None
    allow_late: Optional[bool] = None


class AssignmentOut(BaseModel):
    id: int
    course_id: int
    title: str
    description: str
    max_mark: float
    model_answer: str
    rubric: Optional[dict]
    rubric_approved: bool
    due_at: Optional[datetime]
    allow_late: bool
    published: bool
    created_at: datetime


def assignment_out(a) -> AssignmentOut:
    return AssignmentOut(
        id=a.id, course_id=a.course_id, title=a.title, description=a.description or "",
        max_mark=a.max_mark, model_answer=a.model_answer or "", rubric=a.rubric_json,
        rubric_approved=bool(a.rubric_approved), due_at=a.due_at, allow_late=bool(a.allow_late),
        published=bool(a.results_released_at), created_at=a.created_at,
    )


class AssignmentSubmissionOut(BaseModel):
    id: int
    assignment_id: int
    student_id: int
    student_name: str
    university_id: str
    status: str
    error: Optional[str]
    late: bool
    submitted_at: Optional[datetime]
    needs_review: bool
    final_mark: Optional[float]
    ai_mark: Optional[float]


def asub_out(s) -> AssignmentSubmissionOut:
    return AssignmentSubmissionOut(
        id=s.id, assignment_id=s.assignment_id, student_id=s.student_id, student_name=s.student.name,
        university_id=s.student.university_id, status=s.status, error=s.error, late=bool(s.late),
        submitted_at=s.submitted_at, needs_review=bool(s.needs_review) and s.decision == "pending",
        final_mark=s.final_mark, ai_mark=s.ai_mark,
    )


class AssignmentReviewOut(BaseModel):
    submission_id: int
    assignment_id: int
    student_name: str
    university_id: str
    status: str
    n_files: int
    max_mark: float
    rubric: Optional[dict]
    ai_mark: Optional[float]
    ai_reason: str
    ai_confidence: Optional[float]
    ai_needs_review: bool
    ai_rubric_breakdown: list
    ai_answer_text: str
    answer_status: str
    review_flags: list
    needs_review: bool
    final_mark: Optional[float]
    decision: str
    reviewer_note: Optional[str]


def asub_review_out(s) -> AssignmentReviewOut:
    a = s.assignment
    return AssignmentReviewOut(
        submission_id=s.id, assignment_id=s.assignment_id, student_name=s.student.name,
        university_id=s.student.university_id, status=s.status, n_files=len(s.file_paths or []),
        max_mark=a.max_mark, rubric=a.rubric_json, ai_mark=s.ai_mark, ai_reason=s.ai_reason or "",
        ai_confidence=s.ai_confidence, ai_needs_review=bool(s.ai_needs_review),
        ai_rubric_breakdown=s.ai_rubric_breakdown or [], ai_answer_text=s.ai_answer_text or "",
        answer_status=s.answer_status, review_flags=s.review_flags or [], needs_review=bool(s.needs_review),
        final_mark=s.final_mark, decision=s.decision, reviewer_note=s.reviewer_note,
    )


# --------------------------------------------------------------------------- grade scale (per course)


class LetterBand(BaseModel):
    letter: str = Field(min_length=1, max_length=10)
    min_percent: float = Field(ge=0, le=100)


class LetterScaleIn(BaseModel):
    """Either a ready-made preset name, or a fully custom list of bands. Exactly one of the two."""
    preset: Optional[Literal["scale_100", "scale_nbu5"]] = None
    bands: Optional[list[LetterBand]] = None


# --------------------------------------------------------------------------- weighted course gradebook


class GradeCategoryIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    weight_pct: float = Field(gt=0, le=100)
    order: int = 0
    drop_lowest: int = Field(default=0, ge=0)


class GradeCategoryUpdate(BaseModel):
    name: Optional[str] = None
    weight_pct: Optional[float] = Field(default=None, gt=0, le=100)
    order: Optional[int] = None
    drop_lowest: Optional[int] = Field(default=None, ge=0)


class GradeCategoryItemIn(BaseModel):
    entity_type: Literal["exam", "assignment"]
    entity_id: int


class GradeCategoryItemOut(BaseModel):
    id: int
    entity_type: str
    entity_id: int
    title: str
    max_mark: float
    published: bool


class GradeCategoryOut(BaseModel):
    id: int
    course_id: int
    name: str
    weight_pct: float
    order: int
    drop_lowest: int
    items: list[GradeCategoryItemOut]


# --------------------------------------------------------------------------- self-service auth


class RegisterFacultyIn(BaseModel):
    university_id: str = Field(min_length=1, max_length=50)
    name: str = Field(min_length=1, max_length=200)
    email: EmailStr
    password: str = Field(min_length=8, max_length=200)


class ForgotPasswordIn(BaseModel):
    university_id: Optional[str] = None
    email: Optional[EmailStr] = None


class ResetPasswordIn(BaseModel):
    token: str = Field(min_length=10)
    new_password: str = Field(min_length=8, max_length=200)
