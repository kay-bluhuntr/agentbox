# AgentBox

A self-hosted execution service that runs arbitrary container workloads inside a Kubernetes cluster with hard isolation guardrails — built specifically for AI agent pipelines that need to execute untrusted or long-running code safely.

## The problem it solves

When an AI agent needs to run code — execute a script, process a file, call a CLI tool — the naive approach is to shell out directly on the host or in the agent's own container. That means:

- **No isolation.** A prompt-injected command can read secrets, exhaust memory, or pivot to other services.
- **No retry logic.** If the execution fails mid-run due to a flaky node or OOM, the agent has to handle it itself.
- **No observability.** You can't see what ran, when, or why it failed without instrumenting every callsite.
- **No backpressure.** A burst of agent tasks can overwhelm the host with no way to throttle.

AgentBox solves all of this with a single async API: the agent submits a workload, AgentBox enforces the guardrails and manages execution on Kubernetes, and the agent polls for the result. The agent never touches the cluster directly.

## How it works

```
Agent  ──POST /v1/sessions──►  AgentBox API  ──(async)──►  Kubernetes Job
                                     │                           │
                               Postgres (state)           isolated pod
                                     │                           │
                               Reconciler  ◄──── polls status ───┘
                                     │
                               retries / timeouts / cleanup
```

1. **API records intent.** `POST /v1/sessions` writes a `PENDING` row and returns immediately. The caller gets a session ID.
2. **Reconciler drives execution.** A background loop submits pending sessions to Kubernetes, watches running ones, applies timeouts, retries failures with exponential backoff, and cleans up finished resources.
3. **Kubernetes enforces isolation.** Every workload runs as a hardened Job: non-root, no privilege escalation, all capabilities dropped, read-only root filesystem, no cluster API credentials, and hard CPU/memory limits. A default-deny `NetworkPolicy` in the execution namespace blocks outbound traffic by default.
4. **Postgres is the source of truth.** If a Job disappears (node loss, cluster restart), the reconciler notices and retries — the session state is never lost.

## Key guarantees

| Concern | How AgentBox handles it |
|---|---|
| Runaway processes | `activeDeadlineSeconds` on the Job + reconciler soft timeout |
| Memory/CPU abuse | Hard resource limits on every container, configurable per-request |
| Network exfiltration | Default-deny `NetworkPolicy` in the execution namespace |
| Cluster API access | `automountServiceAccountToken: false` on all workload pods |
| Cascading failures | Fast API returns 201 immediately; executor outage degrades to a queue, not errors |
| Duplicate driving | Postgres advisory lock ensures exactly one reconciler runs across replicas |
| Flaky infra | Per-session retry budget with jitter backoff, tracked in Postgres |

## Quickstart

**Local dev (no cluster needed):**

```bash
make dev          # starts the API + Postgres via docker-compose
make smoke        # submits a test session and polls for success
```

**Full Kubernetes setup:**

```bash
make kind-up      # creates a local kind cluster and deploys everything

# The API Service is ClusterIP, so forward a local port before smoke-testing:
kubectl -n agentbox port-forward svc/agentbox 8080:80 &
make smoke        # runs the smoke test against the forwarded port
```

`make smoke` targets `http://localhost:8080` (override with `AGENTBOX_HOST`). In
the docker-compose path that port is published directly; on kind it's the
port-forward above.

## API

### Submit a workload

```bash
curl -X POST http://localhost:8080/v1/sessions \
  -H 'Content-Type: application/json' \
  -d '{
    "image": "python:3.12-slim",
    "command": ["python", "-c", "print(\"hello from AgentBox\")"],
    "timeout_seconds": 60,
    "max_retries": 2
  }'
```

Response:
```json
{
  "id": "a3f1c2d4-...",
  "state": "pending",
  "image": "python:3.12-slim",
  "command": ["python", "-c", "print(\"hello from AgentBox\")"],
  "attempt": 0,
  "created_at": "2026-06-12T10:00:00Z",
  ...
}
```

### Poll for result

```bash
curl http://localhost:8080/v1/sessions/a3f1c2d4-...
```

### Get logs

```bash
curl http://localhost:8080/v1/sessions/a3f1c2d4-.../logs
```

### Cancel

```bash
curl -X DELETE http://localhost:8080/v1/sessions/a3f1c2d4-...
```

## Session lifecycle

```
PENDING → SCHEDULED → RUNNING → SUCCEEDED
                ↓           ↓
             FAILED      FAILED → RETRYING → SCHEDULED (retry loop)
                ↓           ↓
           CANCELLED    TIMED_OUT
```

## Configuration

All settings are environment variables prefixed `AGENTBOX_`:

| Variable | Default | Description |
|---|---|---|
| `AGENTBOX_DATABASE_URL` | `postgresql+psycopg://...` | Postgres connection string |
| `AGENTBOX_EXECUTOR` | `kubernetes` | `kubernetes` or `local` (dev mode) |
| `AGENTBOX_EXEC_NAMESPACE` | `agentbox-exec` | Kubernetes namespace for workload Jobs |
| `AGENTBOX_DEFAULT_CPU_LIMIT` | `500m` | CPU cap per workload |
| `AGENTBOX_DEFAULT_MEMORY_LIMIT` | `512Mi` | Memory cap per workload |
| `AGENTBOX_MAX_TIMEOUT_SECONDS` | `3600` | Hard ceiling on session timeout |
| `AGENTBOX_DEFAULT_TIMEOUT_SECONDS` | `300` | Default if not specified per-request |
| `AGENTBOX_MAX_RETRIES_CEILING` | `5` | Max retries a caller can request |
| `AGENTBOX_RECONCILE_INTERVAL_SECONDS` | `2.0` | How often the reconciler polls |

## Deployment

ArgoCD manifests and Helm charts are in [`deploy/`](deploy/). Separate value files exist for dev and prod:

```
deploy/
  helm/agentbox/        # Helm chart (Deployment/Rollout, RBAC, NetworkPolicy,
                        #   ResourceQuota, ServiceMonitor, PrometheusRule)
  envs/dev/values.yaml  # fast iteration: plain Deployment, no canary
  envs/prod/values.yaml # full pattern: ServiceMonitor + alerts + canary rollout
  argocd/               # App-of-apps root, AppProject, dev/prod Applications
  terraform/            # cluster-level infra
```

### Platform capabilities

The chart is feature-flagged so the same templates serve a bare kind cluster and a
fully-instrumented production cluster:

- **Progressive delivery** (`rollout.enabled`): ships the control plane as an
  [Argo Rollouts](https://argoproj.github.io/rollouts/) `Rollout` with a canary
  strategy instead of a `Deployment`. The `AnalysisTemplate` gates promotion on
  the control plane's *own* health signals — reconcile error ratio and API p99
  latency — so a regression auto-rolls-back before reaching every replica. Enabled
  in prod, off in dev (speed over safety there). Stable/canary share one pod spec
  via [`_helpers.tpl`](deploy/helm/agentbox/templates/_helpers.tpl).
- **Observability bootstrap** (`metrics.serviceMonitor.enabled`,
  `monitoring.prometheusRule.enabled`): a `ServiceMonitor` so Prometheus scrapes
  `/metrics`, plus alerts (reconciler stalled, high API latency, session failure
  rate) and a Google-SRE multi-window **burn-rate SLO** on reconcile success.
- **GitOps guardrails**: an ArgoCD `AppProject` fences every Application to this
  repo and the AgentBox namespaces; `sync-wave` annotations apply the namespace,
  NetworkPolicy, quota, and RBAC (wave -1) before the workload (wave 0).

These need the Prometheus Operator and Argo Rollouts CRDs in-cluster, so they
default off in the base chart (the kind dev path stays dependency-free) and are
switched on in [`envs/prod/values.yaml`](deploy/envs/prod/values.yaml).

## Development

The project requires Python 3.11+. If your system default is older, create a venv explicitly:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Then:

```bash
make test       # run the test suite
make lint       # ruff check
make typecheck  # mypy
```

Tests use SQLite + a stub executor so no cluster is required.
