"""SQLAlchemy engine / session setup."""
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import get_settings

settings = get_settings()
settings.ensure_dirs()


class Base(DeclarativeBase):
    pass


def configure_sqlite(engine: Engine) -> None:
    """Foreign keys + WAL so background grading threads and the API can coexist."""

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        try:
            cur.execute("PRAGMA journal_mode=WAL")
        except Exception:
            pass
        cur.close()


def make_engine(url: str) -> Engine:
    if url.startswith("sqlite"):
        eng = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})
        configure_sqlite(eng)
        return eng
    return create_engine(url, pool_pre_ping=True)


engine = make_engine(settings.database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    from . import models  # noqa: F401  (register tables)

    Base.metadata.create_all(bind=engine)
