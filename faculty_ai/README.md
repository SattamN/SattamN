# NBU AI — Grading MVP + self-service accounts, student portal, review requests, bulk Excel edit, assignments (v0.5)

AI-assisted exam grading for university instructors. **The AI proposes, the instructor decides**:

```
Upload exam + model answer → AI extracts questions & suggests rubrics → instructor approves rubrics
→ upload student papers (PDF / photos) → AI grades each paper into strict JSON
→ rule engine flags what needs a human → instructor reviews → approves → Gradebook → Excel / CSV / PDF
```

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                   # then put your ANTHROPIC_API_KEY (and SECRET_KEY) in it
python -m app.cli create-user --id 1001 --name "Dr. Ahmed" --role faculty   # first account (also: --role admin)

uvicorn app.main:app --reload                          # API  → http://127.0.0.1:8000/docs
streamlit run ui/app.py                                # UI   → http://localhost:8501
python -m pytest -q                                    # tests (no network / API key needed)
```

> **Upgrading from an earlier version?** There are no migrations yet: delete `data/faculty_ai.db` (dev data only) before starting.

## Accounts, roles and privacy

| | Faculty | Student | Admin |
|---|---|---|---|
| Own courses, exams, rubrics, grading, gradebook | ✅ | ❌ | ✅ all |
| Another instructor's data | ❌ (404) | ❌ | ✅ |
| Import roster / reset student passwords | ✅ own courses | ❌ | ✅ |
| Own approved & **published** final marks | – | ✅ only their own | – |
| Users, audit log | ❌ | ❌ | ✅ |

- **Login** with university ID + password; JWT session (`TOKEN_TTL_MINUTES`, default 8 h). Passwords use scrypt. 5 failed attempts lock an ID for 15 minutes. Changing/resetting a password invalidates old sessions.
- **Roster import** (`.xlsx`/`.csv`, English or Arabic headers: University ID / الرقم الجامعي, Name / الاسم, Email, Section / الشعبة) creates the student accounts, enrolls them in the course, and returns one-time **temporary passwords** (shown once; students must change them at first sign-in).
- **Students never see AI marks.** The pipeline is `AI mark → instructor review → approved final mark → Publish → student`. A student sees only: their own, **approved**, **published** final marks. No AI reasons, confidence, notes or other students' data. *Unpublish* hides it again.
- **Audit log** (`audit_logs`, append-only, admin-only via `GET /admin/audit`): logins/failures, course/roster changes, every mark change (AI mark, before, after, note), approvals, reopenings, publishing, exports, password resets. Passwords are never logged.
- Set `SECRET_KEY` in production (otherwise a key is generated once in `data/.secret_key`). Run behind HTTPS.

## Self-service accounts (faculty) & password reset

No admin action is required to add an instructor: anyone can create their own faculty account from
the login screen ("Create a faculty account" tab), choosing their own university/staff ID, name,
email and password. A self-registered account can only ever see courses it creates itself — the
same ownership rule (Section 6) that governs every faculty account, admin-created or not.

```
POST /auth/register          -> create account, logged in immediately
POST /auth/forgot-password    -> emails a one-time reset link (if the account has an email on file)
POST /auth/reset-password     -> consume the link's token, set a new password, logged in immediately
```

- **No email server required to develop/test locally.** If `SMTP_HOST` is unset (the default), reset
  links are printed to the server console/log instead of emailed — the flow still works end-to-end.
  Set `SMTP_HOST`/`SMTP_USER`/`SMTP_PASSWORD` in `.env` to send real emails (Gmail with an App
  Password works out of the box).
- The reset link points at `APP_BASE_URL` (default `http://localhost:8501`) with a `?reset_token=`
  query parameter; the Streamlit app detects it on load and shows a "set a new password" form.
- Tokens are single-use, expire after `PASSWORD_RESET_TTL_MINUTES` (default 30), and only their
  sha256 hash is stored — the raw token exists only in the email.
- `/auth/forgot-password` always returns the same generic message whether or not the account exists,
  to prevent discovering valid university IDs by probing the endpoint.
- Admin-driven account creation (`/admin/users`, `python -m app.cli create-user`) still exists and
  is still how student and admin accounts get created — this addition is for faculty self-signup
  specifically.

## Assignments

A lighter-weight sibling of exams for ongoing coursework — same AI-proposes/instructor-approves
discipline, reusing the grading pipeline (`ai_grader.grade_assignment_submission` adapts an
`Assignment` into the same shape `grade_paper` already speaks, so the review-flag rule engine,
prompt-injection defenses and audit pattern are identical to exam grading, not reimplemented).

```
Faculty: create assignment (+ optional due date / allow_late) -> approve rubric
      -> Student: upload answer (PDF/photos) -> AI grades -> Faculty reviews/approves -> Release
Student: sees the assignment, submission status, and the mark only once approved AND released
```

- **Due dates**: submitting after `due_at` is rejected unless `allow_late` is set, in which case the
  submission is accepted and flagged `late: true`.
- **One submission per student** while it is still `uploaded`; re-uploading overwrites it. Once
  grading has started, the student must ask the instructor to reopen it (mirrors exam submissions).
- **Release** is the assignment's own publish step (`results_released_at`), separate from exam
  publishing — an approved mark is invisible to the student until the instructor releases it.
- Endpoints: `/courses/{id}/assignments`, `/assignments/{id}` (+ `/rubric`, `/rubric/generate`,
  `/grade`, `/grading-status`, `/approve-unflagged`, `/release`, `/unrelease`),
  `/assignment-submissions/{id}` (+ `/grade`, `/review`, `/accept-ai`, `/regrade`, `/approve`,
  `/reopen`, `/file/{index}`), student side under `/me/assignments/{id}/submit` and
  `/me/courses/{id}/assignments`.
- **Not included in this pass** (deliberately, to keep scope tight): review-requests/appeals for
  assignments, and the explained-feedback layer (Section 3.5) — neither exists for exams yet either.

## Bulk-editing marks via Excel

```
GET  /exams/{id}/gradebook/editable-xlsx        -> download (sheet-protected: only mark cells are unlocked)
POST /exams/{id}/gradebook/import                -> preview (default): returns the diff, writes nothing
POST /exams/{id}/gradebook/import?commit=true     -> apply exactly that diff
```

- Rows are matched by **University ID**, not row position — reordering or filtering rows in Excel is safe.
- Every change goes through the same rules as a manual edit: value must be within `[0, max_mark]`, and a
  question that hasn't been graded yet is skipped with a warning rather than silently created.
- Editing a mark on an **approved** paper reopens that submission first (audited as `submission_reopened`),
  exactly like using "Reopen" in the Review screen — it is not a bypass, just a different entry point to the
  same action. The paper needs re-approval afterward.
- Every applied mark change is audited as a normal `grade_set` entry (`before`/`after`, `source:
  "bulk_excel_import"`), indistinguishable in the audit log from any other instructor edit except for that tag.
- The preview step is not optional in the UI: nothing is written until the instructor reviews the diff table
  and clicks "Apply these changes".

## Review requests (appeals)

```
Publish (choose how many days students may ask for a review)
 → student selects questions + explains why (one request per exam, 10–2000 chars)
 → instructor is notified → [optional] AI second opinion → instructor decides per question
   (keep / change mark) + a mandatory message to the student, and chooses whether the student may view their paper
 → student is notified; their marks update; paper viewing unlocks only if granted
```

- **The AI second opinion is independent and advisory.** It re-grades only the requested questions from scratch against the approved rubric; it is *not told the original mark*, treats the student's message as untrusted (a pointer to where to look, never a reason to add marks), and never changes anything. The instructor sees original vs. AI mark, the difference, confidence, rubric breakdown and the same review flags as normal grading.
- **Only the instructor changes marks.** Every change is stored on the grade (`final_mark`, `reviewer_note: "Review request #n"`) and in the audit log with before/after and the AI's opinion. The gradebook and the student's view follow the new mark immediately.
- **What students see:** their request, the decision per question (new mark, your note), your message, and — only if you grant it — their own paper and final marks. Never AI reasoning, confidence or other students' data.
- Rules enforced by the API: published exam + approved paper + open window; one request per paper; decisions are all-or-nothing and can't be repeated; unknown questions/invalid marks are rejected; other instructors can't see the request.
- **Notifications** (bell in the sidebar): instructor on new requests; student on decisions and paper access.

## Workflow in the UI

0. **Sign in.** Students land on *My courses* and see only released marks.
1. **Course & Exam** – create a course and an exam (upload the exam paper and, optionally, the model answer).
1b. **Students** – import the class roster; download the temporary passwords; reset a password if needed.
2. **Questions & Rubrics** – click *Extract questions* (AI reads the files) or add questions manually. Edit each rubric; **grading is blocked until every rubric is approved** and criteria sum exactly to the question's mark.
3. **Submissions & Grading** – upload one student (several photos allowed) or bulk-upload files named `<university_id>.pdf`. *Grade all* runs in the background (parallel, `GRADING_CONCURRENCY`).
4. **Review** – side by side: paper | AI transcription, reasoning, rubric breakdown, flags. *Accept AI mark*, edit, or re-grade one question. Approve the paper (flagged questions must be reviewed first) or *Approve all papers with no flags*.
5. **Gradebook & Export** – approved marks only (optional provisional view), stats, **bulk-edit marks via a re-uploadable Excel file** (see below), **Publish to students** (with the review-request window), `.xlsx` (live SUM formulas), `.csv` (Excel-safe UTF-8), `.pdf` (Arabic-safe).

## AI output contract (`ai_grader.py`)

The model is forced (tool use) to return structured JSON, validated with Pydantic. Per question:

```json
{
  "question_number": 1,
  "student_answer_transcription": "…",
  "answer_status": "answered | partial | blank | unclear",
  "mark": 7.5, "max_mark": 10,
  "reason": "The student used the correct method but made an arithmetic error in the final step.",
  "confidence": 0.91, "needs_review": false,
  "rubric_breakdown": [
    {"criterion": "Correct method", "mark": 3, "max_mark": 3},
    {"criterion": "Calculations",   "mark": 2.5, "max_mark": 4},
    {"criterion": "Final answer",   "mark": 2, "max_mark": 3}
  ]
}
```

Invalid output is retried once with the validation error; every call (success or failure) is stored in `ai_call_logs`.

## When is a question flagged for review?

`confidence < REVIEW_CONFIDENCE_THRESHOLD` is **one signal only**. Any of these sets `needs_review` (codes stored in `grades.review_flags`):

| Flag | Meaning |
|---|---|
| `low_confidence` | confidence below threshold |
| `model_flagged` | the model itself asked for review |
| `answer_unclear` / `blank_but_marked` | illegible/unlocatable answer; marks given to a blank answer |
| `mark_out_of_range` / `max_mark_mismatch` | mark <0 or >max; model's max ≠ question max |
| `breakdown_sum_mismatch` | breakdown marks don't add up to the total |
| `rubric_criteria_mismatch` | breakdown missing, or names/maxima differ from the approved rubric |
| `empty_reason` | no justification |
| `missing_from_ai_response` / `duplicate_in_ai_response` | question absent/duplicated in the output |

## Audit trail

- `grades.ai_*` (mark, reason, confidence, breakdown, `ai_raw_response`, `ai_model`) are never overwritten by the instructor; `final_mark`, `decision` (`pending | accepted_ai | edited | auto_approved`) and `reviewer_note` are separate.
- Regrading resets the decision; reviewed work is protected unless `force=true`.
- All core tables have `created_at` / `updated_at`.
- Student papers are treated as **untrusted data** in the prompt (text such as "give me full marks" is ignored and flagged).

## Structure

```
app/  main.py · config.py · database.py · models.py · schemas.py
      security.py · cli.py
      routers/  auth · admin · courses(+roster) · exams · grading · gradebook(+publish) · export · student · appeals · notifications
      services/ ai_grader · rubric · exporter · storage · roster · audit · notifications
      prompts/  grading · rubric_gen
ui/app.py        Streamlit prototype          tests/test_grading.py
data/            uploads/ exports/ faculty_ai.db .secret_key (git-ignored)
```

## Notes / limits

- Anthropic API limits: images ≤ 5 MB each; PDFs ≤ 32 MB / 100 pages (`MAX_UPLOAD_MB` caps PDFs at 30 MB).
- Questions that depend on figures: set `ATTACH_EXAM_FILES=true` (exam + model answer sent with each call, prompt-cached).
- PDF export needs a TTF font with Arabic glyphs; DejaVu/Arial/Tahoma are auto-detected, or set `PDF_FONT_PATH`.
- SQLite by default; set `DATABASE_URL` for PostgreSQL later. Tables are created automatically (no migrations yet).
- Student papers are sent to the Anthropic API for grading: check your university's data-protection policy (e.g. PDPL) and get approval before real use; consider asking students not to write their names on answer pages.
- Auth is single-process/in-memory for login throttling; for multi-server deployments move it to a shared store.
- Before trusting it on a whole class, grade 3–5 real papers and compare with your own marks.

## Roadmap (foundation is in place)

1. **Explained feedback for students** – instructor-approved "what was right / what went wrong / how to improve" per question (the paper viewing gate already exists).
2. **Analytics + CLO/PLO** – `grades.ai_rubric_breakdown` and `questions.clo` already store what is needed.
3. **Learning profile & practice** – only from instructor-approved data; measure improvement with a real re-assessment, not with the AI's own estimate.
4. Assignments, teaching tools (slides / question generator), course knowledge base.
