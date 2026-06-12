"""Application entrypoint.

Wires together the API, the executor, the reconciler thread, metrics, and
health probes. Run with: uvicorn agentbox.main:app
"""

import threading
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from agentbox.api.routes import router
from agentbox.config import get_settings
from agentbox.models import init_db
from agentbox.observability import API_LATENCY, configure_logging
from agentbox.orchestrator import build_executor
from agentbox.reconciler import Reconciler

app_state: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)
    init_db()

    executor = build_executor()
    app_state["executor"] = executor

    reconciler = Reconciler(executor)
    app_state["reconciler"] = reconciler
    thread = threading.Thread(target=reconciler.run_forever, daemon=True, name="reconciler")
    thread.start()

    yield

    reconciler.stop()


app = FastAPI(
    title="AgentBox",
    description="Sandboxed execution service for AI agent workloads on Kubernetes",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(router)


@app.get("/metrics", include_in_schema=False)
def metrics() -> Response:
    # A plain route, not a sub-app mount: mounts answer /metrics with a 307
    # to /metrics/, which default Prometheus scrape configs won't follow.
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.middleware("http")
async def record_latency(request: Request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    route = request.scope.get("route")
    API_LATENCY.labels(
        method=request.method, route=route.path if route else "unknown"
    ).observe(time.perf_counter() - start)
    return response


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"status": "ok"}


@app.get("/readyz", include_in_schema=False)
def readyz():
    # Ready when the DB is reachable; a failed query raises and returns 500.
    from sqlalchemy import text

    from agentbox.models import get_engine

    with get_engine().connect() as conn:
        conn.execute(text("SELECT 1"))
    return {"status": "ready"}
