"""Kubernetes executor.

Each session attempt becomes a Kubernetes Job in a dedicated execution
namespace. Sandboxing is enforced at submission time, not by convention:

- Hard CPU/memory limits on every container.
- `activeDeadlineSeconds` as a kernel-level backstop for the session
  timeout (the reconciler enforces the soft timeout first).
- Non-root, no privilege escalation, all capabilities dropped,
  read-only root filesystem with a scratch volume at /tmp.
- `automountServiceAccountToken: false` — workloads get no API credentials.
- The execution namespace ships with a default-deny NetworkPolicy and a
  ResourceQuota (see deploy/helm), so a runaway or hostile workload can't
  exfiltrate over the network or starve its neighbours.
"""

import structlog
from kubernetes import client, config

from agentbox.config import get_settings
from agentbox.orchestrator.base import WorkloadPhase, WorkloadSpec, WorkloadStatus

log = structlog.get_logger()

MANAGED_BY = "agentbox"


class KubernetesExecutor:
    def __init__(self) -> None:
        settings = get_settings()
        if settings.kubeconfig:
            config.load_kube_config(config_file=settings.kubeconfig)
        else:
            try:
                config.load_incluster_config()
            except config.ConfigException:
                config.load_kube_config()
        self.namespace = settings.exec_namespace
        self.service_account = settings.exec_service_account
        self.batch = client.BatchV1Api()
        self.core = client.CoreV1Api()

    # -- naming -------------------------------------------------------------

    @staticmethod
    def job_name(session_id: str, attempt: int) -> str:
        return f"abx-{session_id[:8]}-a{attempt}"

    # -- Executor protocol ---------------------------------------------------

    def submit(self, spec: WorkloadSpec) -> str:
        name = self.job_name(spec.session_id, spec.attempt)
        job = self._build_job(name, spec)
        self.batch.create_namespaced_job(namespace=self.namespace, body=job)
        log.info("job_submitted", job=name, session_id=spec.session_id, attempt=spec.attempt)
        return name

    def status(self, ref: str) -> WorkloadStatus:
        try:
            job = self.batch.read_namespaced_job_status(name=ref, namespace=self.namespace)
        except client.ApiException as exc:
            if exc.status == 404:
                return WorkloadStatus(phase=WorkloadPhase.GONE)
            raise

        status = job.status
        if status.succeeded:
            return WorkloadStatus(phase=WorkloadPhase.SUCCEEDED, exit_code=0)
        if status.failed:
            reason = self._failure_reason(ref)
            return WorkloadStatus(phase=WorkloadPhase.FAILED, exit_code=1, reason=reason)
        if status.active:
            return WorkloadStatus(phase=WorkloadPhase.RUNNING)
        return WorkloadStatus(phase=WorkloadPhase.WAITING)

    def logs(self, ref: str, tail: int = 1000) -> str:
        pods = self.core.list_namespaced_pod(
            namespace=self.namespace, label_selector=f"job-name={ref}"
        )
        if not pods.items:
            return ""
        pod = sorted(pods.items, key=lambda p: p.metadata.creation_timestamp)[-1]
        try:
            return self.core.read_namespaced_pod_log(
                name=pod.metadata.name, namespace=self.namespace, tail_lines=tail
            )
        except client.ApiException:
            return ""

    def cancel(self, ref: str) -> None:
        self._delete_job(ref)

    def cleanup(self, ref: str) -> None:
        self._delete_job(ref)

    # -- internals ------------------------------------------------------------

    def _delete_job(self, ref: str) -> None:
        try:
            self.batch.delete_namespaced_job(
                name=ref,
                namespace=self.namespace,
                propagation_policy="Background",
            )
            log.info("job_deleted", job=ref)
        except client.ApiException as exc:
            if exc.status != 404:
                raise

    def _failure_reason(self, ref: str) -> str | None:
        pods = self.core.list_namespaced_pod(
            namespace=self.namespace, label_selector=f"job-name={ref}"
        )
        for pod in pods.items:
            for cs in pod.status.container_statuses or []:
                if cs.state.terminated and cs.state.terminated.reason:
                    return cs.state.terminated.reason
        return None

    def _build_job(self, name: str, spec: WorkloadSpec) -> client.V1Job:
        labels = {
            "app.kubernetes.io/managed-by": MANAGED_BY,
            "agentbox.io/session-id": spec.session_id,
            "agentbox.io/attempt": str(spec.attempt),
        }
        container = client.V1Container(
            name="workload",
            image=spec.image,
            command=spec.command,
            env=[client.V1EnvVar(name=k, value=v) for k, v in spec.env.items()],
            resources=client.V1ResourceRequirements(
                requests={"cpu": "50m", "memory": "64Mi"},
                limits={"cpu": spec.cpu_limit, "memory": spec.memory_limit},
            ),
            security_context=client.V1SecurityContext(
                run_as_non_root=True,
                run_as_user=65534,
                allow_privilege_escalation=False,
                read_only_root_filesystem=True,
                capabilities=client.V1Capabilities(drop=["ALL"]),
                seccomp_profile=client.V1SeccompProfile(type="RuntimeDefault"),
            ),
            volume_mounts=[client.V1VolumeMount(name="scratch", mount_path="/tmp")],
        )
        pod_spec = client.V1PodSpec(
            restart_policy="Never",
            service_account_name=self.service_account,
            automount_service_account_token=False,
            containers=[container],
            volumes=[
                client.V1Volume(
                    name="scratch",
                    empty_dir=client.V1EmptyDirVolumeSource(size_limit="256Mi"),
                )
            ],
        )
        return client.V1Job(
            metadata=client.V1ObjectMeta(name=name, labels=labels),
            spec=client.V1JobSpec(
                # Retries are owned by the reconciler so attempts are recorded
                # in Postgres; the Job itself never retries.
                backoff_limit=0,
                active_deadline_seconds=spec.timeout_seconds + 30,
                ttl_seconds_after_finished=24 * 3600,  # belt-and-braces; reconciler cleans first
                template=client.V1PodTemplateSpec(
                    metadata=client.V1ObjectMeta(labels=labels),
                    spec=pod_spec,
                ),
            ),
        )
