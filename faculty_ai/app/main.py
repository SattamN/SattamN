from contextlib import asynccontextmanager

from fastapi import FastAPI

from .config import get_settings
from .database import init_db
from .routers import admin, appeals, assignments, auth, courses, exams, export, gradebook, grading, notifications, student


@asynccontextmanager
async def lifespan(_: FastAPI):
    get_settings().ensure_dirs()
    init_db()
    yield


app = FastAPI(title="NBU AI", version="0.5.0", lifespan=lifespan,
              description="AI-assisted exam and assignment grading with mandatory instructor review, role-based access, review requests and a bulk Excel grade-edit workflow, all audited.")

for r in (auth.router, admin.router, courses.router, exams.router, grading.router, gradebook.router,
          export.router, student.router, appeals.student_router, appeals.router, notifications.router,
          assignments.router, assignments.student_router):
    app.include_router(r)


@app.get("/health", tags=["meta"])
def health():
    s = get_settings()
    return {"status": "ok", "ai_configured": bool(s.anthropic_api_key), "model": s.claude_model,
            "review_confidence_threshold": s.review_confidence_threshold}
