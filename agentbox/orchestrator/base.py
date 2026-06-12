"""Executor abstraction.

The orchestration logic (API + reconciler) never talks to Kubernetes
directly — it talks to an Executor. That keeps the control plane testable,
makes laptop development possible without a cluster, and means a future
executor (Firecracker, ECS, a second cluster) is a new module rather than
a rewrite.
"""

import subprocess
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class WorkloadPhase(StrEnum):
    WAITING = "waiting"      # accepted by the executor, not running yet
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    GONE = "gone"            # executor has no record of it


@dataclass
class WorkloadStatus:
    phase: WorkloadPhase
    exit_code: int | None = None
    reason: str | None = None


@dataclass
class WorkloadSpec:
    session_id: str
    attempt: int
    image: str
    command: list[str]
    env: dict[str, str]
    cpu_limit: str
    memory_limit: str
    timeout_seconds: int


class Executor(Protocol):
    """The minimal contract a runtime backend must satisfy."""

    def submit(self, spec: WorkloadSpec) -> str:
        """Submit a workload. Returns an executor reference (e.g. Job name)."""
        ...

    def status(self, ref: str) -> WorkloadStatus:
        """Report current workload status."""
        ...

    def logs(self, ref: str, tail: int = 1000) -> str:
        """Fetch workload logs (best effort)."""
        ...

    def cancel(self, ref: str) -> None:
        """Stop a workload and release its resources. Idempotent."""
        ...

    def cleanup(self, ref: str) -> None:
        """Delete all resources for a finished workload. Idempotent."""
        ...


class LocalExecutor:
    """Runs workloads as local subprocesses. Development only.

    Ignores the image and resource limits — it exists so the API and
    reconciler can be exercised end-to-end on a laptop with nothing but
    Postgres running.
    """

    def __init__(self) -> None:
        self._procs: dict[str, subprocess.Popen] = {}

    def submit(self, spec: WorkloadSpec) -> str:
        ref = f"local-{spec.session_id}-{spec.attempt}"
        proc = subprocess.Popen(
            spec.command,
            env={**spec.env},
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self._procs[ref] = proc
        return ref

    def status(self, ref: str) -> WorkloadStatus:
        proc = self._procs.get(ref)
        if proc is None:
            return WorkloadStatus(phase=WorkloadPhase.GONE)
        code = proc.poll()
        if code is None:
            return WorkloadStatus(phase=WorkloadPhase.RUNNING)
        if code == 0:
            return WorkloadStatus(phase=WorkloadPhase.SUCCEEDED, exit_code=0)
        return WorkloadStatus(phase=WorkloadPhase.FAILED, exit_code=code, reason="non-zero exit")

    def logs(self, ref: str, tail: int = 1000) -> str:
        proc = self._procs.get(ref)
        if proc is None or proc.stdout is None:
            return ""
        return proc.stdout.read() or ""

    def cancel(self, ref: str) -> None:
        proc = self._procs.get(ref)
        if proc is not None and proc.poll() is None:
            proc.kill()

    def cleanup(self, ref: str) -> None:
        self._procs.pop(ref, None)
