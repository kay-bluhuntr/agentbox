"""Observability: Prometheus metrics and structured JSON logging.

Metric design follows the rule that you should be able to answer the three
on-call questions from /metrics alone: is work flowing (sessions created /
finished), is it healthy (failure and retry rates), and is the control
loop alive (reconcile loops / errors)?
"""

import logging

import structlog
from prometheus_client import Counter, Histogram

SESSIONS_CREATED = Counter(
    "agentbox_sessions_created_total", "Sessions accepted by the API"
)
SESSIONS_FINISHED = Counter(
    "agentbox_sessions_finished_total", "Sessions reaching a terminal state", ["state"]
)
SESSION_RETRIES = Counter(
    "agentbox_session_retries_total", "Retry attempts scheduled by the reconciler"
)
RECONCILE_LOOPS = Counter(
    "agentbox_reconcile_loops_total", "Completed reconciler ticks"
)
RECONCILE_ERRORS = Counter(
    "agentbox_reconcile_errors_total", "Reconciler ticks that raised"
)
API_LATENCY = Histogram(
    "agentbox_api_request_seconds", "API request latency", ["method", "route"]
)


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(level=level, format="%(message)s")
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
    )
