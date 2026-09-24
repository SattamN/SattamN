from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Exam, Grade, Question, User
from ..security import check_course_access, faculty_user
from ..services.audit import audit
from ..schemas import ExamOut, QuestionIn, QuestionOut, QuestionUpdate, RubricIn, exam_out, question_out
from ..services import rubric as rubric_svc
from ..services.ai_grader import AIClient, AIError, make_logger
from ..services.storage import save_upload
from .courses import get_course

router = APIRouter(tags=["exams"])


def get_exam(db: Session, exam_id: int, user: User) -> Exam:
    e = db.get(Exam, exam_id)
    if not e:
        raise HTTPException(404, "Exam not found")
    check_course_access(e.course, user)
    return e


def get_question(db: Session, question_id: int, user: User) -> Question:
    q = db.get(Question, question_id)
    if not q:
        raise HTTPException(404, "Question not found")
    check_course_access(q.exam.course, user)
    return q


def _has_grades(db: Session, exam_id: int) -> bool:
    return db.query(Grade.id).join(Question, Grade.question_id == Question.id).filter(Question.exam_id == exam_id).first() is not None


def _locked(db: Session, exam_id: int) -> None:
    if _has_grades(db, exam_id):
        raise HTTPException(409, "This exam already has graded papers; questions/rubrics are locked. Create a new exam or delete the graded papers.")


def _set_rubric(q: Question, rubric: dict, approved: bool) -> None:
    q.rubric_json = rubric
    q.rubric_approved = approved


# ------------------------------------------------------------------ exams


@router.post("/courses/{course_id}/exams", response_model=ExamOut, status_code=201)
def create_exam(
    course_id: int,
    title: str = Form(...),
    exam_file: Optional[UploadFile] = File(None),
    model_answer_file: Optional[UploadFile] = File(None),
    db: Session = Depends(get_db), user: User = Depends(faculty_user),
):
    get_course(db, course_id, user)
    e = Exam(course_id=course_id, title=title.strip())
    if exam_file is not None and exam_file.filename:
        e.exam_file_path = save_upload(exam_file, "exams")
    if model_answer_file is not None and model_answer_file.filename:
        e.model_answer_path = save_upload(model_answer_file, "exams")
    db.add(e)
    db.commit()
    db.refresh(e)
    return exam_out(e)


@router.put("/exams/{exam_id}/files", response_model=ExamOut)
def replace_exam_files(
    exam_id: int,
    exam_file: Optional[UploadFile] = File(None),
    model_answer_file: Optional[UploadFile] = File(None),
    db: Session = Depends(get_db), user: User = Depends(faculty_user),
):
    e = get_exam(db, exam_id, user)
    if exam_file is not None and exam_file.filename:
        e.exam_file_path = save_upload(exam_file, "exams")
    if model_answer_file is not None and model_answer_file.filename:
        e.model_answer_path = save_upload(model_answer_file, "exams")
    db.commit()
    db.refresh(e)
    return exam_out(e)


@router.get("/courses/{course_id}/exams", response_model=list[ExamOut])
def list_exams(course_id: int, db: Session = Depends(get_db), user: User = Depends(faculty_user)):
    get_course(db, course_id, user)
    exams = db.query(Exam).filter(Exam.course_id == course_id).order_by(Exam.created_at.desc()).all()
    return [exam_out(e) for e in exams]


@router.get("/exams/{exam_id}", response_model=ExamOut)
def read_exam(exam_id: int, db: Session = Depends(get_db), user: User = Depends(faculty_user)):
    return exam_out(get_exam(db, exam_id, user))


@router.delete("/exams/{exam_id}", status_code=204)
def delete_exam(exam_id: int, db: Session = Depends(get_db), user: User = Depends(faculty_user)):
    e = get_exam(db, exam_id, user)
    audit(db, user, "exam_deleted", "exam", e.id, {"title": e.title})
    db.delete(e)
    db.commit()


# ------------------------------------------------------------------ questions


@router.get("/exams/{exam_id}/questions", response_model=list[QuestionOut])
def list_questions(exam_id: int, db: Session = Depends(get_db), user: User = Depends(faculty_user)):
    return [question_out(q) for q in get_exam(db, exam_id, user).questions]


@router.post("/exams/{exam_id}/questions", response_model=QuestionOut, status_code=201)
def add_question(exam_id: int, body: QuestionIn, db: Session = Depends(get_db), user: User = Depends(faculty_user)):
    exam = get_exam(db, exam_id, user)
    _locked(db, exam_id)
    if any(q.number == body.number for q in exam.questions):
        raise HTTPException(409, f"Question {body.number} already exists")
    if not (body.text.strip() or body.model_answer.strip()):
        raise HTTPException(400, "Provide the question text or a model answer")
    q = Question(exam_id=exam_id, number=body.number, text=body.text, max_mark=body.max_mark,
                 clo=body.clo, model_answer=body.model_answer)
    if body.rubric:
        try:
            rub = rubric_svc.normalize_rubric(body.rubric.model_dump())
            rubric_svc.validate_rubric_total(rub, body.max_mark)
        except ValueError as e:
            raise HTTPException(400, str(e))
        _set_rubric(q, rub, False)  # suggested, still needs instructor approval
    db.add(q)
    db.commit()
    db.refresh(q)
    return question_out(q)


@router.put("/questions/{question_id}", response_model=QuestionOut)
def update_question(question_id: int, body: QuestionUpdate, db: Session = Depends(get_db), user: User = Depends(faculty_user)):
    q = get_question(db, question_id, user)
    _locked(db, q.exam_id)
    data = body.model_dump(exclude_unset=True)
    for k, v in data.items():
        setattr(q, k, v)
    if "max_mark" in data and q.rubric_json:
        try:
            rubric_svc.validate_rubric_total(q.rubric_json, q.max_mark)
        except ValueError:
            q.rubric_approved = False  # rubric no longer matches -> must be re-approved
    db.commit()
    db.refresh(q)
    return question_out(q)


@router.delete("/questions/{question_id}", status_code=204)
def delete_question(question_id: int, db: Session = Depends(get_db), user: User = Depends(faculty_user)):
    q = get_question(db, question_id, user)
    _locked(db, q.exam_id)
    db.delete(q)
    db.commit()


# ------------------------------------------------------------------ AI: extraction


@router.post("/exams/{exam_id}/extract-questions", response_model=list[QuestionOut])
def extract_questions(exam_id: int, replace: bool = False, db: Session = Depends(get_db), user: User = Depends(faculty_user)):
    exam = get_exam(db, exam_id, user)
    if not exam.exam_file_path:
        raise HTTPException(400, "Upload the exam file first")
    if exam.questions:
        if not replace:
            raise HTTPException(409, "Exam already has questions. Pass replace=true to overwrite them.")
        _locked(db, exam_id)
    log = make_logger(db, exam_id=exam_id)
    try:
        extracted = rubric_svc.extract_questions(exam, AIClient(), on_call=log)
    except (AIError, ValueError, FileNotFoundError) as e:
        db.commit()  # keep the failure log
        raise HTTPException(502, str(e))
    exam.questions.clear()
    db.flush()
    for x in extracted:
        q = Question(exam_id=exam_id, number=x["number"], text=x["text"], max_mark=x["max_mark"],
                     model_answer=x["model_answer"])
        if x["rubric"]:
            _set_rubric(q, x["rubric"], False)
        exam.questions.append(q)
    db.commit()
    db.refresh(exam)
    return [question_out(q) for q in exam.questions]


# ------------------------------------------------------------------ rubrics


@router.post("/questions/{question_id}/rubric/generate", response_model=QuestionOut)
def generate_rubric(question_id: int, mode: str = "ai", db: Session = Depends(get_db), user: User = Depends(faculty_user)):
    q = get_question(db, question_id, user)
    _locked(db, q.exam_id)
    if mode == "default":
        rub = rubric_svc.default_rubric(q.max_mark)
    elif mode == "ai":
        log = make_logger(db, exam_id=q.exam_id)
        try:
            rub = rubric_svc.generate_rubric(q, AIClient(), on_call=log)
        except AIError as e:
            db.commit()
            raise HTTPException(502, str(e))
    else:
        raise HTTPException(400, "mode must be 'ai' or 'default'")
    _set_rubric(q, rub, False)
    db.commit()
    db.refresh(q)
    return question_out(q)


@router.put("/questions/{question_id}/rubric", response_model=QuestionOut)
def save_and_approve_rubric(question_id: int, body: RubricIn, db: Session = Depends(get_db), user: User = Depends(faculty_user)):
    """The instructor's explicit approval of a rubric (saves + approves)."""
    q = get_question(db, question_id, user)
    _locked(db, q.exam_id)
    try:
        rub = rubric_svc.normalize_rubric(body.model_dump())
        rubric_svc.validate_rubric_total(rub, q.max_mark)
    except ValueError as e:
        raise HTTPException(400, str(e))
    _set_rubric(q, rub, True)
    db.commit()
    db.refresh(q)
    return question_out(q)


@router.post("/exams/{exam_id}/rubrics/approve-all")
def approve_all_rubrics(exam_id: int, db: Session = Depends(get_db), user: User = Depends(faculty_user)):
    exam = get_exam(db, exam_id, user)
    _locked(db, exam_id)
    approved, missing = 0, []
    for q in exam.questions:
        try:
            if not q.rubric_json:
                raise ValueError("no rubric")
            rubric_svc.validate_rubric_total(q.rubric_json, q.max_mark)
            q.rubric_approved = True
            approved += 1
        except ValueError:
            missing.append(q.number)
    db.commit()
    return {"approved": approved, "needs_attention": missing}
