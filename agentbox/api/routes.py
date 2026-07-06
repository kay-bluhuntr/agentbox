"""HTTP API.

Design intent: the API records intent and reads state; the reconciler does
the work. POST /v1/sessions only writes a PENDING row — submission to the
executor happens asynchronously, which keeps request latency flat and
means an executor outage degrades to a queue, not an error storm.
"""

import structlog
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session as DbSession

from agentbox.api.schemas import CreateSessionRequest, LogsResponse, SessionResponse
from agentbox.config import get_settings
from agentbox.models import Session, get_session_factory
from agentbox.observability import SESSIONS_CREATED
from agentbox.state import State, can_transition, is_terminal, transition

log = structlog.get_logger()
router = APIRouter(prefix="/v1")


def get_db():
    factory = get_session_factory()
    db = factory()
    try:
        yield db
    finally:
        db.close()


def get_executor():
    from agentbox.main import app_state

    return app_state["executor"]


@router.post("/sessions", response_model=SessionResponse, status_code=201)
def create_session(req: CreateSessionRequest, db: DbSession = Depends(get_db)):
    settings = get_settings()
    session = Session(
        image=req.image,
        command=req.command,
        env=req.env,
        cpu_limit=req.cpu_limit or settings.default_cpu_limit,
        memory_limit=req.memory_limit or settings.default_memory_limit,
        timeout_seconds=req.timeout_seconds or settings.default_timeout_seconds,
        max_retries=req.max_retries,
        gpu_count=req.gpu.count if req.gpu else 0,
        gpu_type=req.gpu.type if req.gpu and req.gpu.count > 0 else None,
    )
    db.add(session)
    db.commit()
    SESSIONS_CREATED.inc()
    log.info("session_created", session_id=session.id, image=session.image)
    return session


@router.get("/sessions/{session_id}", response_model=SessionResponse)
def get_session(session_id: str, db: DbSession = Depends(get_db)):
    session = db.get(Session, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    return session


@router.get("/sessions", response_model=list[SessionResponse])
def list_sessions(state: str | None = None, limit: int = 100, db: DbSession = Depends(get_db)):
    q = db.query(Session).order_by(Session.created_at.desc())
    if state:
        q = q.filter(Session.state == state)
    return q.limit(min(limit, 500)).all()


@router.get("/sessions/{session_id}/logs", response_model=LogsResponse)
def get_logs(session_id: str, tail: int = 1000, db: DbSession = Depends(get_db)):
    session = db.get(Session, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    if session.executor_ref is None:
        return LogsResponse(session_id=session_id, logs="")
    executor = get_executor()
    return LogsResponse(session_id=session_id, logs=executor.logs(session.executor_ref, tail))


@router.delete("/sessions/{session_id}", response_model=SessionResponse)
def cancel_session(session_id: str, db: DbSession = Depends(get_db)):
    session = db.get(Session, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")

    current = State(session.state)
    if is_terminal(current):
        return session  # idempotent: cancelling a finished session is a no-op
    if not can_transition(current, State.CANCELLED):
        raise HTTPException(status_code=409, detail=f"cannot cancel from state {current}")

    if session.executor_ref:
        executor = get_executor()
        try:
            executor.cancel(session.executor_ref)
        except Exception:
            log.exception("cancel_failed", session_id=session_id)
            raise HTTPException(status_code=502, detail="executor cancel failed") from None

    session.state = transition(current, State.CANCELLED)
    db.commit()
    log.info("session_cancelled", session_id=session_id)
    return session
