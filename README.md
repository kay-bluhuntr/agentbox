# AgentBox

A self-hosted execution service that runs arbitrary container workloads inside a Kubernetes cluster with hard isolation guardrails — built specifically for AI agent pipelines that need to execute untrusted or long-running code safely.

## Architecture at a glance

AgentBox ships as **two repositories**. This repo (`agentbox`) holds the application and *how* to build and deploy it; [`agentbox-gitops`](https://github.com/kay-bluhuntr/agentbox-gitops) holds the per-environment state ArgoCD reconciles — *which version runs where*. CI builds and pushes the image and bumps the **dev** tag in the GitOps repo; promotion to **prod** is a reviewed PR there. Nothing ever touches a cluster by hand.

```mermaid
flowchart TB
    dev(["Developer"])

    subgraph APP["📦 agentbox · application repo — the what"]
        direction TB
        code["App code + Dockerfile"]
        chart["Helm chart<br/>deploy/helm"]
        argo["ArgoCD manifests<br/>deploy/argocd"]
        subgraph CI["GitHub Actions CI"]
            direction LR
            t["test<br/>lint · pytest"] --> s["security<br/>Checkov"] --> b["build<br/>multi-arch · Trivy<br/>tag = short SHA"] --> dd["deploy-dev"]
        end
    end

    img[("🐳 GHCR image<br/>:short-sha + :latest")]

    subgraph GITOPS["📂 agentbox-gitops · deployment state — which version, where"]
        direction TB
        edev["envs/dev/values.yaml<br/>tag bumped by CI"]
        eprod["envs/prod/values.yaml<br/>tag bumped by promote PR"]
    end

    subgraph K8S["☸️ Kubernetes cluster — ArgoCD-managed"]
        direction TB
        acd{{"ArgoCD<br/>app-of-apps"}}
        ddep["agentbox-dev<br/>Deployment · plain"]
        prol["agentbox · prod<br/>Rollout · canary"]
        exec["agentbox-exec*<br/>sandboxed Jobs<br/>default-deny NetworkPolicy"]
        prom["Prometheus + Grafana<br/>ServiceMonitor · alerts · burn-rate SLO"]
    end

    dev -->|git push| code
    code -->|triggers| t
    b -->|push image| img
    dd -->|writes image tag| edev
    dev -.->|scripts/promote.sh · PR| eprod
    argo -.->|kubectl apply root.yaml| acd

    acd -->|reads chart| chart
    acd -->|reads dev values| edev
    acd -->|reads prod values| eprod
    acd ==>|syncs| ddep
    acd ==>|syncs| prol
    img -.->|image pull| ddep
    img -.->|image pull| prol
    ddep -->|submits Jobs| exec
    prol -->|submits Jobs| exec
    prol <-.->|canary analysis| prom
    prom -.->|scrape metrics| ddep
    prom -.->|scrape metrics| prol

    style APP fill:#eaf2ff,stroke:#3b6fb0,color:#111
    style GITOPS fill:#eafbea,stroke:#3a9a3a,color:#111
    style K8S fill:#fff4e6,stroke:#d08a2c,color:#111
    style CI fill:#f3edff,stroke:#7d5fbf,color:#111
```

> **Reading the diagram:** solid arrows = Git/CI flow and ArgoCD reads · **thick** arrows = ArgoCD syncs to the cluster · dotted = image pulls, metric scraping, canary analysis, and the promotion/bootstrap steps. The app repo's `main` never receives automated commits — CI writes only to `agentbox-gitops`.

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

## Two repositories

Deployment state is split across two repos so the application repo's `main` never receives automated commits:

| Repo | Holds | Changes via |
|---|---|---|
| **`agentbox`** (this repo) | application code, Dockerfile, Helm chart, ArgoCD manifests, Terraform — *the what* | human PRs only |
| **[`agentbox-gitops`](https://github.com/kay-bluhuntr/agentbox-gitops)** | per-environment values + pinned image tags (`envs/dev`, `envs/prod`) — *which version runs where* | CI (dev tag) and promotion PRs (prod tag) |

CI builds the image and writes the new tag into `agentbox-gitops`, not here — so `git push` against this repo is never rejected by a CI bot commit. See [CI/CD pipeline](#cicd-pipeline) and [The GitOps platform](#the-gitops-platform).

---

## Prerequisites

| Path | Needs |
|---|---|
| Local dev | Docker (Compose v2) |
| kind cluster | Docker, [`kind`](https://kind.sigs.k8s.io/), `kubectl`, [`helm`](https://helm.sh/) v3 |
| Full GitOps platform | the above + [`gh`](https://cli.github.com/) (GitHub CLI), a GitHub account that can create repos/secrets |
| Working on the code | Python 3.11+ |

---

## Quickstart

There are three ways to run AgentBox, in increasing order of completeness. Pick one.

### A. Local dev — no cluster (fastest)

Runs the API + Postgres via docker-compose with the in-process "local" executor (workloads run as subprocesses, not Jobs). Good for working on the API/reconciler.

```bash
make dev      # builds + starts API + Postgres (creates .env from .env.example on first run)
make smoke    # submits a test session and polls it to success
```

`make smoke` targets `http://localhost:8080` (override with `AGENTBOX_HOST`); docker-compose publishes that port directly.

### B. Single-node kind — real Kubernetes, no GitOps

Spins up a kind cluster and deploys the **base chart** (plain `Deployment`, no ArgoCD/Prometheus/canary). This is the dependency-free path — nothing beyond kind is required.

```bash
make kind-up                                              # create cluster, build+load image, helm install
kubectl -n agentbox port-forward svc/agentbox 8080:80 &   # Service is ClusterIP
make smoke
```

> `make kind-up` builds the image locally and `kind load`s it, deploying with `image.pullPolicy=IfNotPresent` into the `agentbox` namespace. It also seeds an in-cluster Postgres + secret. **Don't** also run the full GitOps path (below) into the same `agentbox` namespace without first `helm uninstall agentbox -n agentbox` — two controllers owning the same release will fight.

### C. Full GitOps platform on kind

The real thing: ArgoCD pulls the chart + env values from Git and reconciles dev and prod, with Prometheus scraping and canary rollouts. This is the [setup guide below](#full-setup-from-scratch).

---

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

```json
{ "id": "a3f1c2d4-...", "state": "pending", "image": "python:3.12-slim", "attempt": 0, ... }
```

### Poll / logs / cancel

```bash
curl http://localhost:8080/v1/sessions/<id>
curl http://localhost:8080/v1/sessions/<id>/logs
curl -X DELETE http://localhost:8080/v1/sessions/<id>
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
| `AGENTBOX_DATABASE_URL` | `postgresql+psycopg://...` | Postgres connection string (local-dev default; real deploys inject from a Secret) |
| `AGENTBOX_EXECUTOR` | `kubernetes` | `kubernetes` or `local` (dev mode) |
| `AGENTBOX_EXEC_NAMESPACE` | `agentbox-exec` | Kubernetes namespace for workload Jobs |
| `AGENTBOX_DEFAULT_CPU_LIMIT` | `500m` | CPU cap per workload |
| `AGENTBOX_DEFAULT_MEMORY_LIMIT` | `512Mi` | Memory cap per workload |
| `AGENTBOX_MAX_TIMEOUT_SECONDS` | `3600` | Hard ceiling on session timeout |
| `AGENTBOX_DEFAULT_TIMEOUT_SECONDS` | `300` | Default if not specified per-request |
| `AGENTBOX_MAX_RETRIES_CEILING` | `5` | Max retries a caller can request |
| `AGENTBOX_RECONCILE_INTERVAL_SECONDS` | `2.0` | How often the reconciler polls |

---

## The GitOps platform

```
deploy/
  helm/agentbox/        # Helm chart (Deployment/Rollout, RBAC, NetworkPolicy,
                        #   ResourceQuota, ServiceMonitor, PrometheusRule)
  argocd/               # App-of-apps root, AppProject, dev/prod Applications
  terraform/            # cluster-level infra (VPC + EKS + RDS via community modules)

agentbox-gitops/        # SEPARATE repo — deployment state
  envs/dev/values.yaml  # fast iteration: plain Deployment, no canary
  envs/prod/values.yaml # full pattern: ServiceMonitor + alerts + canary rollout
```

The chart is **feature-flagged**: the same templates serve a bare kind cluster and a fully-instrumented production cluster. CRD-dependent features (`rollout.enabled`, `metrics.serviceMonitor.enabled`, `monitoring.prometheusRule.enabled`) default **off** in the base chart and are switched **on** in `agentbox-gitops`'s `envs/prod/values.yaml`.

### How sync works

Pull-based — nobody runs `helm`/`kubectl` against a cluster by hand. ArgoCD reconciles the cluster to Git:

```
git push (app repo) ─► CI builds + scans + pushes image ─► CI writes the image tag
                                                           to agentbox-gitops envs/dev
                                                                    │
                                                        ArgoCD detects the change
                                                                    │
                                                 ArgoCD syncs the cluster to Git
```

- **App-of-apps** — one root Application ([`argocd/root.yaml`](deploy/argocd/root.yaml)) points at [`argocd/apps/`](deploy/argocd/apps); adding/changing an environment is a PR against that directory.
- **Environments** — [`apps/dev.yaml`](deploy/argocd/apps/dev.yaml) and [`apps/prod.yaml`](deploy/argocd/apps/prod.yaml) are multi-source Applications: the **chart** comes from this repo, the per-env **values** from `agentbox-gitops` (the `$values` ref). Both auto-sync.
- **AppProject guardrail** ([`apps/project.yaml`](deploy/argocd/apps/project.yaml)) — fences every Application to the two AgentBox repos and the AgentBox namespaces, and whitelists `Namespace` as the only cluster-scoped resource it may create.
- **Sync waves** — `sync-wave: -1` applies the execution namespace, default-deny `NetworkPolicy`, `ResourceQuota`, and RBAC *before* the control plane (wave 0).

### Full setup from scratch

This reproduces, as repeatable steps, everything needed to bring up the full platform on a fresh kind cluster. Replace `kay-bluhuntr` with your GitHub org/user throughout.

> **One-time GitHub setup.** The manifests reference `github.com/kay-bluhuntr/agentbox` and `…/agentbox-gitops`. If you forked/renamed, update the `repoURL`s in `deploy/argocd/**` and `image.repository` in the chart/values, and the `agentbox-gitops` URLs in `.github/workflows/ci.yml` and `scripts/promote.sh`.

**1. Create the cluster and the base deployment**

```bash
make kind-up   # cluster + locally-built image + base chart in the `agentbox` namespace
```

If you intend to let ArgoCD own the `agentbox` namespace for prod, remove the helm-managed release first so the two don't conflict:

```bash
helm uninstall agentbox -n agentbox   # leaves the kubectl-applied Postgres + secret in place
```

**2. Install the controllers**

```bash
# ArgoCD (server-side apply: the ApplicationSet CRD is too large for client-side apply)
kubectl create namespace argocd
kubectl apply -n argocd --server-side -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml

# Argo Rollouts (canary controller + Rollout/AnalysisTemplate CRDs)
kubectl create namespace argo-rollouts
kubectl apply -n argo-rollouts -f https://github.com/argoproj/argo-rollouts/releases/latest/download/install.yaml

# Prometheus Operator + Prometheus + Grafana (provides ServiceMonitor/PrometheusRule CRDs)
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts && helm repo update
helm install kube-prometheus-stack prometheus-community/kube-prometheus-stack \
  -n monitoring --create-namespace --set alertmanager.enabled=false --wait
```

**3. Make the image pullable**

CI pushes a **multi-arch** image (`linux/amd64,linux/arm64`) to `ghcr.io/<owner>/agentbox`, so it runs on both real amd64 clusters and arm64 kind (Apple Silicon). GHCR packages are **private by default**, so the cluster needs pull access. Pick one:

- **Public package** (simplest for a demo): GitHub → your avatar → **Packages → agentbox → Package settings → Change visibility → Public**. Anonymous pulls then work everywhere.
- **Private + imagePullSecret**: create a `dockerconfigjson` Secret from a PAT with `read:packages` and reference it from the workload's service account.

**4. Give ArgoCD read access to both private Git repos**

ArgoCD's repo-server clones the chart (this repo) and the values (`agentbox-gitops`). Register a credential **template keyed to your org prefix** — this matches every repo under the org regardless of a trailing `.git`, which avoids a subtle gotcha where an exact-URL credential registered with `.git` won't match an Application `repoURL` written without it:

```bash
kubectl apply -f - <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: <owner>-repo-creds
  namespace: argocd
  labels:
    argocd.argoproj.io/secret-type: repo-creds   # credential TEMPLATE, matched by URL prefix
stringData:
  type: git
  url: https://github.com/<owner>
  username: <owner>
  password: <read-only PAT with repo:read>
EOF
```

**5. Give CI write access to the GitOps repo**

The `deploy-dev` CI job pushes the image-tag bump to `agentbox-gitops`, which the workflow's built-in `GITHUB_TOKEN` can't reach (different repo). Add a `GITOPS_TOKEN` secret to *this* repo — a fine-grained PAT with **Contents: write** on `agentbox-gitops`:

```bash
gh secret set GITOPS_TOKEN --repo <owner>/agentbox
```

**6. Bootstrap ArgoCD**

```bash
kubectl apply -f deploy/argocd/root.yaml   # app-of-apps → creates AppProject + dev/prod Applications
```

**7. Seed a database per ArgoCD-managed namespace**

The chart deploys only the control plane; it expects a Postgres reachable via the `agentbox-db` secret it reads. (Production would point this at managed Postgres — see [`deploy/terraform`](deploy/terraform).) For kind, seed a throwaway Postgres + secret in each target namespace (`agentbox-dev` for dev, `agentbox` for prod):

```bash
seed_db() {  # usage: seed_db <namespace>
  ns="$1"; pw="$(openssl rand -hex 16)"
  kubectl create namespace "$ns" --dry-run=client -o yaml | kubectl apply -f -
  kubectl -n "$ns" apply -f - <<YAML
apiVersion: apps/v1
kind: Deployment
metadata: {name: postgres}
spec:
  replicas: 1
  selector: {matchLabels: {app: postgres}}
  template:
    metadata: {labels: {app: postgres}}
    spec:
      containers:
        - name: postgres
          image: postgres:16-alpine
          env:
            - {name: POSTGRES_USER, value: agentbox}
            - {name: POSTGRES_PASSWORD, value: "$pw"}
            - {name: POSTGRES_DB, value: agentbox}
          ports: [{containerPort: 5432}]
---
apiVersion: v1
kind: Service
metadata: {name: postgres}
spec: {selector: {app: postgres}, ports: [{port: 5432}]}
YAML
  kubectl -n "$ns" create secret generic agentbox-db \
    --from-literal=database-url="postgresql+psycopg://agentbox:${pw}@postgres.${ns}.svc:5432/agentbox" \
    --from-literal=postgres-password="$pw" \
    --dry-run=client -o yaml | kubectl apply -f -
}

seed_db agentbox-dev
seed_db agentbox        # only if running prod here
```

**8. Verify dev is green**

```bash
kubectl -n argocd get applications        # agentbox-dev should reach Synced / Healthy
kubectl -n agentbox-dev get pods          # control-plane pod + postgres Running
kubectl -n agentbox-dev port-forward svc/agentbox 8080:80 &
make smoke
```

`agentbox-prod` will sit **OutOfSync** until you promote (its tag is `initial`, which was never built) — that's expected.

### Deploying to prod (promotion)

You never deploy to prod by hand. You **promote**: copy dev's current tag into `agentbox-gitops/envs/prod/values.yaml` via a PR, and ArgoCD canary-rolls it out on merge.

```bash
./scripts/promote.sh
# → opens a PR on agentbox-gitops (prod tag: initial → <dev tag>)
# → review + merge the PR
# → ArgoCD canary-deploys prod into the `agentbox` namespace
```

The first prod rollout goes straight to 100% (no prior version to canary against); subsequent promotions run the canary steps + analysis. Watch it:

```bash
kubectl -n argocd get app agentbox-prod -o jsonpath='{.status.sync.status}/{.status.health.status}{"\n"}'
kubectl argo rollouts get rollout agentbox -n agentbox   # if the kubectl-argo-rollouts plugin is installed
```

### Observability

The control plane exposes Prometheus metrics at `/metrics` ([`agentbox/observability.py`](agentbox/observability.py)):

| Metric | Meaning |
|---|---|
| `agentbox_sessions_created_total` | sessions accepted by the API |
| `agentbox_sessions_finished_total{state}` | sessions reaching a terminal state |
| `agentbox_session_retries_total` | retries scheduled by the reconciler |
| `agentbox_reconcile_loops_total` / `_errors_total` | control-loop liveness and failures |
| `agentbox_api_request_seconds` | API latency histogram (by method, route) |

With the observability flags on, the chart ships a **`ServiceMonitor`** (scrape `/metrics`) and a **`PrometheusRule`** with operational alerts (`AgentboxReconcilerStalled`, `AgentboxHighApiLatency`, `AgentboxSessionFailureRateHigh`) plus recording rules for the reconcile-success SLI and a Google-SRE **multi-window burn-rate SLO**. Label them to match your operator's selector (kube-prometheus-stack defaults to `release: kube-prometheus-stack`).

### Progressive delivery (canary)

With `rollout.enabled`, the control plane ships as an [Argo Rollouts](https://argoproj.github.io/rollouts/) `Rollout` instead of a `Deployment`. A new version takes `rollout.canaryWeight`% of traffic, then an `AnalysisTemplate` queries Prometheus for the control plane's *own* health — reconcile error ratio and API p99 latency — and **auto-rolls-back** if either regresses. Stable and canary share one pod spec via [`_helpers.tpl`](deploy/helm/agentbox/templates/_helpers.tpl). On in prod, off in dev.

### Accessing the dashboards

```bash
# ArgoCD UI — https://localhost:8081 (accept the self-signed cert)
kubectl -n argocd port-forward svc/argocd-server 8081:443
#   user: admin
#   pass: kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d; echo

# Prometheus — targets, rules, graph
kubectl -n monitoring port-forward svc/kube-prometheus-stack-prometheus 9090:9090

# Grafana — admin / prom-operator
kubectl -n monitoring port-forward svc/kube-prometheus-stack-grafana 3000:80
```

---

## CI/CD pipeline

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs on every push/PR:

1. **test** — lint (ruff) + the suite on SQLite *and* a real Postgres service container.
2. **security** — Checkov scans `deploy/` (Terraform + K8s manifests). [`.trivyignore`](.trivyignore) documents accepted base-image CVEs.
3. **build** (main only) — builds the image, scans it with **Trivy** (HIGH/CRITICAL fail the build), then pushes a **multi-arch** (`amd64`+`arm64`) image to GHCR tagged with the **short commit SHA** and `:latest`.
4. **deploy-dev** (main only) — checks out `agentbox-gitops` using `GITOPS_TOKEN` and pins the new short-SHA tag into `envs/dev/values.yaml`. ArgoCD takes it from there. **No commit ever lands on this repo's `main`.**

Promotion to prod is a separate, human-reviewed PR against `agentbox-gitops` (`scripts/promote.sh`).

---

## Troubleshooting

Issues you're likely to hit on a fresh cluster (all encountered and resolved during bring-up):

| Symptom | Cause | Fix |
|---|---|---|
| `kubectl` commands fail / connection refused | Wrong context (e.g. `minikube`) | `kubectl config use-context kind-agentbox` |
| ArgoCD install: `metadata.annotations: Too long` | ApplicationSet CRD too big for client-side apply | install with `kubectl apply --server-side` (add `--force-conflicts` if re-applying) |
| App stuck `Unknown` / `ComparisonError: authentication required: Repository not found` | ArgoCD can't read the private repo, or the repo credential URL has a `.git` the Application `repoURL` lacks | register an org-prefix `repo-creds` template (step 4) so matching ignores the `.git` suffix |
| Pod `ImagePullBackOff` → `401 Unauthorized` from ghcr.io | GHCR package is private with no pull creds | make the package public, or add an `imagePullSecret` (step 3) |
| Pod `ErrImagePull` → `no match for platform in manifest` | image is single-arch (amd64) but the node is arm64 (Apple Silicon kind) | ensure CI built multi-arch (it does); for a local image use `docker buildx --platform` or `kind load` an arm64 build |
| Sync stuck retrying → `namespaces "agentbox-exec" not found` | a sync deleted the exec namespace mid-flight; RBAC reconcile races ahead of namespace creation | `kubectl create namespace agentbox-exec` to unblock, then let the sync finish |
| Session submit fails with `(401) Unauthorized` from the K8s API | controller pods hold a stale token after their ServiceAccount was deleted+recreated | restart the pods: `kubectl -n <ns> delete pod -l app.kubernetes.io/name=agentbox` |
| `make kind-up` pod can't pull the GHCR `:latest`/SHA tag | base chart defaults to `pullPolicy: Always` | `kind-up` already overrides to `IfNotPresent` for the locally-loaded image; for ArgoCD dev, `envs/dev/values.yaml` sets `IfNotPresent` |
| Two `agentbox` controllers fighting in one namespace | a manual `helm install` and the ArgoCD app both own the release | `helm uninstall agentbox -n <ns>`, then let ArgoCD self-heal |
| `git push` to this repo rejected (`fetch first`) repeatedly | a CI bot used to commit to `main` | resolved by the two-repo split; otherwise `git pull --rebase` (this repo sets `pull.rebase=true`) |

---

## Development

Requires Python 3.11+. If your system default is older, create a venv explicitly:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

make test       # run the test suite (SQLite + stub executor — no cluster needed)
make lint       # ruff check
make typecheck  # mypy
```
