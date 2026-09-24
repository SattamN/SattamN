"""Rubric helpers: validation, defaults, AI generation, and question extraction from an exam file."""
from typing import Optional

from pydantic import BaseModel, ValidationError

from ..models import Exam, Question
from ..prompts.rubric_gen import (
    EXTRACT_SYSTEM_PROMPT,
    EXTRACT_TOOL,
    RUBRIC_SYSTEM_PROMPT,
    RUBRIC_TOOL,
    build_rubric_request,
)
from .ai_grader import TOL, AIClient, AIError, OnCall
from .storage import build_file_blocks

_DEFAULT_SPLIT = [
    ("Correct method / approach", 0.30, "Appropriate method, principles and setup."),
    ("Calculations", 0.30, "Correct intermediate steps and arithmetic."),
    ("Final answer", 0.20, "Correct final result."),
    ("Units", 0.10, "Correct units and consistency."),
    ("Presentation", 0.10, "Clear, organised, readable solution."),
]


def _round_q(x: float) -> float:
    return round(x * 4) / 4  # quarter-mark granularity


def default_rubric(max_mark: float) -> dict:
    """Generic starter rubric (method / calculations / answer / units / presentation) summing to max_mark."""
    criteria, used = [], 0.0
    for i, (name, share, desc) in enumerate(_DEFAULT_SPLIT):
        m = round(max_mark - used, 4) if i == len(_DEFAULT_SPLIT) - 1 else _round_q(max_mark * share)
        used += m
        criteria.append({"criterion": name, "max_mark": m, "description": desc})
    criteria = [c for c in criteria if c["max_mark"] > 0]
    return {"criteria": criteria}


def normalize_rubric(data: dict) -> dict:
    """Validate structure and return a clean copy. Raises ValueError."""
    crit = (data or {}).get("criteria")
    if not isinstance(crit, list) or not crit:
        raise ValueError("Rubric needs at least one criterion.")
    out, seen = [], set()
    for c in crit:
        name = str(c.get("criterion", "")).strip()
        try:
            mm = float(c.get("max_mark"))
        except (TypeError, ValueError):
            raise ValueError(f"Criterion '{name}' has an invalid max_mark.")
        if not name:
            raise ValueError("Every criterion needs a name.")
        if mm <= 0:
            raise ValueError(f"Criterion '{name}' must have max_mark > 0.")
        if name.lower() in seen:
            raise ValueError(f"Duplicate criterion name '{name}'.")
        seen.add(name.lower())
        out.append({"criterion": name, "max_mark": mm, "description": str(c.get("description", "")).strip()})
    return {"criteria": out}


def rubric_total(rubric: dict) -> float:
    return round(sum(float(c["max_mark"]) for c in rubric["criteria"]), 4)


def validate_rubric_total(rubric: dict, max_mark: float) -> None:
    total = rubric_total(rubric)
    if abs(total - float(max_mark)) > TOL:
        raise ValueError(f"Rubric criteria sum to {total:g}, but the question is worth {max_mark:g}.")


def rescale_rubric(rubric: dict, max_mark: float) -> dict:
    """Proportionally rescale (used to repair AI rubrics that are slightly off)."""
    total = rubric_total(rubric)
    if total <= 0:
        return default_rubric(max_mark)
    crit, used = [], 0.0
    for i, c in enumerate(rubric["criteria"]):
        m = round(max_mark - used, 4) if i == len(rubric["criteria"]) - 1 else _round_q(c["max_mark"] * max_mark / total)
        used += m
        crit.append({**c, "max_mark": m})
    if any(c["max_mark"] <= 0 for c in crit):
        return default_rubric(max_mark)
    return {"criteria": crit}


# --------------------------------------------------------------------------- AI: single rubric


def generate_rubric(question: Question, ai: Optional[AIClient] = None, on_call: Optional[OnCall] = None) -> dict:
    ai = ai or AIClient()
    content = [{"type": "text", "text": build_rubric_request(question.text, question.model_answer, question.max_mark)}]
    try:
        res = ai.call_tool(system=RUBRIC_SYSTEM_PROMPT, content=content, tool=RUBRIC_TOOL, max_tokens=2000)
    except AIError as e:
        if on_call:
            on_call("rubric", None, str(e))
        raise
    if on_call:
        on_call("rubric", res, None)
    try:
        rubric = normalize_rubric(res.data)
    except ValueError as e:
        raise AIError(f"AI returned an invalid rubric: {e}") from e
    if abs(rubric_total(rubric) - question.max_mark) > TOL:
        rubric = rescale_rubric(rubric, question.max_mark)
    return rubric


# --------------------------------------------------------------------------- AI: extract questions


class _ExtractedQuestion(BaseModel):
    number: int
    text: str = ""
    max_mark: float
    model_answer: str = ""
    rubric: list[dict] = []


class _ExtractedExam(BaseModel):
    questions: list[_ExtractedQuestion]


def extract_questions(exam: Exam, ai: Optional[AIClient] = None, on_call: Optional[OnCall] = None) -> list[dict]:
    """Read the exam (+ model answer) and propose questions with rubrics. Nothing is approved automatically."""
    if not exam.exam_file_path:
        raise ValueError("Upload the exam file first.")
    ai = ai or AIClient()
    content = [{"type": "text", "text": "EXAM PAPER:"}] + build_file_blocks([exam.exam_file_path])
    if exam.model_answer_path:
        content += [{"type": "text", "text": "MODEL ANSWER / MARKING SCHEME:"}] + build_file_blocks([exam.model_answer_path])
    content.append({"type": "text", "text": "Extract all questions now."})
    try:
        res = ai.call_tool(system=EXTRACT_SYSTEM_PROMPT, content=content, tool=EXTRACT_TOOL, max_tokens=16000)
        parsed = _ExtractedExam.model_validate(res.data)
    except AIError as e:
        if on_call:
            on_call("extract_questions", None, str(e))
        raise
    except ValidationError as e:
        if on_call:
            on_call("extract_questions", res, f"validation_failed: {e}")
        raise AIError(f"AI returned invalid question data: {e}") from e
    if on_call:
        on_call("extract_questions", res, None)

    out, seen = [], set()
    for q in sorted(parsed.questions, key=lambda x: x.number):
        if q.number in seen or q.max_mark <= 0:
            continue
        seen.add(q.number)
        try:
            rub = normalize_rubric({"criteria": q.rubric})
            if abs(rubric_total(rub) - q.max_mark) > TOL:
                rub = rescale_rubric(rub, q.max_mark)
        except ValueError:
            rub = None
        out.append(
            {"number": q.number, "text": q.text, "max_mark": q.max_mark, "model_answer": q.model_answer, "rubric": rub}
        )
    if not out:
        raise AIError("No questions could be extracted from the exam file.")
    return out
