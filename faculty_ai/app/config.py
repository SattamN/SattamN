"""Central configuration (environment variables / .env)."""
import os
import secrets
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _bool(value, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


class Settings:
    def __init__(self) -> None:
        self.anthropic_api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        self.claude_model = os.getenv("CLAUDE_MODEL", "claude-sonnet-5")
        self.feedback_language = os.getenv("FEEDBACK_LANGUAGE", "English")
        self.attach_exam_files = _bool(os.getenv("ATTACH_EXAM_FILES"))
        self.grading_max_tokens = int(os.getenv("GRADING_MAX_TOKENS", "16000"))
        self.ai_max_attempts = max(1, int(os.getenv("AI_MAX_ATTEMPTS", "2")))

        self.review_confidence_threshold = float(os.getenv("REVIEW_CONFIDENCE_THRESHOLD", "0.75"))
        self.grading_concurrency = max(1, int(os.getenv("GRADING_CONCURRENCY", "3")))
        self.max_upload_mb = int(os.getenv("MAX_UPLOAD_MB", "30"))

        self.data_dir = Path(os.getenv("DATA_DIR", str(BASE_DIR / "data"))).resolve()
        self.database_url = os.getenv("DATABASE_URL") or (
            f"sqlite:///{(self.data_dir / 'faculty_ai.db').as_posix()}"
        )
        self.pdf_font_path = os.getenv("PDF_FONT_PATH", "").strip()

        self.token_ttl_minutes = int(os.getenv("TOKEN_TTL_MINUTES", "480"))
        self.login_max_failures = int(os.getenv("LOGIN_MAX_FAILURES", "5"))
        self.login_lock_minutes = int(os.getenv("LOGIN_LOCK_MINUTES", "15"))
        self._secret_key = os.getenv("SECRET_KEY", "").strip()

        self.password_reset_ttl_minutes = int(os.getenv("PASSWORD_RESET_TTL_MINUTES", "30"))
        self.app_base_url = os.getenv("APP_BASE_URL", "http://localhost:8501").rstrip("/")
        self.smtp_host = os.getenv("SMTP_HOST", "").strip()
        self.smtp_port = int(os.getenv("SMTP_PORT", "587"))
        self.smtp_user = os.getenv("SMTP_USER", "").strip()
        self.smtp_password = os.getenv("SMTP_PASSWORD", "").strip()
        self.smtp_from = os.getenv("SMTP_FROM", "").strip() or self.smtp_user
        self.smtp_use_tls = _bool(os.getenv("SMTP_USE_TLS"), True)

    @property
    def secret_key(self) -> str:
        """JWT signing key. Set SECRET_KEY in production; in dev one is generated once and stored in data/.secret_key."""
        if self._secret_key:
            return self._secret_key
        f = self.data_dir / ".secret_key"
        if not f.exists():
            self.data_dir.mkdir(parents=True, exist_ok=True)
            f.write_text(secrets.token_urlsafe(48))
            try:
                f.chmod(0o600)
            except OSError:
                pass
        self._secret_key = f.read_text().strip()
        return self._secret_key

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def exports_dir(self) -> Path:
        return self.data_dir / "exports"

    def ensure_dirs(self) -> None:
        self.uploads_dir.mkdir(parents=True, exist_ok=True)
        self.exports_dir.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()
