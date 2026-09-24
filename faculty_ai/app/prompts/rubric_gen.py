"""Prompts + tool schemas for rubric generation and question extraction."""

RUBRIC_TOOL_NAME = "submit_rubric"
EXTRACT_TOOL_NAME = "submit_questions"

_CRITERION = {
    "type": "object",
    "properties": {
        "criterion": {"type": "string"},
        "max_mark": {"type": "number"},
        "description": {"type": "string", "description": "What earns full / partial marks."},
    },
    "required": ["criterion", "max_mark", "description"],
}

RUBRIC_TOOL = {
    "name": RUBRIC_TOOL_NAME,
    "description": "Submit the grading rubric for one question.",
    "input_schema": {
        "type": "object",
        "properties": {"criteria": {"type": "array", "items": _CRITERION, "minItems": 2, "maxItems": 8}},
        "required": ["criteria"],
    },
}

EXTRACT_TOOL = {
    "name": EXTRACT_TOOL_NAME,
    "description": "Submit every question found in the exam, with model answer and a proposed rubric.",
    "input_schema": {
        "type": "object",
        "properties": {
            "questions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "number": {"type": "integer"},
                        "text": {"type": "string", "description": "Full question text (transcribe equations)."},
                        "max_mark": {"type": "number"},
                        "model_answer": {
                            "type": "string",
                            "description": "Model answer/solution steps from the model-answer file, if provided.",
                        },
                        "rubric": {"type": "array", "items": _CRITERION},
                    },
                    "required": ["number", "text", "max_mark", "model_answer", "rubric"],
                },
            }
        },
        "required": ["questions"],
    },
}

RUBRIC_SYSTEM_PROMPT = """You are an assessment-design assistant for university instructors. Produce clear, objective grading rubrics.
- The criteria max marks MUST sum exactly to the question's total marks.
- Use 2-6 criteria. Typical engineering split: correct method/approach, calculations, final answer, units, presentation - but adapt to the question.
- Criteria must be observable in a student's written solution and worded so two graders would award the same marks.
- Write in the same language as the question text (English if unclear).
- Always answer by calling the tool."""

EXTRACT_SYSTEM_PROMPT = """You are an assessment assistant. Read the exam paper (and the model-answer file if provided) and extract every question.
- Keep the exam's own question numbering (1, 2, 3 ...). Treat sub-parts (a, b, c) as part of their parent question unless they carry their own separate marks and the exam is clearly graded per part; in that case still keep one entry per numbered question and mention sub-parts in the text.
- `max_mark` must be the mark printed in the exam. If it is not printed, estimate proportionally and keep the total sensible.
- For each question propose a rubric whose criteria max marks sum exactly to max_mark.
- Do not invent content that is not in the files. Always answer by calling the tool."""


def build_rubric_request(question_text: str, model_answer: str, max_mark: float, extra: str = "") -> str:
    return (
        f"Question ({max_mark:g} marks):\n{question_text or '(see attached exam)'}\n\n"
        f"Model answer:\n{model_answer or '(none provided)'}\n\n"
        f"{extra}\nCreate a rubric whose criteria sum to exactly {max_mark:g}."
    )
