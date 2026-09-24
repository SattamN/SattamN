"""Streamlit prototype UI.  Run:  streamlit run ui/app.py   (API must be running on API_URL)."""
import html
import os
from pathlib import Path

import pandas as pd
import requests
import streamlit as st

API = os.getenv("API_URL", "http://127.0.0.1:8000").rstrip("/")
ASSETS = Path(__file__).resolve().parent / "assets"
NBU_LOGO = ASSETS / "nbu_logo.jpeg"
CIVIL_ENG_LOGO = ASSETS / "civil_eng_logo.jpeg"

st.set_page_config(page_title="NBU AI", page_icon="🎓", layout="wide")


def show_header_logos():
    """University + Civil Engineering Department logos, side by side, above the page title."""
    if NBU_LOGO.exists() and CIVIL_ENG_LOGO.exists():
        c1, c2 = st.columns([1, 1])
        c1.image(str(NBU_LOGO), width="stretch")
        c2.image(str(CIVIL_ENG_LOGO), width="stretch")

st.markdown(
    "<style>.ans{direction:auto;unicode-bidi:plaintext;white-space:pre-wrap;background:#f6f8fa;"
    "border:1px solid #e1e4e8;border-radius:6px;padding:.6rem .8rem;color:#111}</style>",
    unsafe_allow_html=True,
)

FLAG_TEXT = {
    "low_confidence": "Low AI confidence",
    "model_flagged": "AI asked for review",
    "answer_unclear": "Answer unclear / illegible",
    "blank_but_marked": "Blank answer but marks given",
    "mark_out_of_range": "Mark outside allowed range",
    "max_mark_mismatch": "Max mark mismatch",
    "breakdown_sum_mismatch": "Rubric breakdown ≠ total",
    "rubric_criteria_mismatch": "Breakdown doesn't follow approved rubric",
    "empty_reason": "No reason given",
    "missing_from_ai_response": "AI returned nothing for this question",
    "duplicate_in_ai_response": "Duplicate result from AI",
}


# ------------------------------------------------------------------ API helpers


def _headers() -> dict:
    tok = st.session_state.get("token")
    return {"Authorization": f"Bearer {tok}"} if tok else {}


def call(method: str, path: str, **kw):
    timeout = kw.pop("timeout", 300)
    try:
        r = requests.request(method, f"{API}{path}", timeout=timeout, headers=_headers(), **kw)
    except requests.ConnectionError:
        st.error(f"Cannot reach the API at {API}. Start it with:  uvicorn app.main:app --reload")
        st.stop()
    if r.status_code == 401 and st.session_state.get("token"):
        st.session_state.clear()  # session expired or password changed elsewhere
        st.rerun()
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail", r.text)
        except Exception:
            detail = r.text
        st.error(f"{detail}")
        return None
    return r


def show_file(resp, name: str, key: str):
    """Render a stored paper page: image preview when possible, otherwise a download button."""
    if resp is None:
        return
    if "pdf" not in resp.headers.get("content-type", ""):
        try:
            st.image(resp.content, width="stretch")
            return
        except Exception:
            st.caption("Preview unavailable for this file.")
    st.download_button(f"Download {name}", resp.content, name, key=key)


def get(path: str, **kw):
    r = call("GET", path, **kw)
    return r.json() if r is not None else None



# ------------------------------------------------------------------ login / password change


def _store_session(d: dict):
    st.session_state.update(token=d["access_token"], role=d["role"], name=d["name"],
                            uid=d["university_id"], must_change=d["must_change_password"])


# A reset-password link looks like  http://.../?reset_token=XXXX  — handle it before anything else.
_reset_token = st.query_params.get("reset_token")
if _reset_token and not st.session_state.get("token"):
    show_header_logos()
    st.title("🎓 NBU AI")
    st.subheader("Set a new password")
    with st.form("reset"):
        new_pw = st.text_input("New password (min 8 characters)", type="password")
        new_pw2 = st.text_input("Repeat new password", type="password")
        if st.form_submit_button("Save new password", type="primary"):
            if new_pw != new_pw2:
                st.error("The two passwords do not match.")
            else:
                r = call("POST", "/auth/reset-password", json={"token": _reset_token, "new_password": new_pw})
                if r:
                    st.query_params.clear()
                    _store_session(r.json())
                    st.success("Password set. You're signed in.")
                    st.rerun()
    st.stop()

if not st.session_state.get("token"):
    show_header_logos()
    st.title("🎓 NBU AI")
    tab_in, tab_up, tab_forgot = st.tabs(["Sign in", "Create a faculty account", "Forgot password"])

    with tab_in:
        with st.form("login"):
            uid_in = st.text_input("University ID")
            pw_in = st.text_input("Password", type="password")
            if st.form_submit_button("Sign in", type="primary"):
                try:
                    r = requests.post(f"{API}/auth/login", json={"university_id": uid_in, "password": pw_in}, timeout=30)
                except requests.ConnectionError:
                    st.error(f"Cannot reach the API at {API}.")
                    st.stop()
                if r.status_code == 200:
                    _store_session(r.json())
                    st.rerun()
                else:
                    st.error(r.json().get("detail", "Sign-in failed"))

    with tab_up:
        st.caption("Each instructor sets up their own account. You'll only ever see courses you create yourself.")
        with st.form("register"):
            r_id = st.text_input("Choose a university/staff ID")
            r_name = st.text_input("Full name")
            r_email = st.text_input("Email (used for password reset)")
            r_pw = st.text_input("Choose a password (min 8 characters)", type="password")
            r_pw2 = st.text_input("Repeat password", type="password")
            if st.form_submit_button("Create account", type="primary"):
                if r_pw != r_pw2:
                    st.error("The two passwords do not match.")
                elif not (r_id.strip() and r_name.strip() and r_email.strip() and r_pw):
                    st.error("Fill in every field.")
                else:
                    try:
                        rr = requests.post(f"{API}/auth/register", json={
                            "university_id": r_id.strip(), "name": r_name.strip(),
                            "email": r_email.strip(), "password": r_pw,
                        }, timeout=30)
                    except requests.ConnectionError:
                        st.error(f"Cannot reach the API at {API}.")
                        st.stop()
                    if rr.status_code == 201:
                        _store_session(rr.json())
                        st.rerun()
                    else:
                        detail = rr.json().get("detail", "Could not create the account")
                        st.error(detail if isinstance(detail, str) else "Please check the form and try again.")

    with tab_forgot:
        st.caption("Enter your university ID; if an email is on file, a reset link will be sent to it.")
        with st.form("forgot"):
            f_id = st.text_input("University ID", key="forgot_id")
            if st.form_submit_button("Send reset link"):
                try:
                    rr = requests.post(f"{API}/auth/forgot-password", json={"university_id": f_id.strip()}, timeout=30)
                    st.success(rr.json().get("message", "If an account exists, a reset link has been sent."))
                except requests.ConnectionError:
                    st.error(f"Cannot reach the API at {API}.")
    st.stop()

if st.session_state.get("must_change"):
    st.title("Choose a new password")
    st.info("You are using a temporary password. Set your own to continue.")
    with st.form("chpw"):
        cur_pw = st.text_input("Temporary password", type="password")
        new_pw = st.text_input("New password (min 8 characters)", type="password")
        new_pw2 = st.text_input("Repeat new password", type="password")
        if st.form_submit_button("Save password", type="primary"):
            if new_pw != new_pw2:
                st.error("The two passwords do not match.")
            else:
                r = call("POST", "/auth/change-password", json={"current_password": cur_pw, "new_password": new_pw})
                if r:
                    _store_session(r.json())
                    st.rerun()
    st.stop()

if NBU_LOGO.exists():
    st.sidebar.image(str(NBU_LOGO), width="stretch")
st.sidebar.title("🎓 NBU AI")
st.sidebar.caption(f"{st.session_state['name']} ({st.session_state['role']})")
if st.sidebar.button("Sign out"):
    st.session_state.clear()
    st.rerun()

nt = get("/notifications") or {"unread": 0, "items": []}
with st.sidebar.expander(f"🔔 Notifications ({nt['unread']} new)", expanded=False):
    if not nt["items"]:
        st.caption("Nothing yet.")
    for n_ in nt["items"][:10]:
        st.markdown(("**" if not n_["read"] else "") + n_["title"] + ("**" if not n_["read"] else ""))
        if n_["body"]:
            st.caption(n_["body"])
    if nt["unread"] and st.button("Mark all as read"):
        call("POST", "/notifications/read-all")
        st.rerun()

# ------------------------------------------------------------------ student portal

if st.session_state["role"] == "student":
    st.header("My courses")
    my = get("/me/courses") or []
    if not my:
        st.info("You are not enrolled in any course yet.")
        st.stop()
    crs = st.selectbox("Course", my, format_func=lambda c: f"{c.get('code') or ''} {c['name']}".strip())
    st.caption(f"Instructor: {crs['instructor']}" + (f" · Section {crs['section']}" if crs.get("section") else ""))
    grades = get(f"/me/courses/{crs['id']}/grades") or []
    if not grades:
        st.info("No grades have been released for this course yet.")
    for ex in grades:
        with st.container(border=True):
            st.subheader(ex["title"])
            c1, c2 = st.columns([1, 3])
            c1.metric("Total", f"{ex['total']:g} / {ex['total_max']:g}", f"{ex['percent']}%" if ex["percent"] is not None else None, delta_color="off")
            c2.dataframe(pd.DataFrame([{"Question": f"Q{q['number']}", "Mark": q["mark"], "Out of": q["max_mark"]} for q in ex["questions"]]),
                         hide_index=True, width="stretch")
            rv = ex["review"]
            if rv["can_request"]:
                with st.expander("Request a review of my marks"):
                    st.caption(f"Open until {str(rv['window_closes_at'])[:10]}. You can send one request per exam, so include every question you want checked.")
                    nums = st.multiselect("Questions", [q["number"] for q in ex["questions"]], format_func=lambda n: f"Q{n}", key=f"rq{ex['exam_id']}")
                    msg = st.text_area("Explain why (at least 10 characters)", max_chars=2000, key=f"rm{ex['exam_id']}")
                    if st.button("Submit request", key=f"rs{ex['exam_id']}", type="primary"):
                        if call("POST", f"/me/exams/{ex['exam_id']}/review-requests", json={"question_numbers": nums, "message": msg}):
                            st.rerun()
            elif rv["request_id"]:
                st.caption(f"Review request: {rv['request_status']}")
    st.caption("Only marks approved and released by your instructor are shown.")

    st.subheader("Assignments")
    assigns = get(f"/me/courses/{crs['id']}/assignments") or []
    if not assigns:
        st.caption("No assignments in this course yet.")
    for asg in assigns:
        with st.container(border=True):
            c1, c2 = st.columns([3, 1])
            c1.markdown(f"**{asg['title']}**" + (f" — due {str(asg['due_at'])[:16]}" if asg["due_at"] else ""))
            if asg["mark"] is not None:
                c2.metric("Mark", f"{asg['mark']:g} / {asg['max_mark']:g}")
            else:
                c2.caption({"not_submitted": "Not submitted", "uploaded": "Submitted, awaiting grading",
                           "queued": "Grading queued…", "grading": "Grading…", "graded": "Graded, pending approval",
                           "approved": "Approved, awaiting release", "failed": "Grading failed"}.get(asg["status"], asg["status"]))
                if asg["late"]:
                    c2.caption("⚠️ submitted late")
            if asg["status"] in ("not_submitted", "uploaded"):
                files = st.file_uploader("Upload your answer (PDF or photos)", type=["pdf", "png", "jpg", "jpeg", "webp"],
                                         accept_multiple_files=True, key=f"asub{asg['id']}")
                if files and st.button("Submit", key=f"asubbtn{asg['id']}"):
                    up = [("files", (f.name, f.getvalue(), f.type)) for f in files]
                    if call("POST", f"/me/assignments/{asg['id']}/submit", files=up):
                        st.success("Submitted.")
                        st.rerun()

    mine = get("/me/review-requests") or []
    if mine:
        st.subheader("My review requests")
    for rq in mine:
        icon = {"pending": "🕓", "decided": "✅", "rejected": "⛔"}[rq["status"]]
        with st.expander(f"{icon} {rq['exam_title']} — {rq['status']}", expanded=rq["status"] != "pending"):
            st.markdown(f"<div class='ans'>{html.escape(rq['message'])}</div>", unsafe_allow_html=True)
            st.dataframe(pd.DataFrame([{"Question": f"Q{i['number']}", "Mark when requested": i["original_mark"],
                                        "Decision": i["decision"], "New mark": i["new_mark"], "Instructor note": i["note"]}
                                       for i in rq["items"]]), hide_index=True, width="stretch")
            if rq["response_message"]:
                st.caption("Instructor's response")
                st.markdown(f"<div class='ans'>{html.escape(rq['response_message'])}</div>", unsafe_allow_html=True)
            if rq["paper_access"]:
                if st.toggle("View my paper", key=f"vp{rq['id']}"):
                    pp = get(f"/me/review-requests/{rq['id']}/paper")
                    if pp:
                        st.metric("Total", f"{pp['total']:g} / {pp['total_max']:g}")
                        for i in range(pp["n_files"]):
                            show_file(call("GET", f"/me/review-requests/{rq['id']}/file/{i}"), f"my_paper_{i+1}", f"pf{rq['id']}{i}")
    st.stop()

# ------------------------------------------------------------------ sidebar: course / exam selection

health = get("/health") or {}
if health and not health.get("ai_configured"):
    st.sidebar.warning("ANTHROPIC_API_KEY is not set on the server — AI actions will fail.")

courses = get("/courses") or []
course = None
if courses:
    course = st.sidebar.selectbox("Course", courses, format_func=lambda c: f"{c.get('code') or ''} {c['name']}".strip())
exams = (get(f"/courses/{course['id']}/exams") or []) if course else []
exam = None
if exams:
    exam = st.sidebar.selectbox("Exam", exams, format_func=lambda e: e["title"])

_pending = len(get("/review-requests", params={"status": "pending"}) or [])
page = st.sidebar.radio(
    "Step",
    ["Course & Exam", "Students", "Questions & Rubrics", "Submissions & Grading", "Review",
     "Gradebook & Export", f"Review requests ({_pending})" if _pending else "Review requests",
     "Assignments"],
)


def need_exam():
    if not exam:
        st.info("Create/select a course and an exam first (Course & Exam).")
        st.stop()


# ------------------------------------------------------------------ 1. course & exam

if page == "Course & Exam":
    st.header("Course & Exam")
    c1, c2 = st.columns(2)
    with c1:
        st.subheader("New course")
        with st.form("course"):
            name = st.text_input("Course name")
            code = st.text_input("Course code (optional)")
            if st.form_submit_button("Create course") and name.strip():
                if call("POST", "/courses", json={"name": name.strip(), "code": code.strip() or None}):
                    st.rerun()
    with c2:
        st.subheader("New exam")
        if not course:
            st.info("Create a course first.")
        else:
            with st.form("exam"):
                title = st.text_input("Exam title", placeholder="Midterm")
                ef = st.file_uploader("Exam paper (PDF/image)", type=["pdf", "png", "jpg", "jpeg", "webp"], key="ef")
                mf = st.file_uploader("Model answer (PDF/image, optional)", type=["pdf", "png", "jpg", "jpeg", "webp"], key="mf")
                if st.form_submit_button("Create exam") and title.strip():
                    files = {}
                    if ef:
                        files["exam_file"] = (ef.name, ef.getvalue(), ef.type)
                    if mf:
                        files["model_answer_file"] = (mf.name, mf.getvalue(), mf.type)
                    if call("POST", f"/courses/{course['id']}/exams", data={"title": title.strip()}, files=files or None):
                        st.rerun()
    if exams:
        st.subheader("Exams in this course")
        st.dataframe(pd.DataFrame(exams)[["id", "title", "question_count", "total_marks", "rubrics_approved",
                                            "has_exam_file", "has_model_answer_file"]], hide_index=True, width="stretch")

# ------------------------------------------------------------------ students / roster

elif page == "Students":
    if not course:
        st.info("Create a course first.")
        st.stop()
    st.header(f"Students — {course['name']}")
    st.caption("Upload an Excel/CSV with: University ID | Student Name | Email | Section (Arabic headers work too). "
               "Each new student gets a temporary password that is shown only once.")
    up = st.file_uploader("Class roster", type=["xlsx", "csv"], key="roster")
    if up and st.button("Import roster", type="primary"):
        r = call("POST", f"/courses/{course['id']}/roster", files={"file": (up.name, up.getvalue(), up.type)})
        if r:
            res = r.json()
            st.success(f"Enrolled {res['enrolled']} new, {res['already_enrolled']} already enrolled, {res['accounts_created']} accounts created.")
            for w in res["warnings"]:
                st.warning(w)
            if res["new_accounts"]:
                df = pd.DataFrame(res["new_accounts"])
                st.session_state["new_accounts_csv"] = df.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")
                st.session_state["new_accounts_n"] = len(df)
    if st.session_state.get("new_accounts_csv"):
        st.warning(f"{st.session_state['new_accounts_n']} temporary passwords were generated. Download and hand them out now — they cannot be shown again.")
        st.download_button("⬇️ Download temporary passwords (CSV)", st.session_state["new_accounts_csv"], "temporary_passwords.csv")
        if st.button("I have saved them — clear from screen"):
            st.session_state.pop("new_accounts_csv", None)
            st.rerun()
    roster = get(f"/courses/{course['id']}/students") or []
    if roster:
        st.dataframe(pd.DataFrame(roster).drop(columns=["student_id"]), hide_index=True, width="stretch")
        pick = st.selectbox("Reset a student's password", roster, format_func=lambda x: f"{x['university_id']} — {x['name']}")
        if st.button("Reset password"):
            r = call("POST", f"/courses/{course['id']}/students/{pick['student_id']}/reset-password")
            if r:
                st.code(f"{r.json()['university_id']}  /  {r.json()['temporary_password']}")
                st.caption("Give this to the student; they must change it at first sign-in.")

# ------------------------------------------------------------------ questions & rubrics

elif page == "Questions & Rubrics":
    need_exam()
    st.header(f"Questions & Rubrics — {exam['title']}")
    qs = get(f"/exams/{exam['id']}/questions") or []

    a, b = st.columns([1, 1])
    with a:
        if exam["has_exam_file"]:
            if st.button("🤖 Extract questions + suggest rubrics from the exam file", width="stretch"):
                with st.spinner("Reading the exam…"):
                    if call("POST", f"/exams/{exam['id']}/extract-questions", params={"replace": bool(qs)}):
                        st.rerun()
        else:
            st.caption("Upload an exam file on the Course & Exam page to enable AI extraction, or add questions manually.")
    with b:
        if qs and st.button("✅ Approve all valid rubrics", width="stretch"):
            res = call("POST", f"/exams/{exam['id']}/rubrics/approve-all")
            if res:
                info = res.json()
                if info["needs_attention"]:
                    st.warning(f"Rubric missing/invalid for questions: {info['needs_attention']}")
                st.rerun()

    with st.expander("➕ Add a question manually"):
        with st.form("addq"):
            n = st.number_input("Number", 1, 200, len(qs) + 1)
            txt = st.text_area("Question text")
            ma = st.text_area("Model answer")
            mm = st.number_input("Max mark", 0.25, 100.0, 10.0, 0.25)
            clo = st.text_input("CLO (optional)")
            if st.form_submit_button("Add"):
                if call("POST", f"/exams/{exam['id']}/questions",
                        json={"number": int(n), "text": txt, "model_answer": ma, "max_mark": mm, "clo": clo or None}):
                    st.rerun()

    for q in qs:
        badge = "✅ approved" if q["rubric_approved"] else ("📝 needs approval" if q["rubric"] else "⚠️ no rubric")
        with st.expander(f"Q{q['number']} — {q['max_mark']:g} marks — {badge}", expanded=not q["rubric_approved"]):
            st.markdown(f"<div class='ans'>{html.escape(q['text'] or '(no text)')}</div>", unsafe_allow_html=True)
            if q["model_answer"]:
                st.caption("Model answer")
                st.markdown(f"<div class='ans'>{html.escape(q['model_answer'])}</div>", unsafe_allow_html=True)
            rub = (q["rubric"] or {}).get("criteria", [])
            df = pd.DataFrame(rub or [{"criterion": "", "max_mark": 0.0, "description": ""}])
            edited = st.data_editor(df, num_rows="dynamic", key=f"rub{q['id']}", width="stretch",
                                    column_config={"max_mark": st.column_config.NumberColumn(min_value=0.0, step=0.25)})
            total = float(pd.to_numeric(edited["max_mark"], errors="coerce").fillna(0).sum())
            (st.success if abs(total - q["max_mark"]) < 0.01 else st.warning)(f"Criteria total: {total:g} / {q['max_mark']:g}")
            c1, c2, c3 = st.columns(3)
            if c1.button("Save & approve rubric", key=f"ap{q['id']}"):
                crit = [
                    {"criterion": str(r["criterion"]).strip(), "max_mark": float(r["max_mark"]),
                     "description": str(r.get("description") or "")}
                    for r in edited.to_dict("records") if str(r["criterion"]).strip()
                ]
                if call("PUT", f"/questions/{q['id']}/rubric", json={"criteria": crit}):
                    st.rerun()
            if c2.button("🤖 Suggest with AI", key=f"ai{q['id']}"):
                with st.spinner("Generating…"):
                    if call("POST", f"/questions/{q['id']}/rubric/generate", params={"mode": "ai"}):
                        st.rerun()
            if c3.button("Use default template", key=f"df{q['id']}"):
                if call("POST", f"/questions/{q['id']}/rubric/generate", params={"mode": "default"}):
                    st.rerun()

# ------------------------------------------------------------------ 3. submissions & grading

elif page == "Submissions & Grading":
    need_exam()
    st.header(f"Submissions & Grading — {exam['title']}")
    c1, c2 = st.columns(2)
    with c1:
        st.subheader("One student")
        with st.form("one"):
            sname = st.text_input("Student name")
            sid_ = st.text_input("University ID")
            fl = st.file_uploader("Answer pages (PDF or photos)", type=["pdf", "png", "jpg", "jpeg", "webp"],
                                  accept_multiple_files=True)
            if st.form_submit_button("Upload") and sid_.strip() and fl:
                files = [("files", (f.name, f.getvalue(), f.type)) for f in fl]
                if call("POST", f"/exams/{exam['id']}/submissions", data={"student_name": sname, "university_id": sid_},
                        files=files):
                    st.rerun()
    with c2:
        st.subheader("Bulk (file name = university ID)")
        with st.form("bulk"):
            bl = st.file_uploader("e.g. 441001.pdf, 441002.pdf …", type=["pdf", "png", "jpg", "jpeg", "webp"],
                                  accept_multiple_files=True, key="bulk")
            if st.form_submit_button("Upload all") and bl:
                r = call("POST", f"/exams/{exam['id']}/submissions/bulk", files=[("files", (f.name, f.getvalue(), f.type)) for f in bl])
                if r:
                    res = r.json()
                    st.success(f"Uploaded {len(res['created'])} papers")
                    if res["skipped"]:
                        st.warning(res["skipped"])

    st.divider()

    @st.fragment(run_every="4s")
    def status_panel():
        stt = get(f"/exams/{exam['id']}/grading-status") or {"total": 0, "counts": {}}
        total, counts = stt["total"], stt["counts"]
        done = counts.get("graded", 0) + counts.get("approved", 0)
        st.progress(done / total if total else 0.0, text=f"{done}/{total} graded · " +
                    " · ".join(f"{k}: {v}" for k, v in counts.items()))
        subs = get(f"/exams/{exam['id']}/submissions") or []
        if subs:
            df = pd.DataFrame(subs)[["university_id", "student_name", "status", "n_files", "n_graded", "n_pending_review", "total", "error"]]
            st.dataframe(df, hide_index=True, width="stretch")

    status_panel()
    g1, g2 = st.columns(2)
    if g1.button("🤖 Grade all pending papers", type="primary", width="stretch"):
        r = call("POST", f"/exams/{exam['id']}/grade", json={})
        if r:
            st.success(f"Queued {r.json()['queued']} paper(s). Progress updates automatically.")
    if g2.button("↻ Retry failed papers", width="stretch"):
        r = call("POST", f"/exams/{exam['id']}/grade", json={"force": False})
        if r:
            st.info(f"Queued {r.json()['queued']}")

# ------------------------------------------------------------------ 4. review

elif page == "Review":
    need_exam()
    st.header(f"Review — {exam['title']}")
    subs = [s for s in (get(f"/exams/{exam['id']}/submissions") or []) if s["status"] in ("graded", "approved")]
    if not subs:
        st.info("No graded papers yet.")
        st.stop()

    top = st.columns([3, 1])
    with top[1]:
        if st.button("✅ Approve all papers with no flags", width="stretch"):
            r = call("POST", f"/exams/{exam['id']}/approve-unflagged")
            if r:
                st.success(r.json())
                st.rerun()
    subs.sort(key=lambda s: (s["status"] == "approved", -s["n_pending_review"], s["university_id"]))
    sub = top[0].selectbox(
        "Paper", subs,
        format_func=lambda s: f"{'✅' if s['status']=='approved' else ('🔴 ' + str(s['n_pending_review']) if s['n_pending_review'] else '🟢')}  "
                              f"{s['university_id']} — {s['student_name']}",
    )
    rv = get(f"/submissions/{sub['id']}/review")
    locked = rv["status"] == "approved"

    left, right = st.columns([2, 3])
    with left:
        st.subheader("Student paper")
        for i in range(rv["n_files"]):
            show_file(call("GET", f"/submissions/{sub['id']}/file/{i}"), f"paper_{sub['university_id']}_{i+1}", f"dl{i}")
    with right:
        st.subheader(f"{rv['student_name']} — {rv['university_id']}")
        st.metric("Total (current)", f"{rv['total_current'] if rv['total_current'] is not None else '—'} / {rv['total_max']:g}")
        for g in rv["grades"]:
            flagged = g["needs_review"] and g["decision"] == "pending"
            icon = "🔴" if flagged else ("✅" if g["decision"] != "pending" else "🟢")
            conf = f"{g['ai_confidence']:.2f}" if g["ai_confidence"] is not None else "—"
            title = f"{icon} Q{g['question_number']} — AI: {g['ai_mark'] if g['ai_mark'] is not None else '—'} / {g['max_mark']:g}  (conf {conf})"
            with st.expander(title, expanded=flagged):
                if g["review_flags"]:
                    st.warning(" • ".join(FLAG_TEXT.get(f, f) for f in g["review_flags"]))
                st.caption("Student answer (AI transcription)")
                st.markdown(f"<div class='ans'>{html.escape(g['ai_answer_text'] or '(nothing transcribed)')}</div>", unsafe_allow_html=True)
                st.caption("AI reasoning")
                st.write(g["ai_reason"])
                if g["ai_rubric_breakdown"]:
                    st.dataframe(pd.DataFrame(g["ai_rubric_breakdown"]), hide_index=True, width="stretch")
                base = g["final_mark"] if g["final_mark"] is not None else (g["ai_mark"] or 0.0)
                c1, c2 = st.columns([1, 2])
                mark = c1.number_input("Final mark", 0.0, float(g["max_mark"]), float(base), 0.25, key=f"m{g['id']}", disabled=locked)
                note = c2.text_input("Note (optional)", g["reviewer_note"] or "", key=f"n{g['id']}", disabled=locked)
                b1, b2, b3 = st.columns(3)
                if not locked:
                    if b1.button("Accept AI mark", key=f"a{g['id']}", disabled=g["ai_mark"] is None):
                        if call("POST", f"/grades/{g['id']}/accept-ai"):
                            st.rerun()
                    if b2.button("Save my mark", key=f"s{g['id']}"):
                        if call("PATCH", f"/grades/{g['id']}", json={"final_mark": mark, "reviewer_note": note or None}):
                            st.rerun()
                    if b3.button("Re-grade this question", key=f"r{g['id']}"):
                        with st.spinner("Re-grading…"):
                            if call("POST", f"/grades/{g['id']}/regrade", params={"force": g["decision"] != "pending"}):
                                st.rerun()
                st.caption(f"Decision: {g['decision']}")
        st.divider()
        if locked:
            if st.button("Reopen for editing"):
                if call("POST", f"/submissions/{sub['id']}/reopen"):
                    st.rerun()
        elif st.button("✅ Approve this paper", type="primary"):
            if call("POST", f"/submissions/{sub['id']}/approve"):
                st.rerun()

# ------------------------------------------------------------------ review requests (appeals)

elif page.startswith("Review requests"):
    st.header("Review requests")
    reqs = get("/review-requests") or []
    if not reqs:
        st.info("No review requests yet. Students can file them after you publish grades.")
        st.stop()
    pick = st.selectbox("Request", reqs, format_func=lambda r: f"{'🔴' if r['status']=='pending' else '✅'} {r['course']}, {r['exam']}: "
                                                              f"{r['university_id']} {r['student']} (Q{', Q'.join(map(str, r['questions']))})")
    d = get(f"/review-requests/{pick['id']}")
    pending_ = d["status"] == "pending"
    st.subheader(f"{d['student']['name']} — {d['student']['university_id']}")
    st.caption("The student's message (untrusted text)")
    st.markdown(f"<div class='ans'>{html.escape(d['message'])}</div>", unsafe_allow_html=True)
    with st.expander("Student paper"):
        for i in range(d["n_files"]):
            show_file(call("GET", f"/submissions/{d['submission_id']}/file/{i}"), f"paper_{i+1}", f"rp{i}")

    if pending_ and st.button("🤖 Get an independent AI second opinion", help="Advisory only. It never changes a mark."):
        with st.spinner("Re-grading the requested questions…"):
            if call("POST", f"/review-requests/{d['id']}/ai-assist"):
                st.rerun()

    decisions = {}
    for it in d["items"]:
        so = it["second_opinion"]
        with st.container(border=True):
            st.markdown(f"**Q{it['number']}** — current mark **{it['current_mark']:g} / {it['max_mark']:g}**"
                        + (f" (was {it['original_mark']:g} when requested)" if it["current_mark"] != it["original_mark"] else ""))
            st.caption("What the student wrote (AI transcription)")
            st.markdown(f"<div class='ans'>{html.escape(it['student_answer'] or '(nothing transcribed)')}</div>", unsafe_allow_html=True)
            st.caption("Original AI reasoning")
            st.write(it["original_reason"])
            if it["original_breakdown"]:
                st.dataframe(pd.DataFrame(it["original_breakdown"]), hide_index=True, width="stretch")
            if so:
                diff = so["difference"]
                st.info(f"AI second opinion: **{so['mark'] if so['mark'] is not None else '—'} / {it['max_mark']:g}**"
                        + (f" ({diff:+g} vs. original)" if diff is not None else "")
                        + f", confidence {so['confidence']:.2f}" if so["confidence"] is not None else "AI second opinion")
                if so["flags"]:
                    st.warning(" • ".join(FLAG_TEXT.get(f, f) for f in so["flags"]))
                st.write(so["reason"])
                if so["breakdown"]:
                    st.dataframe(pd.DataFrame(so["breakdown"]), hide_index=True, width="stretch")
            if pending_:
                choice = st.radio("Your decision", ["Keep current mark", "Change mark"], key=f"dc{it['question_id']}", horizontal=True)
                newm = st.number_input("New mark", 0.0, float(it["max_mark"]), float(so["mark"] if so and so["mark"] is not None else it["current_mark"] or 0.0),
                                       0.25, key=f"dm{it['question_id']}", disabled=choice == "Keep current mark")
                note = st.text_input("Note for the student (optional)", key=f"dn{it['question_id']}")
                decisions[it["question_id"]] = (choice, newm, note)
            else:
                st.success(f"Decision: {it['decision']}" + (f" → {it['new_mark']:g}" if it["new_mark"] is not None else "")
                           + (f" — {it['note']}" if it["note"] else ""))
    if pending_:
        resp_msg = st.text_area("Message to the student (required)", key="rr_msg", max_chars=2000)
        grant = st.checkbox("Let the student view their own paper", key="rr_grant")
        c1, c2 = st.columns(2)
        if c1.button("Submit decision", type="primary", width="stretch"):
            items = [{"question_id": qid, "decision": "change" if ch == "Change mark" else "keep",
                      "new_mark": nm if ch == "Change mark" else None, "note": nt_ or None}
                     for qid, (ch, nm, nt_) in decisions.items()]
            if call("POST", f"/review-requests/{d['id']}/decide", json={"items": items, "response_message": resp_msg, "grant_paper_access": grant}):
                st.rerun()
        if c2.button("Reject request", width="stretch"):
            if call("POST", f"/review-requests/{d['id']}/reject", json={"response_message": resp_msg}):
                st.rerun()
    else:
        st.caption("Your message to the student")
        st.markdown(f"<div class='ans'>{html.escape(d['response_message'] or '')}</div>", unsafe_allow_html=True)
        if d["status"] == "decided":
            new_val = st.toggle("Student may view their own paper", value=d["paper_access"], key=f"pa{d['id']}")
            if new_val != d["paper_access"]:
                if call("POST", f"/review-requests/{d['id']}/paper-access", params={"grant": new_val}):
                    st.rerun()

# ------------------------------------------------------------------ assignments

elif page == "Assignments":
    if not course:
        st.info("Create a course first.")
        st.stop()
    st.header(f"Assignments — {course['name']}")
    assignments = get(f"/courses/{course['id']}/assignments") or []

    with st.expander("➕ New assignment"):
        with st.form("newassign"):
            title = st.text_input("Title", placeholder="Homework 1")
            desc = st.text_area("Description / task")
            mm = st.number_input("Max mark", 0.25, 100.0, 10.0, 0.25)
            due = st.date_input("Due date (optional)", value=None)
            allow_late = st.checkbox("Allow late submissions")
            if st.form_submit_button("Create") and title.strip():
                body = {"title": title.strip(), "description": desc, "max_mark": mm, "allow_late": allow_late}
                if due:
                    body["due_at"] = f"{due}T23:59:00Z"
                if call("POST", f"/courses/{course['id']}/assignments", json=body):
                    st.rerun()

    if not assignments:
        st.info("No assignments yet.")
        st.stop()
    a = st.selectbox("Assignment", assignments, format_func=lambda x: x["title"])

    rub_col, act_col = st.columns([2, 1])
    with rub_col:
        st.subheader("Rubric")
        badge = "✅ approved" if a["rubric_approved"] else ("📝 needs approval" if a["rubric"] else "⚠️ no rubric")
        st.caption(badge)
        crit = (a["rubric"] or {}).get("criteria", [])
        df = pd.DataFrame(crit or [{"criterion": "", "max_mark": 0.0, "description": ""}])
        edited = st.data_editor(df, num_rows="dynamic", key=f"arub{a['id']}", width="stretch",
                                column_config={"max_mark": st.column_config.NumberColumn(min_value=0.0, step=0.25)})
        total = float(pd.to_numeric(edited["max_mark"], errors="coerce").fillna(0).sum())
        (st.success if abs(total - a["max_mark"]) < 0.01 else st.warning)(f"Criteria total: {total:g} / {a['max_mark']:g}")
        b1, b2, b3 = st.columns(3)
        if b1.button("Save & approve", key=f"aap{a['id']}"):
            crit_rows = [{"criterion": str(r["criterion"]).strip(), "max_mark": float(r["max_mark"]),
                         "description": str(r.get("description") or "")} for r in edited.to_dict("records") if str(r["criterion"]).strip()]
            if call("PUT", f"/assignments/{a['id']}/rubric", json={"criteria": crit_rows}):
                st.rerun()
        if b2.button("🤖 AI suggest", key=f"aai{a['id']}"):
            if call("POST", f"/assignments/{a['id']}/rubric/generate", params={"mode": "ai"}):
                st.rerun()
        if b3.button("Default template", key=f"adf{a['id']}"):
            if call("POST", f"/assignments/{a['id']}/rubric/generate", params={"mode": "default"}):
                st.rerun()
    with act_col:
        st.subheader("Results")
        st.markdown(("🟢 released to students" if a["published"] else "⚪ not released yet"))
        if not a["published"]:
            if st.button("Release results", type="primary"):
                if call("POST", f"/assignments/{a['id']}/release"):
                    st.rerun()
        elif st.button("Unrelease"):
            if call("POST", f"/assignments/{a['id']}/unrelease"):
                st.rerun()
        if a["due_at"]:
            st.caption(f"Due: {str(a['due_at'])[:16]}" + (" (late allowed)" if a["allow_late"] else ""))

    st.divider()
    st.subheader("Submissions")
    subs = get(f"/assignments/{a['id']}/submissions") or []
    if subs:
        st.dataframe(pd.DataFrame(subs)[["university_id", "student_name", "status", "late", "ai_mark", "final_mark", "needs_review"]],
                     hide_index=True, width="stretch")
    g1, g2 = st.columns(2)
    if g1.button("🤖 Grade all pending", type="primary"):
        r = call("POST", f"/assignments/{a['id']}/grade")
        if r:
            st.success(f"Queued {r.json()['queued']} submission(s).")
    if g2.button("✅ Approve all with no flags"):
        r = call("POST", f"/assignments/{a['id']}/approve-unflagged")
        if r:
            st.success(r.json())
            st.rerun()

    if subs:
        pick = st.selectbox("Review a submission", subs,
                            format_func=lambda s: f"{'🔴' if s['needs_review'] else '🟢'} {s['university_id']} — {s['student_name']}")
        if pick["status"] in ("graded", "approved"):
            rv = get(f"/assignment-submissions/{pick['id']}/review")
            locked = rv["status"] == "approved"
            for i in range(rv.get("n_files", 0)):
                show_file(call("GET", f"/assignment-submissions/{pick['id']}/file/{i}"), f"{pick['university_id']}_{i+1}", f"af{pick['id']}{i}")
            st.caption("Student answer (AI transcription)")
            st.markdown(f"<div class='ans'>{html.escape(rv['ai_answer_text'] or '(nothing transcribed)')}</div>", unsafe_allow_html=True)
            if rv["review_flags"]:
                st.warning(" • ".join(FLAG_TEXT.get(f, f) for f in rv["review_flags"]))
            st.caption("AI reasoning")
            st.write(rv["ai_reason"])
            if rv["ai_rubric_breakdown"]:
                st.dataframe(pd.DataFrame(rv["ai_rubric_breakdown"]), hide_index=True, width="stretch")
            base = rv["final_mark"] if rv["final_mark"] is not None else (rv["ai_mark"] or 0.0)
            mark = st.number_input("Final mark", 0.0, float(rv["max_mark"]), float(base), 0.25, key=f"am{pick['id']}", disabled=locked)
            note = st.text_input("Note (optional)", rv["reviewer_note"] or "", key=f"an{pick['id']}", disabled=locked)
            c1, c2, c3 = st.columns(3)
            if not locked:
                if c1.button("Accept AI mark", key=f"aacc{pick['id']}", disabled=rv["ai_mark"] is None):
                    if call("POST", f"/assignment-submissions/{pick['id']}/accept-ai"):
                        st.rerun()
                if c2.button("Save mark", key=f"asave{pick['id']}"):
                    if call("PATCH", f"/assignment-submissions/{pick['id']}", json={"final_mark": mark, "reviewer_note": note or None}):
                        st.rerun()
                if c3.button("Approve", key=f"aappr{pick['id']}"):
                    if call("POST", f"/assignment-submissions/{pick['id']}/approve"):
                        st.rerun()
            else:
                if st.button("Reopen"):
                    if call("POST", f"/assignment-submissions/{pick['id']}/reopen"):
                        st.rerun()
        else:
            st.info(f"Status: {pick['status']}. Grade it first.")

# ------------------------------------------------------------------ gradebook & export

else:
    need_exam()
    st.header(f"Gradebook — {exam['title']}")
    inc = st.toggle("Include papers not yet approved (provisional, AI marks)", value=False)
    gb = get(f"/exams/{exam['id']}/gradebook", params={"include_unapproved": inc})
    if gb and gb["rows"]:
        qn = [str(q["number"]) for q in gb["questions"]]
        df = pd.DataFrame(
            [{"University ID": r["university_id"], "Student": r["student_name"],
              **{f"Q{n}": r["marks"][n] for n in qn}, "Total": r["total"], "%": r["percent"],
              "Status": "provisional" if r["provisional"] else "approved"} for r in gb["rows"]]
        )
        st.dataframe(df, hide_index=True, width="stretch")
        s = gb["stats"]
        if s.get("count"):
            m = st.columns(5)
            for col, (label, key) in zip(m, [("Mean", "mean"), ("Median", "median"), ("Std dev", "stdev"), ("Highest", "max"), ("Lowest", "min")]):
                col.metric(label, s[key])
        st.divider()
        st.subheader("Bulk-edit marks in Excel")
        st.caption("Download the editable copy, change marks in Excel (only the white cells are unlocked), "
                   "then re-upload it here. Nothing is changed until you review the differences and confirm.")
        c1, c2 = st.columns(2)
        ed = call("GET", f"/exams/{exam['id']}/gradebook/editable-xlsx")
        if ed is not None:
            c1.download_button("⬇️ Download editable Excel", ed.content, f"gradebook_exam{exam['id']}_editable.xlsx",
                               key="dl_editable")
        up = c2.file_uploader("Re-upload the edited file", type=["xlsx"], key="bulk_upload")
        if up is not None:
            preview = call("POST", f"/exams/{exam['id']}/gradebook/import",
                           files={"file": (up.name, up.getvalue(), up.type)})
            if preview is not None:
                pv = preview.json()
                for w in pv["warnings"]:
                    st.warning(w)
                for u in pv["unmatched"]:
                    st.warning(f"Row {u['row']}: {u['university_id']} — {u['reason']}")
                if not pv["changes"]:
                    st.info("No mark changes found in this file.")
                else:
                    st.write(f"**{len(pv['changes'])} mark(s) will change:**")
                    diff_df = pd.DataFrame([
                        {"University ID": c["university_id"], "Student": c["student_name"], "Q": c["question_number"],
                         "Current": c["current_mark"], "New": c["new_mark"],
                         "Note": "⚠️ paper is approved — will be reopened" if c["currently_approved"] else ""}
                        for c in pv["changes"]
                    ])
                    st.dataframe(diff_df, hide_index=True, width="stretch")
                    if any(c["currently_approved"] for c in pv["changes"]):
                        st.caption("Approved papers with a changed mark will be reopened and need re-approval afterward.")
                    if st.button("✅ Apply these changes", type="primary"):
                        commit = call("POST", f"/exams/{exam['id']}/gradebook/import", params={"commit": True},
                                      files={"file": (up.name, up.getvalue(), up.type)})
                        if commit is not None:
                            cj = commit.json()
                            st.success(f"Applied {cj['applied']} change(s); {cj['reopened_submissions']} paper(s) reopened for re-approval.")
                            st.rerun()

        st.divider()
        p1, p2 = st.columns([3, 1])
        p1.markdown("**Student visibility** — " + ("🟢 published: students see their own approved marks." if exam["published"]
                    else "⚪ not published: students see nothing yet.")
                    + (f" Review requests open until {str(exam['appeals_open_until'])[:10]}." if exam["published"] and exam.get("appeals_open_until") else ""))
        if not exam["published"]:
            days = p1.number_input("Days students may request a review (0 = none)", 0, 60, 7)
            if p2.button("Publish to students", type="primary"):
                r = call("POST", f"/exams/{exam['id']}/publish", params={"appeal_days": int(days)})
                if r:
                    st.success(f"Visible to {r.json()['visible_to_students']} student(s); {r.json()['still_hidden']} still hidden (not approved).")
                    st.rerun()
        elif p2.button("Unpublish"):
            if call("POST", f"/exams/{exam['id']}/unpublish"):
                st.rerun()
        if st.button("Generate exports"):
            st.session_state["exports"] = {}
            for fmt in ("xlsx", "csv", "pdf"):
                r = call("GET", f"/exams/{exam['id']}/export/{fmt}", params={"include_unapproved": inc})
                if r is not None:
                    st.session_state["exports"][fmt] = r.content
        for fmt, data in st.session_state.get("exports", {}).items():
            st.download_button(f"⬇️ Download {fmt.upper()}", data, f"gradebook_exam{exam['id']}.{fmt}", key=f"d{fmt}")
    else:
        st.info("Nothing here yet. Approve papers in Review (or include provisional ones).")
