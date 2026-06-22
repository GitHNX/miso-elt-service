"""
Database engine and session factories.

Two engines:
- `app_engine`      — write access, used by the ingestion worker
- `readonly_engine` — connects as the read-only Postgres role, used by the API

This guarantees the API can never mutate data even if application code tries.
"""
from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from src.core.config import get_settings
from src.core.logging import get_logger

logger = get_logger(__name__)


def _make_engine(url: str, pool_size: int = 5):
    engine = create_engine(
        url,
        pool_size=pool_size,
        max_overflow=2,
        pool_pre_ping=True,       # detect stale connections
        pool_recycle=1800,        # recycle every 30 min (RDS proxy friendly)
        connect_args={"connect_timeout": 10},
    )

    @event.listens_for(engine, "connect")
    def set_search_path(dbapi_conn, conn_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("SET search_path TO miso, public")
        cursor.close()

    return engine


def _build_engines():
    settings = get_settings()
    app = _make_engine(settings.app_db_url, pool_size=5)
    readonly = _make_engine(settings.readonly_db_url, pool_size=10)
    return app, readonly


_app_engine, _readonly_engine = _build_engines()

AppSession = sessionmaker(bind=_app_engine, autocommit=False, autoflush=False)
ReadonlySession = sessionmaker(bind=_readonly_engine, autocommit=False, autoflush=False)


@contextmanager
def get_app_session() -> Generator[Session, None, None]:
    session = AppSession()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def get_readonly_session() -> Generator[Session, None, None]:
    session = ReadonlySession()
    try:
        yield session
    finally:
        session.close()


def check_db_connectivity() -> bool:
    """Health-check helper used by the API /health endpoint."""
    try:
        with _readonly_engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:
        logger.error("db_connectivity_check_failed", error=str(exc))
        return False
