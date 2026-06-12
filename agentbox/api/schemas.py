"""API schemas.

Validation is where platform guardrails live: callers can ask for less
than the ceilings but never more. Limits are clamped server-side rather
than rejected where that's friendlier (timeouts), and rejected where
silent clamping would surprise (retries).
"""

from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from agentbox.config import get_settings


class CreateSessionRequest(BaseModel):
    image: str = Field(..., min_length=1, max_length=512, examples=["python:3.12-slim"])
    command: list[str] = Field(..., min_length=1, examples=[["python", "-c", "print('hi')"]])
    env: dict[str, str] = Field(default_factory=dict)
    cpu_limit: str | None = Field(default=None, examples=["500m"])
    memory_limit: str | None = Field(default=None, examples=["512Mi"])
    timeout_seconds: int | None = Field(default=None, ge=1)
    max_retries: int = Field(default=0, ge=0)

    @field_validator("timeout_seconds")
    @classmethod
    def clamp_timeout(cls, v: int | None) -> int | None:
        if v is None:
            return v
        return min(v, get_settings().max_timeout_seconds)

    @field_validator("max_retries")
    @classmethod
    def cap_retries(cls, v: int) -> int:
        ceiling = get_settings().max_retries_ceiling
        if v > ceiling:
            raise ValueError(f"max_retries cannot exceed {ceiling}")
        return v

    @field_validator("env")
    @classmethod
    def env_size(cls, v: dict[str, str]) -> dict[str, str]:
        if len(v) > 50:
            raise ValueError("too many environment variables (max 50)")
        return v


class SessionResponse(BaseModel):
    id: str
    state: str
    image: str
    command: list[str]
    attempt: int
    max_retries: int
    timeout_seconds: int
    exit_code: int | None
    failure_reason: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    model_config = {"from_attributes": True}


class LogsResponse(BaseModel):
    session_id: str
    logs: str
