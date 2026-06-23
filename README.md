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

Deployment state is split across two repos:

- **This repo** holds the application *and* its Helm chart + ArgoCD manifests — the
  *what*.
- **[`agentbox-gitops`](https://github.com/kay-bluhuntr/agentbox-gitops)** holds the
  per-env values (`envs/dev`, `envs/prod`) — *which version runs where*. CI writes
  image tags there, so **this repo's `main` only ever changes by human PR** (no bot
  commits, so your `git push` is never rejected by a CI commit).

```
deploy/
  helm/agentbox/        # Helm chart (Deployment/Rollout, RBAC, NetworkPolicy,
                        #   ResourceQuota, ServiceMonitor, PrometheusRule)
  argocd/               # App-of-apps root, AppProject, dev/prod Applications
  terraform/            # cluster-level infra

agentbox-gitops/        # SEPARATE repo — deployment state
  envs/dev/values.yaml  # fast iteration: plain Deployment, no canary
  envs/prod/values.yaml # full pattern: ServiceMonitor + alerts + canary rollout
```

The chart is feature-flagged so the same templates serve a bare kind cluster and a
fully-instrumented production cluster. The CRD-dependent features default **off** in
the base chart (so the kind dev path stays dependency-free) and are switched **on**
in `agentbox-gitops`'s `envs/prod/values.yaml`.

### GitOps with ArgoCD

Deployment is pull-based: nobody runs `helm` against a real cluster by hand. Git is
the only deployment interface, and ArgoCD reconciles the cluster to match.

```
git push (app repo) ─► CI builds + scans image ─► CI writes image tag to
                                                  agentbox-gitops envs/dev/values.yaml
                                                          │
                                              ArgoCD detects the change
                                                          │
                                       ArgoCD syncs the cluster to Git
```

- **App-of-apps**: one root Application ([`argocd/root.yaml`](deploy/argocd/root.yaml))
  points at [`argocd/apps/`](deploy/argocd/apps); adding or changing an environment is
  a pull request against that directory. Bootstrap a cluster once with:
  ```bash
  kubectl apply -f deploy/argocd/root.yaml
  ```
- **Environments**: [`apps/dev.yaml`](deploy/argocd/apps/dev.yaml) and
  [`apps/prod.yaml`](deploy/argocd/apps/prod.yaml) are multi-source Applications — the
  chart comes from this repo, the per-env `values.yaml` from the `agentbox-gitops`
  repo (the `$values` ref). Both auto-sync; the gate for prod is the **pull request**
  against `agentbox-gitops` (run [`scripts/promote.sh`](scripts/promote.sh)), not a
  manual sync button.
- **AppProject guardrail** ([`apps/project.yaml`](deploy/argocd/apps/project.yaml)):
  an RBAC boundary that fences every Application to this repo and the AgentBox
  namespaces, and whitelists `Namespace` as the only cluster-scoped resource it may
  create.
- **Sync waves**: `sync-wave: -1` annotations apply the execution namespace,
  default-deny `NetworkPolicy`, `ResourceQuota`, and RBAC *before* the control plane
  (wave 0), so the guardrails always exist before any workload can.

> **Credentials this split needs:**
> - **ArgoCD** must be able to read both private repos (chart + `agentbox-gitops`) —
>   register read-only repo creds (`argocd-repo-creds` Secret), or Applications fail
>   to sync with `authentication required`.
> - **CI** needs a `GITOPS_TOKEN` repo secret with write access to `agentbox-gitops`
>   so the `deploy-dev` job can push the image-tag bump there (a fine-grained PAT or
>   a deploy key).

### Observability

The control plane exposes Prometheus metrics at `/metrics`
([`agentbox/observability.py`](agentbox/observability.py)):

| Metric | Meaning |
|---|---|
| `agentbox_sessions_created_total` | sessions accepted by the API |
| `agentbox_sessions_finished_total{state}` | sessions reaching a terminal state |
| `agentbox_session_retries_total` | retries scheduled by the reconciler |
| `agentbox_reconcile_loops_total` / `_errors_total` | control-loop liveness and failures |
| `agentbox_api_request_seconds` | API latency histogram (by method, route) |

Enabling `metrics.serviceMonitor.enabled` and `monitoring.prometheusRule.enabled`
ships:

- a **`ServiceMonitor`** so the Prometheus Operator scrapes `/metrics` (label it to
  match your operator's selector, e.g. `release: kube-prometheus-stack`);
- a **`PrometheusRule`** with operational alerts (`AgentboxReconcilerStalled`,
  `AgentboxHighApiLatency`, `AgentboxSessionFailureRateHigh`) plus recording rules
  for the reconcile-success SLI and a Google-SRE **multi-window burn-rate SLO**
  (fast-burn pages, slow-burn tickets) against `monitoring.slo.reconcileSuccessTarget`.

### Progressive delivery (canary)

With `rollout.enabled`, the control plane ships as an
[Argo Rollouts](https://argoproj.github.io/rollouts/) `Rollout` instead of a
`Deployment`. A new version takes `rollout.canaryWeight`% of traffic, then an
`AnalysisTemplate` queries Prometheus for the control plane's *own* health — reconcile
error ratio and API p99 latency — and **auto-rolls-back** if either regresses before
the change reaches every replica. Stable and canary share one pod spec via
[`_helpers.tpl`](deploy/helm/agentbox/templates/_helpers.tpl). Enabled in prod, off in
dev (speed over safety there).

### Running the full platform stack on kind

`make kind-up` deploys the dependency-free base chart. To exercise GitOps,
observability, and canary locally, install the controllers and deploy with the flags
on:

```bash
# 1. Controllers (CRDs the features depend on)
kubectl create namespace argocd && \
  kubectl apply -n argocd --server-side -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml
kubectl create namespace argo-rollouts && \
  kubectl apply -n argo-rollouts -f https://github.com/argoproj/argo-rollouts/releases/latest/download/install.yaml
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts && helm repo update
helm install kube-prometheus-stack prometheus-community/kube-prometheus-stack \
  -n monitoring --create-namespace --set alertmanager.enabled=false

# 2. Deploy with the platform flags on (local image, Prometheus-Operator selector label)
helm upgrade --install agentbox deploy/helm/agentbox -n agentbox \
  --set image.repository=agentbox --set image.tag=dev --set image.pullPolicy=IfNotPresent \
  --set rollout.enabled=true \
  --set metrics.serviceMonitor.enabled=true \
  --set metrics.serviceMonitor.labels.release=kube-prometheus-stack \
  --set monitoring.prometheusRule.enabled=true \
  --set monitoring.prometheusRule.labels.release=kube-prometheus-stack
```

Then explore:

```bash
kubectl -n agentbox get rollout,servicemonitor,prometheusrule,analysistemplate
kubectl -n monitoring port-forward svc/kube-prometheus-stack-prometheus 9090:9090  # targets, rules, graph
kubectl -n monitoring port-forward svc/kube-prometheus-stack-grafana 3000:80       # admin / prom-operator
```

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
