# AgentBox Roadmap — GPU & Inference Platform Extension

> **Purpose:** evolve AgentBox from a sandboxed batch-execution control plane into a
> GPU-aware inference platform, in four phases. Each phase is scoped so it can be
> built and verified incrementally, and each has an explicit **verification ledger**
> distinguishing what is *executed/tested* from what is *written but not exercised*.
> This file is written to be worked against with Claude Code — each task references
> real paths in this repo.

**Conventions for all phases**
- Every new behaviour lands with tests in `tests/` (extend the existing pytest suite; CI is `.github/workflows/ci.yml`).
- Helm changes go in `deploy/helm/agentbox/`; keep `values.yaml` backwards-compatible (GPU features default **off**).
- Update the README's honesty ledger at the end of each phase: *verified by execution* vs *written, not yet run end-to-end*.
- UK/AWS assumptions where cloud is needed: `eu-west-2`, spot GPU instances, budget ceiling per phase noted.

---

## Phase 1 — GPU-aware session scheduling *(no GPU required; ~2–3 evenings)*

**Goal:** the control plane can accept a GPU request on session creation and renders a Job spec correctly pinned to a tainted GPU node pool. Entirely testable with spec assertions — zero cloud cost.

### Tasks
1. **Schema** — `agentbox/api/schemas.py`: add optional `gpu` block to session-create request:
   `{ "gpu": { "count": 1, "type": "nvidia-t4" } }`. Validate count ∈ {0,1} for now (single-GPU sessions; document why — MIG/time-slicing is Phase 4 territory).
2. **Model + state** — `agentbox/models.py`, `agentbox/state.py`: persist GPU request on the session record (migration if schema is versioned).
3. **Job rendering** — `agentbox/orchestrator/k8s.py`: when `gpu.count > 0`, render:
   - `resources.limits."nvidia.com/gpu"` = count (limits only — GPU is not overcommittable; add code comment saying so)
   - `tolerations` for taint `agentbox.io/gpu=true:NoSchedule`
   - `nodeAffinity` requiring label `agentbox.io/node-pool=gpu`
   - **All three of taint-toleration + affinity** — toleration alone permits, affinity pins.
4. **Helm** — `deploy/helm/agentbox/values.yaml` + templates: add `gpu.enabled` flag gating (a) NVIDIA device-plugin DaemonSet manifest, (b) documented expectations for the GPU node pool (taint + label above). Default `false`.
5. **Tests** — `tests/test_orchestrator_gpu.py` (new): assert rendered Job spec for GPU and non-GPU sessions; assert a GPU request with `gpu.enabled=false` config is rejected with a clear API error, not silently scheduled to CPU.
6. **Docs** — README section: how a GPU node pool must be tainted/labelled; one paragraph on why GPU limits ≠ requests semantics.

### Definition of done
- [ ] CI green with new tests
- [ ] `kind` cluster smoke (`scripts/kind-up.sh` + `scripts/smoke.sh`) still passes with `gpu.enabled=false`
- [ ] Ledger updated: *“GPU Job rendering verified by spec-assertion tests; not yet exercised on a physical GPU node.”*

**Cost: £0.**

---

## Phase 2 — Inference serving session type *(the core of the extension; ~1–2 weeks of evenings)*

**Goal:** a new long-lived session type that serves a model over HTTP (vLLM), managed by the existing reconciler lifecycle — this is what makes AgentBox an *inference platform* rather than a batch runner.

### Tasks
1. **Session kind** — extend schema/state with `kind: "batch" | "serve"` (default `batch`, fully backwards-compatible). `serve` sessions have no run-to-completion semantics; lifecycle is `pending → serving → stopped/failed`.
2. **Workload rendering** — `agentbox/orchestrator/k8s.py`: `serve` renders a **Deployment + ClusterIP Service** (not a Job). Image: `vllm/vllm-openai`, model configurable (default a small open model, e.g. Qwen2.5-0.5B-Instruct, so CPU validation is feasible).
3. **Model-weights caching** — mount a PVC (or `emptyDir` + init-container download for v1) so pod restarts don’t re-pull gigabytes. Document the trade-off in-code; PVC is the target, note StorageClass expectations in Helm values.
4. **Readiness that means ready** — readinessProbe against vLLM’s `/health`, generous `startupProbe` (model load is slow); **no liveness probe on model-load path** — killing a pod mid-load creates a crash loop (this is the Task-1 lesson applied for real).
5. **Reconciler** — `agentbox/reconciler.py`: manage `serve` lifecycle — detect Deployment availability for `serving`, TTL/idle-stop support, graceful drain on stop (preStop + terminationGracePeriodSeconds in the rendered spec).
6. **Sandbox parity** — the hardened defaults from batch Jobs (non-root, read-only rootfs where vLLM allows, default-deny NetworkPolicy with an explicit ingress allow from the AgentBox API namespace only, per-namespace quotas) apply to serve pods too. Tests assert this.
7. **Platform observability for the new session kind** — `agentbox/observability.py`: extend the existing Prometheus instrumentation to cover `serve` sessions — counters/gauges for sessions by kind and state (`serving`, `stopped`, `failed`), histogram for time-from-create-to-serving (model-load latency as seen by the platform), counter for TTL/idle stops, and reconciler visibility (reconcile-loop errors labelled by session kind). Structured log lines for GPU scheduling decisions from Phase 1 ("session X requested GPU, rendered toleration+affinity for pool Y"). Extend the existing `prometheusrule.yaml` with one alert: serve sessions stuck in `pending` beyond a threshold. All testable at £0 on kind — this is *platform* observability; *GPU/inference* metrics (DCGM, vLLM) stay in Phase 3 where a real GPU exists to emit them.
8. **Tests** — extend suite: lifecycle state machine for `serve`, rendered Deployment/Service specs, TTL stop, reconciler crash-recovery for serve sessions (reuse the advisory-lock patterns from existing `tests/test_reconciler.py`), plus assertions that the new metrics register and increment.
9. **CPU-first validation** — run the small model CPU-only on `kind`/docker-compose to prove the plumbing (slow inference is fine; the platform is what’s under test).

### Definition of done
- [ ] `serve` session creatable via API; reaches `serving`; responds to an OpenAI-format completion request on the ClusterIP (validated CPU-only, locally)
- [ ] Idle/TTL stop works; drain verified locally
- [ ] Ledger: *“serve lifecycle + specs verified in CI and CPU-only on kind; GPU-backed serving pending Phase 3 validation run.”*

**Cost: £0 (CPU-only validation).**

---

## Phase 3 — GPU validation run + GPU observability *(~1 weekend + small spend)*

**Goal:** exercise Phases 1–2 on a real GPU once, and wire GPU/inference metrics into the existing Prometheus stack (`agentbox/observability.py`, `deploy/helm/.../servicemonitor.yaml`, `prometheusrule.yaml`).

### Tasks
1. **One-off validation environment** — extend `deploy/terraform/` with an optional module: single **g4dn.xlarge spot** node group in `eu-west-2` (T4 GPU), tainted/labelled per Phase 1. Spot price is volatile — check at run time; historically very roughly **£0.15–0.25/hr**, so an afternoon of validation ≈ **£1–3**, full weekend of on/off runs comfortably **< £20**. Destroy when done (`terraform destroy` is part of the run-book, not an afterthought).
2. **Validation script** — `scripts/gpu-validate.sh`: create a GPU `serve` session, assert scheduling landed on the GPU node, run a completion request, capture tokens/sec, tear down.
3. **DCGM exporter** — add NVIDIA DCGM exporter to the Helm chart behind `gpu.enabled`; scrape via the existing ServiceMonitor pattern. Key series: GPU utilisation, GPU memory, temperature.
4. **vLLM metrics** — scrape vLLM’s Prometheus endpoint: time-to-first-token, tokens/sec, running/waiting request queue depth.
5. **Inference SLO** — define one honestly: e.g. *p95 time-to-first-token < 2s at concurrency 4 on T4 with the chosen model* — measured, not aspirational. Add a PrometheusRule alert on queue-depth saturation.
6. **Grafana dashboard JSON** committed under `deploy/observability/` (new dir): one dashboard — GPU util/memory + TTFT/tokens-per-sec + queue depth.

### Definition of done
- [ ] Validation run executed on real T4; results (tokens/sec, TTFT) recorded in README
- [ ] Ledger upgraded: *“GPU scheduling and vLLM serving verified by execution on g4dn spot (date, instance, model); MIG/time-slicing not exercised.”*
- [ ] Cloud resources destroyed; cost noted honestly in the ledger

**Budget ceiling: £20.**

---

## Phase 4 — Multi-tenant GPU economics *(design + selective execution; ongoing)*

**Goal:** the differentiator layer — cost-aware GPU multi-tenancy. Mostly design/writing with one cheap executable slice; this maps directly to real interview questions (“how do you share GPUs safely?” / “what does inference cost?”).

### Tasks
1. **Design doc** — `docs/gpu-multitenancy.md`: compare **time-slicing** (soft sharing, no memory isolation — fine for trusted bursty workloads) vs **MIG** (hard partitions, A100/H100-class only — the true multi-tenant answer) vs **whole-GPU per tenant** (simplest, most expensive). Recommend per AgentBox’s threat model: untrusted code ⇒ whole-GPU or MIG only; time-slicing explicitly rejected for untrusted tenants — write down *why* (shared memory = data-exfil risk).
2. **Executable slice** — implement **time-slicing config** for the T4 validation node (device-plugin ConfigMap), demo two concurrent low-priority sessions sharing one GPU, and document the observed interference. Cheap (£ few) and turns the doc from theory into a tested claim.
3. **Cost model** — `docs/inference-economics.md`: per-1k-tokens cost on the validated T4 setup vs on-demand pricing vs a managed endpoint (e.g. Bedrock) for the same model class. GBP throughout. One honest table beats ten paragraphs.
4. **Queue/priority (stretch)** — session priority classes and preemption for GPU contention in the reconciler. Design first; implement only if it stays simple.

### Definition of done
- [ ] Both docs merged; time-slicing demo executed and written up with measured numbers
- [ ] README front page updated: AgentBox described as *“sandboxed execution + GPU inference control plane”*, with the phase ledger linked

---

## Working with Claude Code on this roadmap

- Work one phase at a time; within a phase, one numbered task per session/PR. Feed Claude Code the task text plus the referenced files — the paths above are exact.
- Ask it to write the tests **first or alongside**, never after — the spec-assertion tests in Phases 1–2 are the backbone of the honesty ledger.
- After each merged PR, have it update the README ledger line for the phase. If a claim isn’t backed by an executed test or a recorded run, it goes in the *written-not-verified* column — no exceptions.
- Suggested branch naming: `phase-1/gpu-schema`, `phase-2/serve-lifecycle`, etc., so the commit history itself tells the story.
