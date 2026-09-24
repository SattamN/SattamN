"""AI grading service.

Pipeline:  files + approved rubric  ->  forced tool call (strict JSON)  ->  pydantic validation
           ->  normalisation (clamp/reconcile)  ->  rule-based review flags  ->  DB.

The model's confidence is only ONE review signal; see `evaluate_review_flags`.
"""
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

import anthropic
from pydantic import ValidationError
from sqlalchemy.orm import Session

from .. import database
from ..config import Settings, get_settings
from ..models import AICallLog, Assignment, AssignmentSubmission, Exam, Grade, Question, Submission
from ..prompts.grading import (
    GRADING_TOOL,
    build_grading_instructions,
    build_grading_system_prompt,
    build_objection_block,
    build_second_opinion_system_prompt,
)
from ..schemas import PaperResult, QuestionResult
from .storage import build_file_blocks

TOL = 0.01

# ---- review flag codes (stored in grades.review_flags) ----
FLAG_LOW_CONFIDENCE = "low_confidence"
FLAG_MODEL_FLAGGED = "model_flagged"
FLAG_ANSWER_UNCLEAR = "answer_unclear"
FLAG_BLANK_WITH_MARKS = "blank_but_marked"
FLAG_MARK_OUT_OF_RANGE = "mark_out_of_range"
FLAG_MAX_MISMATCH = "max_mark_mismatch"
FLAG_BREAKDOWN_SUM = "breakdown_sum_mismatch"
FLAG_RUBRIC_MISMATCH = "rubric_criteria_mismatch"
FLAG_EMPTY_REASON = "empty_reason"
FLAG_MISSING = "missing_from_ai_response"
FLAG_DUPLICATE = "duplicate_in_ai_response"


class AIError(Exception):
    """Model call failed or returned unusable output."""


class GradingConflict(Exception):
    """Refusing to overwrite work the instructor already reviewed."""


# --------------------------------------------------------------------------- low-level client


@dataclass
class ToolResult:
    data: dict
    raw: str
    model: str
    stop_reason: Optional[str]
    input_tokens: Optional[int]
    output_tokens: Optional[int]
    latency_ms: int


OnCall = Callable[[str, Optional[ToolResult], Optional[str]], None]  # (purpose, result, error)


class AIClient:
    """Thin wrapper: forces a tool call so the output is always structured JSON."""

    def __init__(self, client=None, settings: Optional[Settings] = None):
        self.settings = settings or get_settings()
        self._client = client

    @property
    def client(self):
        if self._client is None:
            if not self.settings.anthropic_api_key:
                raise AIError("ANTHROPIC_API_KEY is not configured (see .env.example)")
            self._client = anthropic.Anthropic(api_key=self.settings.anthropic_api_key, max_retries=3, timeout=300)
        return self._client

    def call_tool(self, *, system: str, content: list[dict], tool: dict, max_tokens: Optional[int] = None) -> ToolResult:
        t0 = time.perf_counter()
        try:
            resp = self.client.messages.create(
                model=self.settings.claude_model,
                max_tokens=max_tokens or self.settings.grading_max_tokens,
                system=system,
                tools=[tool],
                tool_choice={"type": "tool", "name": tool["name"]},
                messages=[{"role": "user", "content": content}],
            )
        except anthropic.APIError as e:
            raise AIError(f"Anthropic API error: {e}") from e
        latency = int((time.perf_counter() - t0) * 1000)

        stop = getattr(resp, "stop_reason", None)
        if stop == "max_tokens":
            raise AIError("Model output was truncated (max_tokens). Try fewer questions per call or raise GRADING_MAX_TOKENS.")
        block = next(
            (b for b in resp.content if getattr(b, "type", None) == "tool_use" and b.name == tool["name"]),
            None,
        )
        if block is None:
            raise AIError("Model did not return the expected structured output.")
        data = dict(block.input)
        usage = getattr(resp, "usage", None)
        return ToolResult(
            data=data,
            raw=json.dumps(data, ensure_ascii=False),
            model=self.settings.claude_model,
            stop_reason=stop,
            input_tokens=getattr(usage, "input_tokens", None),
            output_tokens=getattr(usage, "output_tokens", None),
            latency_ms=latency,
        )


def make_logger(db: Session, *, exam_id: Optional[int] = None, submission_id: Optional[int] = None) -> OnCall:
    """Callback that persists every model call (success or failure) to ai_call_logs."""

    def _log(purpose: str, res: Optional[ToolResult], error: Optional[str]) -> None:
        db.add(
            AICallLog(
                purpose=purpose,
                exam_id=exam_id,
                submission_id=submission_id,
                model=res.model if res else None,
                raw_response=res.raw if res else None,
                stop_reason=res.stop_reason if res else None,
                input_tokens=res.input_tokens if res else None,
                output_tokens=res.output_tokens if res else None,
                latency_ms=res.latency_ms if res else None,
                error=error,
            )
        )

    return _log


# --------------------------------------------------------------------------- review rules


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def evaluate_review_flags(
    result: QuestionResult, question: Question, threshold: float
) -> list[str]:
    """Rule-based decision on whether an instructor must look at this question.

    Confidence is only one signal. Any flag => needs_review.
    """
    flags: list[str] = []
    max_mark = float(question.max_mark)

    if result.confidence < threshold:
        flags.append(FLAG_LOW_CONFIDENCE)
    if result.needs_review:
        flags.append(FLAG_MODEL_FLAGGED)
    if result.answer_status == "unclear":
        flags.append(FLAG_ANSWER_UNCLEAR)
    if result.answer_status == "blank" and result.mark > TOL:
        flags.append(FLAG_BLANK_WITH_MARKS)
    if result.mark < -TOL or result.mark > max_mark + TOL:
        flags.append(FLAG_MARK_OUT_OF_RANGE)
    if abs(result.max_mark - max_mark) > TOL:
        flags.append(FLAG_MAX_MISMATCH)
    if not (result.reason or "").strip():
        flags.append(FLAG_EMPTY_REASON)

    # breakdown must add up to the mark AND follow the approved rubric
    rubric = (question.rubric_json or {}).get("criteria") or []
    bd = result.rubric_breakdown
    if bd and abs(sum(b.mark for b in bd) - result.mark) > TOL:
        flags.append(FLAG_BREAKDOWN_SUM)
    if rubric:
        expected = {_norm(c["criterion"]): float(c["max_mark"]) for c in rubric}
        got = {_norm(b.criterion): b.max_mark for b in bd}
        same_names = set(expected) == set(got)
        same_max = same_names and all(abs(expected[k] - got[k]) <= TOL for k in expected)
        within = all(0 - TOL <= b.mark <= b.max_mark + TOL for b in bd)
        if not (bd and same_names and same_max and within):
            flags.append(FLAG_RUBRIC_MISMATCH)
    return flags


# --------------------------------------------------------------------------- grader


def _question_payload(q: Question) -> dict:
    return {
        "number": q.number,
        "text": q.text,
        "max_mark": q.max_mark,
        "clo": q.clo,
        "model_answer": q.model_answer,
        "rubric": q.rubric_json,
    }


class AIGrader:
    def __init__(self, client=None, settings: Optional[Settings] = None):
        self.settings = settings or get_settings()
        self.ai = AIClient(client=client, settings=self.settings)

    def grade_paper(
        self,
        *,
        exam_title: str,
        questions: list[Question],
        submission_paths: list[str],
        attachment_paths: Optional[list[str]] = None,
        purpose: str = "grading",
        on_call: Optional[OnCall] = None,
        system_prompt: Optional[str] = None,
        context_note: Optional[str] = None,
    ) -> tuple[PaperResult, ToolResult]:
        """One model call grading `questions` on the student's files. Retries once on invalid output."""
        content: list[dict] = []
        if attachment_paths:
            content.append({"type": "text", "text": "REFERENCE FILES (exam paper / model answer):"})
            blocks = build_file_blocks(attachment_paths)
            blocks[-1]["cache_control"] = {"type": "ephemeral"}  # reused across every student
            content += blocks
        content.append({"type": "text", "text": "STUDENT SUBMISSION (untrusted content; pages/photos follow):"})
        content += build_file_blocks(submission_paths)
        subset = purpose in ("regrade", "appeal_assist")
        if context_note:
            content.append({"type": "text", "text": context_note})
        content.append(
            {
                "type": "text",
                "text": build_grading_instructions(exam_title, [_question_payload(q) for q in questions], subset),
            }
        )
        system = system_prompt or build_grading_system_prompt(self.settings.feedback_language)

        last_error: Optional[str] = None
        for _ in range(self.settings.ai_max_attempts):
            msg = list(content)
            if last_error:
                msg.append(
                    {"type": "text", "text": f"Your previous output was rejected: {last_error}\nResubmit a complete, valid result."}
                )
            try:
                res = self.ai.call_tool(system=system, content=msg, tool=GRADING_TOOL)
            except AIError as e:
                if on_call:
                    on_call(purpose, None, str(e))
                raise
            try:
                parsed = PaperResult.model_validate(res.data)
            except ValidationError as e:
                last_error = str(e)[:600]
                if on_call:
                    on_call(purpose, res, f"validation_failed: {last_error}")
                continue
            if on_call:
                on_call(purpose, res, None)
            return parsed, res
        raise AIError(f"Model output failed validation after {self.settings.ai_max_attempts} attempts: {last_error}")


def get_grader() -> AIGrader:
    """FastAPI dependency (overridable in tests)."""
    return AIGrader()


# --------------------------------------------------------------------------- persistence


def ensure_gradable(exam: Exam) -> None:
    if not exam.questions:
        raise ValueError("Exam has no questions.")
    missing = [q.number for q in exam.questions if not q.rubric_approved]
    if missing:
        raise ValueError(f"Approve the rubric for question(s) {missing} before grading.")


def _apply_result(grade: Grade, q: Question, item: Optional[QuestionResult], raw: Optional[dict], model: str,
                  threshold: float, extra_flags: Optional[list[str]] = None) -> None:
    """Fill the AI columns of a grade and reset the instructor decision."""
    if item is None:
        grade.ai_mark = None
        grade.ai_reason = "The AI did not return a result for this question."
        grade.ai_confidence = 0.0
        grade.ai_needs_review = True
        grade.ai_rubric_breakdown = []
        grade.ai_answer_text = ""
        grade.answer_status = "unclear"
        grade.ai_raw_response = None
        flags = [FLAG_MISSING]
    else:
        flags = evaluate_review_flags(item, q, threshold)
        grade.ai_mark = round(min(max(item.mark, 0.0), float(q.max_mark)), 4)  # stored clamped; raw kept below
        grade.ai_reason = item.reason
        grade.ai_confidence = item.confidence
        grade.ai_needs_review = item.needs_review
        grade.ai_rubric_breakdown = [b.model_dump() for b in item.rubric_breakdown]
        grade.ai_answer_text = item.student_answer_transcription
        grade.answer_status = item.answer_status
        grade.ai_raw_response = json.dumps(raw if raw is not None else item.model_dump(), ensure_ascii=False)
    flags += extra_flags or []
    grade.review_flags = flags
    grade.needs_review = bool(flags)
    grade.ai_model = model
    grade.final_mark = None
    grade.decision = "pending"
    grade.reviewer_note = None


def _index_results(parsed: PaperResult, res: ToolResult) -> tuple[dict[int, QuestionResult], dict[int, dict], set[int]]:
    by_num: dict[int, QuestionResult] = {}
    dups: set[int] = set()
    for item in parsed.questions:
        if item.question_number in by_num:
            dups.add(item.question_number)
        else:
            by_num[item.question_number] = item
    raw_by_num: dict[int, dict] = {}
    for r in res.data.get("questions", []):
        if isinstance(r, dict):
            raw_by_num.setdefault(r.get("question_number"), r)
    return by_num, raw_by_num, dups


def grade_submission(
    db: Session, submission: Submission, *, grader: Optional[AIGrader] = None, force: bool = False
) -> Submission:
    grader = grader or AIGrader()
    settings = grader.settings
    exam = submission.exam
    ensure_gradable(exam)
    if not force and any(g.decision != "pending" for g in submission.grades):
        raise GradingConflict("This paper already has reviewed marks. Use force=true to regrade (they will be reset).")
    if submission.status == "approved":
        raise GradingConflict("Submission is approved. Reopen it before regrading.")

    submission.status = "grading"
    submission.error = None
    db.commit()

    attachments = []
    if settings.attach_exam_files:
        attachments = [p for p in (exam.exam_file_path, exam.model_answer_path) if p]

    questions = sorted(exam.questions, key=lambda q: q.number)
    log = make_logger(db, exam_id=exam.id, submission_id=submission.id)
    try:
        parsed, res = grader.grade_paper(
            exam_title=exam.title,
            questions=questions,
            submission_paths=list(submission.file_paths or []),
            attachment_paths=attachments,
            on_call=log,
        )
    except (AIError, FileNotFoundError) as e:
        submission.status = "failed"
        submission.error = str(e)[:1000]
        db.commit()
        raise AIError(str(e)) from e

    by_num, raw_by_num, dups = _index_results(parsed, res)
    submission.grades.clear()
    db.flush()
    for q in questions:
        g = Grade(submission_id=submission.id, question_id=q.id)
        _apply_result(
            g, q, by_num.get(q.number), raw_by_num.get(q.number), res.model,
            settings.review_confidence_threshold,
            extra_flags=[FLAG_DUPLICATE] if q.number in dups else None,
        )
        submission.grades.append(g)
    submission.status = "graded"
    submission.graded_at = datetime.now(timezone.utc)
    db.commit()
    return submission


def regrade_question(db: Session, grade: Grade, *, grader: Optional[AIGrader] = None, force: bool = False) -> Grade:
    grader = grader or AIGrader()
    settings = grader.settings
    sub = grade.submission
    if sub.status == "approved":
        raise GradingConflict("Submission is approved. Reopen it before regrading.")
    if grade.decision != "pending" and not force:
        raise GradingConflict("This question was already reviewed. Use force=true to regrade (decision will be reset).")
    q = grade.question
    log = make_logger(db, exam_id=sub.exam_id, submission_id=sub.id)
    attachments = [p for p in (sub.exam.exam_file_path, sub.exam.model_answer_path) if p] if settings.attach_exam_files else []
    try:
        parsed, res = grader.grade_paper(
            exam_title=sub.exam.title,
            questions=[q],
            submission_paths=list(sub.file_paths or []),
            attachment_paths=attachments,
            purpose="regrade",
            on_call=log,
        )
    except (AIError, FileNotFoundError) as e:
        db.commit()  # keep the failure log
        raise AIError(str(e)) from e
    by_num, raw_by_num, dups = _index_results(parsed, res)
    _apply_result(grade, q, by_num.get(q.number), raw_by_num.get(q.number), res.model,
                  settings.review_confidence_threshold, [FLAG_DUPLICATE] if q.number in dups else None)
    if sub.status == "approved":
        sub.status = "graded"
    db.commit()
    return grade


def run_exam_grading(
    submission_ids: list[int],
    *,
    force: bool = False,
    grader: Optional[AIGrader] = None,
    session_factory=None,
) -> dict[int, Optional[str]]:
    """Grade many papers concurrently (used by the background task). Returns {submission_id: error|None}."""
    grader = grader or AIGrader()
    factory = session_factory or database.SessionLocal
    workers = grader.settings.grading_concurrency

    def work(sid: int) -> tuple[int, Optional[str]]:
        db = factory()
        try:
            sub = db.get(Submission, sid)
            if sub is None:
                return sid, "not found"
            grade_submission(db, sub, grader=grader, force=force)
            return sid, None
        except GradingConflict as e:
            db.rollback()
            sub = db.get(Submission, sid)
            if sub is not None and sub.status == "queued":  # release from queue, keep existing work
                sub.status = "graded" if sub.grades else "uploaded"
                db.commit()
            return sid, f"skipped: {e}"
        except Exception as e:  # noqa: BLE001 - keep the batch going
            db.rollback()
            try:
                sub = db.get(Submission, sid)
                if sub is not None:
                    sub.status = "failed"
                    sub.error = str(e)[:1000]
                    db.commit()
            except Exception:
                db.rollback()
            return sid, str(e)
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return dict(pool.map(work, submission_ids))


def run_second_opinion(db: Session, request, *, grader: Optional[AIGrader] = None) -> None:
    """Independent AI re-grade of the questions in a review request. Advisory: never touches real marks."""
    grader = grader or AIGrader()
    settings = grader.settings
    sub, exam = request.submission, request.exam
    questions = [i.question for i in request.items]
    log = make_logger(db, exam_id=exam.id, submission_id=sub.id)
    attachments = [p for p in (exam.exam_file_path, exam.model_answer_path) if p] if settings.attach_exam_files else []
    try:
        parsed, res = grader.grade_paper(
            exam_title=exam.title,
            questions=questions,
            submission_paths=list(sub.file_paths or []),
            attachment_paths=attachments,
            purpose="appeal_assist",
            on_call=log,
            system_prompt=build_second_opinion_system_prompt(settings.feedback_language),
            context_note=build_objection_block(request.message),
        )
    except (AIError, FileNotFoundError) as e:
        db.commit()  # keep the failure log
        raise AIError(str(e)) from e
    by_num, raw_by_num, _ = _index_results(parsed, res)
    now = datetime.now(timezone.utc)
    for it in request.items:
        q = it.question
        r = by_num.get(q.number)
        it.ai_ran_at = now
        if r is None:
            it.ai_mark, it.ai_reason, it.ai_confidence, it.ai_breakdown = None, "The AI returned nothing for this question.", 0.0, []
            it.ai_flags, it.ai_raw_response = [FLAG_MISSING], None
            continue
        it.ai_mark = round(min(max(r.mark, 0.0), float(q.max_mark)), 4)
        it.ai_reason, it.ai_confidence = r.reason, r.confidence
        it.ai_breakdown = [b.model_dump() for b in r.rubric_breakdown]
        it.ai_flags = evaluate_review_flags(r, q, settings.review_confidence_threshold)
        raw = raw_by_num.get(q.number)
        it.ai_raw_response = json.dumps(raw if raw is not None else r.model_dump(), ensure_ascii=False)
    db.commit()


# --------------------------------------------------------------------------- assignments
#
# An Assignment has one implicit "question" (its own text/rubric/max_mark). _AssignmentItem
# adapts it to the same shape ai_grader already speaks (Question's attributes), so grade_paper,
# evaluate_review_flags and _question_payload all work completely unchanged.


@dataclass
class _AssignmentItem:
    id: int
    number: int
    text: str
    max_mark: float
    clo: Optional[str]
    model_answer: str
    rubric_json: Optional[dict]


def _as_item(a: Assignment) -> _AssignmentItem:
    return _AssignmentItem(
        id=a.id, number=1, text=a.description, max_mark=a.max_mark,
        clo=None, model_answer=a.model_answer, rubric_json=a.rubric_json,
    )


def ensure_assignment_gradable(a: Assignment) -> None:
    if not a.rubric_json or not a.rubric_approved:
        raise ValueError("Approve the assignment's rubric before grading.")


def _apply_assignment_result(sub: AssignmentSubmission, item_def: _AssignmentItem,
                             item: Optional[QuestionResult], raw: Optional[dict], model: str,
                             threshold: float) -> None:
    flags: list[str]
    if item is None:
        sub.ai_mark = None
        sub.ai_reason = "The AI did not return a result for this submission."
        sub.ai_confidence = 0.0
        sub.ai_needs_review = True
        sub.ai_rubric_breakdown = []
        sub.ai_answer_text = ""
        sub.answer_status = "unclear"
        sub.ai_raw_response = None
        flags = [FLAG_MISSING]
    else:
        flags = evaluate_review_flags(item, item_def, threshold)
        sub.ai_mark = round(min(max(item.mark, 0.0), float(item_def.max_mark)), 4)
        sub.ai_reason = item.reason
        sub.ai_confidence = item.confidence
        sub.ai_needs_review = item.needs_review
        sub.ai_rubric_breakdown = [b.model_dump() for b in item.rubric_breakdown]
        sub.ai_answer_text = item.student_answer_transcription
        sub.answer_status = item.answer_status
        sub.ai_raw_response = json.dumps(raw if raw is not None else item.model_dump(), ensure_ascii=False)
    sub.review_flags = flags
    sub.needs_review = bool(flags)
    sub.ai_model = model
    sub.final_mark = None
    sub.decision = "pending"
    sub.reviewer_note = None


def grade_assignment_submission(
    db: Session, sub: AssignmentSubmission, *, grader: Optional[AIGrader] = None, force: bool = False
) -> AssignmentSubmission:
    grader = grader or AIGrader()
    settings = grader.settings
    a = sub.assignment
    ensure_assignment_gradable(a)
    if not force and sub.decision != "pending":
        raise GradingConflict("This submission already has a reviewed mark. Use force=true to regrade (it will be reset).")
    if sub.status == "approved":
        raise GradingConflict("Submission is approved. Reopen it before regrading.")

    sub.status = "grading"
    sub.error = None
    db.commit()

    item_def = _as_item(a)
    log = make_logger(db, exam_id=None, submission_id=sub.id)
    try:
        parsed, res = grader.grade_paper(
            exam_title=a.title,
            questions=[item_def],
            submission_paths=list(sub.file_paths or []),
            purpose="grading",
            on_call=log,
        )
    except (AIError, FileNotFoundError) as e:
        sub.status = "failed"
        sub.error = str(e)[:1000]
        db.commit()
        raise AIError(str(e)) from e

    by_num, raw_by_num, _dups = _index_results(parsed, res)
    _apply_assignment_result(sub, item_def, by_num.get(1), raw_by_num.get(1), res.model, settings.review_confidence_threshold)
    sub.status = "graded"
    sub.graded_at = datetime.now(timezone.utc)
    db.commit()
    return sub


def run_assignment_grading(
    submission_ids: list[int], *, force: bool = False, grader: Optional[AIGrader] = None, session_factory=None,
) -> dict[int, Optional[str]]:
    grader = grader or AIGrader()
    factory = session_factory or database.SessionLocal
    workers = grader.settings.grading_concurrency

    def work(sid: int) -> tuple[int, Optional[str]]:
        db = factory()
        try:
            sub = db.get(AssignmentSubmission, sid)
            if sub is None:
                return sid, "not found"
            grade_assignment_submission(db, sub, grader=grader, force=force)
            return sid, None
        except GradingConflict as e:
            db.rollback()
            sub = db.get(AssignmentSubmission, sid)
            if sub is not None and sub.status == "queued":
                sub.status = "graded" if sub.decision != "pending" or sub.ai_mark is not None else "uploaded"
                db.commit()
            return sid, f"skipped: {e}"
        except Exception as e:  # noqa: BLE001
            db.rollback()
            try:
                sub = db.get(AssignmentSubmission, sid)
                if sub is not None:
                    sub.status = "failed"
                    sub.error = str(e)[:1000]
                    db.commit()
            except Exception:
                db.rollback()
            return sid, str(e)
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return dict(pool.map(work, submission_ids))
