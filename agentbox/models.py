"""SQLAlchemy models and engine setup.

Postgres is the single stateful dependency. Design notes:

- Session state lives here, not in Kubernetes. K8s Jobs are treated as
  disposable workers; if the cluster loses a Job we still know what the
  session was doing and can retry or surface a clean failure.
- The reconciler uses a Postgres advisory lock for leader election, so the
  control plane can run multiple replicas without double-driving sessions.
  I chose this over Redis to keep the moving parts down — one database is
  easier to operate, back up, and reason about than two.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, Integer, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from agentbox.config import get_settings
from agentbox.state import State


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Session(Base):
    """One execution request and its full lifecycle."""

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    state: Mapped[str] = mapped_column(String(20), default=State.PENDING, index=True)

    # Workload spec
    image: Mapped[str] = mapped_column(String(512))
    command: Mapped[list] = mapped_column(JSON)
    env: Mapped[dict] = mapped_column(JSON, default=dict)
    cpu_limit: Mapped[str] = mapped_column(String(20))
    memory_limit: Mapped[str] = mapped_column(String(20))
    timeout_seconds: Mapped[int] = mapped_column(Integer)
    max_retries: Mapped[int] = mapped_column(Integer, default=0)

    # Execution tracking
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    executor_ref: Mapped[str | None] = mapped_column(String(253), nullable=True)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cleaned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def deadline_exceeded(self, now: datetime) -> bool:
        if self.started_at is None:
            return False
        started = self.started_at
        if started.tzinfo is None:
            started = started.replace(tzinfo=UTC)
        return (now - started).total_seconds() > self.timeout_seconds


_engine = None
_session_factory = None


def get_engine():
    global _engine
    if _engine is None:
        _engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    return _engine


def get_session_factory() -> sessionmaker:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(bind=get_engine(), expire_on_commit=False)
    return _session_factory


def init_db() -> None:
    Base.metadata.create_all(get_engine())
