"""Bootstrap the first accounts:  python -m app.cli create-user --id 1001 --name "Dr. Ahmed" --role faculty"""
import argparse
import getpass
import sys

from .database import SessionLocal, init_db
from .models import Student, User
from .security import hash_password, validate_new_password


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m app.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create-user", help="create a faculty/admin/student account")
    c.add_argument("--id", required=True, help="university ID (login name)")
    c.add_argument("--name", required=True)
    c.add_argument("--role", choices=["faculty", "admin", "student"], default="faculty")
    c.add_argument("--email")
    args = ap.parse_args()

    init_db()
    pw = getpass.getpass("Password (min 8 chars): ")
    if pw != getpass.getpass("Repeat password: "):
        sys.exit("Passwords do not match")
    try:
        validate_new_password(pw)
    except Exception as e:
        sys.exit(getattr(e, "detail", str(e)))

    db = SessionLocal()
    try:
        if db.query(User).filter(User.university_id == args.id).first():
            sys.exit("A user with this ID already exists")
        student_id = None
        if args.role == "student":
            st = db.query(Student).filter(Student.university_id == args.id).first() or Student(name=args.name, university_id=args.id)
            db.add(st)
            db.flush()
            student_id = st.id
        db.add(User(university_id=args.id, name=args.name, email=args.email, role=args.role,
                    password_hash=hash_password(pw), must_change_password=False, student_id=student_id))
        db.commit()
        print(f"Created {args.role} '{args.name}' ({args.id})")
    finally:
        db.close()


if __name__ == "__main__":
    main()
