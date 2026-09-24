"""Database models.

Design notes
- Every core table has created_at / updated_at.
- `grades` keeps the AI's output (ai_*) and the instructor's decision (final_mark,
  decision, reviewer_note) in separate columns so every change is auditable.
- `ai_raw_response` (per grade) and `ai_call_logs` (per API call) let us trace
  exactly why the AI produced a mark.
"""
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class User(TimestampMixin, Base):
    """Login account. Roles: student | faculty | admin. Students link to their roster record via student_id."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    university_id: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    email: Mapped[Optional[str]] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(20), index=True)
    password_hash: Mapped[str] = mapped_column(Text)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    student_id: Mapped[Optional[int]] = mapped_column(ForeignKey("students.id", ondelete="SET NULL"))
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class PasswordResetToken(Base):
    """One-time, short-lived token for self-service password reset. The token itself is emailed to
    the user and never stored in plain text — only its sha256 hash, so a leaked database row can't
    be used to reset anyone's password."""

    __tablename__ = "password_reset_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class Course(TimestampMixin, Base):
    __tablename__ = "courses"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    code: Mapped[Optional[str]] = mapped_column(String(50))
    clos: Mapped[list] = mapped_column(JSON, default=list)  # [{"id": "CLO1", "description": "..."}]
    # [{"letter": "A", "min_percent": 90}, ...] sorted high->low; seeded on create, editable per course
    grade_scale: Mapped[list] = mapped_column(JSON, default=list)
    instructor_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)

    instructor: Mapped["User"] = relationship()
    exams: Mapped[list["Exam"]] = relationship(back_populates="course", cascade="all, delete-orphan")
    enrollments: Mapped[list["CourseEnrollment"]] = relationship(
        back_populates="course", cascade="all, delete-orphan"
    )


class CourseEnrollment(TimestampMixin, Base):
    __tablename__ = "course_enrollments"
    __table_args__ = (UniqueConstraint("course_id", "student_id", name="uq_enrollment_course_student"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id", ondelete="CASCADE"), index=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id", ondelete="CASCADE"), index=True)
    section: Mapped[Optional[str]] = mapped_column(String(20))

    course: Mapped["Course"] = relationship(back_populates="enrollments")
    student: Mapped["Student"] = relationship()


class Exam(TimestampMixin, Base):
    __tablename__ = "exams"

    id: Mapped[int] = mapped_column(primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    grades_published_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))  # students see nothing before this
    appeals_deadline: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))  # students may request a review until then
    exam_file_path: Mapped[Optional[str]] = mapped_column(String(500))
    model_answer_path: Mapped[Optional[str]] = mapped_column(String(500))

    course: Mapped["Course"] = relationship(back_populates="exams")
    questions: Mapped[list["Question"]] = relationship(
        back_populates="exam", cascade="all, delete-orphan", order_by="Question.number"
    )
    submissions: Mapped[list["Submission"]] = relationship(back_populates="exam", cascade="all, delete-orphan")

    @property
    def total_marks(self) -> float:
        return round(sum(q.max_mark for q in self.questions), 4)


class Question(TimestampMixin, Base):
    __tablename__ = "questions"
    __table_args__ = (UniqueConstraint("exam_id", "number", name="uq_question_exam_number"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    exam_id: Mapped[int] = mapped_column(ForeignKey("exams.id", ondelete="CASCADE"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text, default="")
    max_mark: Mapped[float] = mapped_column(Float)
    clo: Mapped[Optional[str]] = mapped_column(String(50))
    model_answer: Mapped[str] = mapped_column(Text, default="")
    # {"criteria": [{"criterion": str, "max_mark": float, "description": str}]}
    rubric_json: Mapped[Optional[dict]] = mapped_column(JSON)
    rubric_approved: Mapped[bool] = mapped_column(Boolean, default=False)

    exam: Mapped["Exam"] = relationship(back_populates="questions")


class Student(TimestampMixin, Base):
    __tablename__ = "students"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    university_id: Mapped[str] = mapped_column(String(50), unique=True, index=True)


class Submission(TimestampMixin, Base):
    __tablename__ = "submissions"
    __table_args__ = (UniqueConstraint("exam_id", "student_id", name="uq_submission_exam_student"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    exam_id: Mapped[int] = mapped_column(ForeignKey("exams.id", ondelete="CASCADE"), index=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id", ondelete="CASCADE"), index=True)
    file_paths: Mapped[list] = mapped_column(JSON, default=list)  # one or more pages/photos
    # uploaded -> queued -> grading -> graded -> approved   (or failed)
    status: Mapped[str] = mapped_column(String(20), default="uploaded", index=True)
    error: Mapped[Optional[str]] = mapped_column(Text)
    graded_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    exam: Mapped["Exam"] = relationship(back_populates="submissions")
    student: Mapped["Student"] = relationship()
    grades: Mapped[list["Grade"]] = relationship(
        back_populates="submission", cascade="all, delete-orphan", order_by="Grade.question_id"
    )


class Grade(TimestampMixin, Base):
    __tablename__ = "grades"
    __table_args__ = (UniqueConstraint("submission_id", "question_id", name="uq_grade_submission_question"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    submission_id: Mapped[int] = mapped_column(ForeignKey("submissions.id", ondelete="CASCADE"), index=True)
    question_id: Mapped[int] = mapped_column(ForeignKey("questions.id", ondelete="CASCADE"), index=True)

    # ---- AI output (never overwritten by the instructor) ----
    ai_mark: Mapped[Optional[float]] = mapped_column(Float)
    ai_reason: Mapped[str] = mapped_column(Text, default="")
    ai_confidence: Mapped[Optional[float]] = mapped_column(Float)
    ai_needs_review: Mapped[bool] = mapped_column(Boolean, default=False)  # the model's own flag
    ai_rubric_breakdown: Mapped[list] = mapped_column(JSON, default=list)
    ai_answer_text: Mapped[str] = mapped_column(Text, default="")  # transcription of the student's answer
    answer_status: Mapped[str] = mapped_column(String(20), default="answered")
    ai_raw_response: Mapped[Optional[str]] = mapped_column(Text)  # exact per-question JSON from the model
    ai_model: Mapped[Optional[str]] = mapped_column(String(100))

    # ---- Rule-based review decision (system) ----
    review_flags: Mapped[list] = mapped_column(JSON, default=list)
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    # ---- Instructor decision ----
    final_mark: Mapped[Optional[float]] = mapped_column(Float)
    # pending | accepted_ai | edited | auto_approved
    decision: Mapped[str] = mapped_column(String(20), default="pending")
    reviewer_note: Mapped[Optional[str]] = mapped_column(Text)

    submission: Mapped["Submission"] = relationship(back_populates="grades")
    question: Mapped["Question"] = relationship()

    @property
    def reviewed(self) -> bool:
        return self.decision != "pending"


class Assignment(TimestampMixin, Base):
    """Lighter-weight sibling of Exam for ongoing coursework: one implicit "question" per assignment."""

    __tablename__ = "assignments"

    id: Mapped[int] = mapped_column(primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    max_mark: Mapped[float] = mapped_column(Float)
    model_answer: Mapped[str] = mapped_column(Text, default="")
    rubric_json: Mapped[Optional[dict]] = mapped_column(JSON)
    rubric_approved: Mapped[bool] = mapped_column(Boolean, default=False)
    due_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    allow_late: Mapped[bool] = mapped_column(Boolean, default=False)
    results_released_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))  # students see nothing before this

    course: Mapped["Course"] = relationship()
    submissions: Mapped[list["AssignmentSubmission"]] = relationship(
        back_populates="assignment", cascade="all, delete-orphan"
    )


class AssignmentSubmission(TimestampMixin, Base):
    """Combines what Submission+Grade are for exams into one row, since an assignment has one
    implicit question. Same AI-vs-instructor column separation and review-flag discipline as grades."""

    __tablename__ = "assignment_submissions"
    __table_args__ = (UniqueConstraint("assignment_id", "student_id", name="uq_asub_assignment_student"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    assignment_id: Mapped[int] = mapped_column(ForeignKey("assignments.id", ondelete="CASCADE"), index=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id", ondelete="CASCADE"), index=True)
    file_paths: Mapped[list] = mapped_column(JSON, default=list)
    submitted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    late: Mapped[bool] = mapped_column(Boolean, default=False)
    # uploaded -> queued -> grading -> graded -> approved (or failed)
    status: Mapped[str] = mapped_column(String(20), default="uploaded", index=True)
    error: Mapped[Optional[str]] = mapped_column(Text)
    graded_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    # ---- AI output (never overwritten by the instructor) ----
    ai_mark: Mapped[Optional[float]] = mapped_column(Float)
    ai_reason: Mapped[str] = mapped_column(Text, default="")
    ai_confidence: Mapped[Optional[float]] = mapped_column(Float)
    ai_needs_review: Mapped[bool] = mapped_column(Boolean, default=False)
    ai_rubric_breakdown: Mapped[list] = mapped_column(JSON, default=list)
    ai_answer_text: Mapped[str] = mapped_column(Text, default="")
    answer_status: Mapped[str] = mapped_column(String(20), default="answered")
    ai_raw_response: Mapped[Optional[str]] = mapped_column(Text)
    ai_model: Mapped[Optional[str]] = mapped_column(String(100))

    # ---- Rule-based review decision (system) ----
    review_flags: Mapped[list] = mapped_column(JSON, default=list)
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    # ---- Instructor decision ----
    final_mark: Mapped[Optional[float]] = mapped_column(Float)
    decision: Mapped[str] = mapped_column(String(20), default="pending")
    reviewer_note: Mapped[Optional[str]] = mapped_column(Text)

    assignment: Mapped["Assignment"] = relationship(back_populates="submissions")
    student: Mapped["Student"] = relationship()


class AICallLog(Base):
    """One row per model call (grading, rubric generation, question extraction)."""

    __tablename__ = "ai_call_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    purpose: Mapped[str] = mapped_column(String(30))  # grading | regrade | rubric | extract_questions
    exam_id: Mapped[Optional[int]] = mapped_column(Integer, index=True)
    submission_id: Mapped[Optional[int]] = mapped_column(Integer, index=True)
    model: Mapped[Optional[str]] = mapped_column(String(100))
    raw_response: Mapped[Optional[str]] = mapped_column(Text)
    stop_reason: Mapped[Optional[str]] = mapped_column(String(30))
    input_tokens: Mapped[Optional[int]] = mapped_column(Integer)
    output_tokens: Mapped[Optional[int]] = mapped_column(Integer)
    latency_ms: Mapped[Optional[int]] = mapped_column(Integer)
    error: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class ReviewRequest(TimestampMixin, Base):
    """A student's request to have specific questions of an approved+published exam re-checked (one per paper)."""

    __tablename__ = "review_requests"
    __table_args__ = (UniqueConstraint("submission_id", name="uq_review_request_submission"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    submission_id: Mapped[int] = mapped_column(ForeignKey("submissions.id", ondelete="CASCADE"), index=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id", ondelete="CASCADE"), index=True)
    exam_id: Mapped[int] = mapped_column(ForeignKey("exams.id", ondelete="CASCADE"), index=True)
    message: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)  # pending | decided | rejected
    response_message: Mapped[Optional[str]] = mapped_column(Text)  # instructor -> student
    decided_by_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    decided_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    paper_access: Mapped[bool] = mapped_column(Boolean, default=False)  # may the student view their own paper?

    submission: Mapped["Submission"] = relationship()
    student: Mapped["Student"] = relationship()
    exam: Mapped["Exam"] = relationship()
    items: Mapped[list["ReviewRequestItem"]] = relationship(
        back_populates="request", cascade="all, delete-orphan", order_by="ReviewRequestItem.id"
    )


class ReviewRequestItem(TimestampMixin, Base):
    __tablename__ = "review_request_items"
    __table_args__ = (UniqueConstraint("request_id", "question_id", name="uq_review_item"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("review_requests.id", ondelete="CASCADE"), index=True)
    question_id: Mapped[int] = mapped_column(ForeignKey("questions.id", ondelete="CASCADE"))
    original_mark: Mapped[Optional[float]] = mapped_column(Float)  # final mark when the request was filed
    # optional AI second opinion (independent re-grade; advisory only)
    ai_mark: Mapped[Optional[float]] = mapped_column(Float)
    ai_reason: Mapped[str] = mapped_column(Text, default="")
    ai_confidence: Mapped[Optional[float]] = mapped_column(Float)
    ai_breakdown: Mapped[list] = mapped_column(JSON, default=list)
    ai_flags: Mapped[list] = mapped_column(JSON, default=list)
    ai_raw_response: Mapped[Optional[str]] = mapped_column(Text)
    ai_ran_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    # instructor decision
    decision: Mapped[str] = mapped_column(String(20), default="pending")  # pending | kept | changed
    new_mark: Mapped[Optional[float]] = mapped_column(Float)
    note: Mapped[Optional[str]] = mapped_column(Text)  # visible to the student

    request: Mapped["ReviewRequest"] = relationship(back_populates="items")
    question: Mapped["Question"] = relationship()


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(40))
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text, default="")
    entity_type: Mapped[Optional[str]] = mapped_column(String(40))
    entity_id: Mapped[Optional[int]] = mapped_column(Integer)
    read_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)


class AuditLog(Base):
    """Who did what, when. Append-only: nothing in the app updates or deletes these rows."""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[Optional[int]] = mapped_column(Integer, index=True)
    actor: Mapped[Optional[str]] = mapped_column(String(50))  # university_id at the time (or attempted id)
    action: Mapped[str] = mapped_column(String(60), index=True)
    entity_type: Mapped[Optional[str]] = mapped_column(String(40))
    entity_id: Mapped[Optional[int]] = mapped_column(Integer)
    details: Mapped[Optional[dict]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)


class GradeCategory(TimestampMixin, Base):
    """A weighted bucket in a course's overall grade (e.g. "Exams" 50%, "Assignments" 30%,
    "Quizzes" 20% with drop_lowest=1). Weights are validated to sum to 100 at the course level
    when computing the weighted gradebook, not enforced per-row (categories are added one at a time)."""

    __tablename__ = "grade_categories"
    __table_args__ = (UniqueConstraint("course_id", "name", name="uq_category_course_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    weight_pct: Mapped[float] = mapped_column(Float)
    order: Mapped[int] = mapped_column(Integer, default=0)
    drop_lowest: Mapped[int] = mapped_column(Integer, default=0)

    course: Mapped["Course"] = relationship()
    items: Mapped[list["GradeCategoryItem"]] = relationship(
        back_populates="category", cascade="all, delete-orphan", order_by="GradeCategoryItem.id"
    )


class GradeCategoryItem(TimestampMixin, Base):
    """One exam or assignment counted inside a category. entity_type + entity_id rather than two
    nullable FKs, since an item is exactly one of Exam or Assignment (both already belong to this
    course; ownership is re-checked through the category's course, not through the entity itself)."""

    __tablename__ = "grade_category_items"
    __table_args__ = (
        UniqueConstraint("category_id", "entity_type", "entity_id", name="uq_category_item"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("grade_categories.id", ondelete="CASCADE"), index=True)
    entity_type: Mapped[str] = mapped_column(String(20))  # "exam" | "assignment"
    entity_id: Mapped[int] = mapped_column(Integer)

    category: Mapped["GradeCategory"] = relationship(back_populates="items")
