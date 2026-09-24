from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Course, CourseEnrollment, Student, User
from ..security import check_course_access, faculty_user, hash_password, new_temp_password
from ..services.audit import audit
from ..services.roster import parse_roster
from ..schemas import CourseIn, CourseOut

router = APIRouter(prefix="/courses", tags=["courses"])


def get_course(db: Session, course_id: int, user: User) -> Course:
    return check_course_access(db.get(Course, course_id), user)


@router.post("", response_model=CourseOut, status_code=201)
def create_course(body: CourseIn, db: Session = Depends(get_db), user: User = Depends(faculty_user)):
    c = Course(name=body.name, code=body.code, clos=[x.model_dump() for x in body.clos], instructor_id=user.id)
    db.add(c)
    db.flush()
    audit(db, user, "course_created", "course", c.id)
    db.commit()
    db.refresh(c)
    return c


@router.get("", response_model=list[CourseOut])
def list_courses(db: Session = Depends(get_db), user: User = Depends(faculty_user)):
    q = db.query(Course)
    if user.role != "admin":
        q = q.filter(Course.instructor_id == user.id)
    return q.order_by(Course.created_at.desc()).all()


@router.get("/{course_id}", response_model=CourseOut)
def read_course(course_id: int, db: Session = Depends(get_db), user: User = Depends(faculty_user)):
    return get_course(db, course_id, user)


@router.put("/{course_id}", response_model=CourseOut)
def update_course(course_id: int, body: CourseIn, db: Session = Depends(get_db), user: User = Depends(faculty_user)):
    c = get_course(db, course_id, user)
    c.name, c.code = body.name, body.code
    c.clos = [x.model_dump() for x in body.clos]
    db.commit()
    db.refresh(c)
    return c


@router.delete("/{course_id}", status_code=204)
def delete_course(course_id: int, db: Session = Depends(get_db), user: User = Depends(faculty_user)):
    c = get_course(db, course_id, user)
    audit(db, user, "course_deleted", "course", c.id, {"name": c.name})
    db.delete(c)
    db.commit()


# ------------------------------------------------------------------ roster / enrollment


@router.post("/{course_id}/roster")
def import_roster(course_id: int, file: UploadFile = File(...), db: Session = Depends(get_db),
                  user: User = Depends(faculty_user)):
    """Excel/CSV: University ID | Student Name | Email | Section. Creates student accounts with one-time
    temporary passwords (returned ONCE in this response, never stored in clear)."""
    course = get_course(db, course_id, user)
    data = file.file.read(2 * 1024 * 1024 + 1)
    if len(data) > 2 * 1024 * 1024:
        raise HTTPException(413, "Roster file is larger than 2 MB")
    try:
        rows, warnings = parse_roster(file.filename or "", data)
    except ValueError as e:
        raise HTTPException(400, str(e))

    new_accounts, enrolled, already, skipped = [], 0, 0, list(warnings)
    for r in rows:
        uid = r["university_id"]
        acct = db.query(User).filter(User.university_id == uid).first()
        if acct is not None and acct.role != "student":
            skipped.append(f"{uid}: belongs to a {acct.role} account, skipped")
            continue
        st = db.query(Student).filter(Student.university_id == uid).first()
        if st is None:
            st = Student(name=r["name"] or uid, university_id=uid)
            db.add(st)
            db.flush()
        elif r["name"] and st.name == st.university_id:
            st.name = r["name"]  # replace the placeholder name created by a bulk upload
        if acct is None:
            temp = new_temp_password()
            db.add(User(university_id=uid, name=st.name, email=r["email"] or None, role="student",
                        password_hash=hash_password(temp), must_change_password=True, student_id=st.id))
            new_accounts.append({"university_id": uid, "name": st.name, "temporary_password": temp})
        enr = db.query(CourseEnrollment).filter_by(course_id=course.id, student_id=st.id).first()
        if enr is None:
            db.add(CourseEnrollment(course_id=course.id, student_id=st.id, section=r["section"] or None))
            enrolled += 1
        else:
            already += 1
            if r["section"]:
                enr.section = r["section"]
    audit(db, user, "roster_imported", "course", course.id,
          {"rows": len(rows), "enrolled": enrolled, "accounts_created": len(new_accounts)})
    db.commit()
    return {"enrolled": enrolled, "already_enrolled": already, "accounts_created": len(new_accounts),
            "new_accounts": new_accounts, "warnings": skipped}


@router.get("/{course_id}/students")
def list_students(course_id: int, db: Session = Depends(get_db), user: User = Depends(faculty_user)):
    course = get_course(db, course_id, user)
    accounts = {u.student_id: u for u in db.query(User).filter(User.role == "student").all()}
    rows = sorted(course.enrollments, key=lambda e: e.student.university_id)
    return [{"student_id": e.student_id, "university_id": e.student.university_id, "name": e.student.name,
             "section": e.section, "has_account": e.student_id in accounts,
             "must_change_password": bool(accounts.get(e.student_id) and accounts[e.student_id].must_change_password)}
            for e in rows]


@router.post("/{course_id}/students/{student_id}/reset-password")
def reset_student_password(course_id: int, student_id: int, db: Session = Depends(get_db),
                           user: User = Depends(faculty_user)):
    course = get_course(db, course_id, user)
    if not db.query(CourseEnrollment.id).filter_by(course_id=course.id, student_id=student_id).first():
        raise HTTPException(404, "Student is not enrolled in this course")
    acct = db.query(User).filter(User.student_id == student_id, User.role == "student").first()
    if acct is None:
        raise HTTPException(404, "This student has no account yet (import the roster first)")
    temp = new_temp_password()
    acct.password_hash, acct.must_change_password = hash_password(temp), True
    audit(db, user, "student_password_reset", "user", acct.id, {"course_id": course.id})
    db.commit()
    return {"university_id": acct.university_id, "temporary_password": temp}


@router.delete("/{course_id}/students/{student_id}", status_code=204)
def unenroll(course_id: int, student_id: int, db: Session = Depends(get_db), user: User = Depends(faculty_user)):
    course = get_course(db, course_id, user)
    enr = db.query(CourseEnrollment).filter_by(course_id=course.id, student_id=student_id).first()
    if enr is None:
        raise HTTPException(404, "Student is not enrolled in this course")
    audit(db, user, "student_unenrolled", "course", course.id, {"student_id": student_id})
    db.delete(enr)
    db.commit()
