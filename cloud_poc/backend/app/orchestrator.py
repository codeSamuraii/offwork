"""Kubernetes worker orchestration helpers for the local proof-of-concept."""

import logging
import subprocess
from textwrap import dedent

from .config import Settings

logger = logging.getLogger(__name__)

_BLOCKED_CONTEXTS = frozenset({"aks-ani-staging", "aks-ani-prod"})
_LOCAL_CONTEXT_NAMES = frozenset({"docker-desktop", "minikube", "rancher-desktop"})
_LOCAL_CONTEXT_PREFIXES = ("kind-", "k3d-")


class WorkerOrchestrator:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        logger.info(
            "orchestrator init  context=%s namespace=%s image=%s kubectl_enabled=%s",
            settings.kubernetes_context,
            settings.kubernetes_namespace,
            settings.worker_image,
            settings.kubectl_enabled,
        )

    def _kubectl_command(self, *args: str) -> list[str]:
        context = self._settings.kubernetes_context.strip()
        if not context:
            raise RuntimeError("PYFUSE_CLOUD_KUBE_CONTEXT must be set to a local Kubernetes context")
        if context in _BLOCKED_CONTEXTS:
            raise RuntimeError(f"refusing to use blocked Kubernetes context {context!r}")
        if context not in _LOCAL_CONTEXT_NAMES and not context.startswith(_LOCAL_CONTEXT_PREFIXES):
            raise RuntimeError(
                f"refusing to use non-local Kubernetes context {context!r}; set PYFUSE_CLOUD_KUBE_CONTEXT to a local context"
            )
        return [self._settings.kubectl_binary, "--context", context, *args]

    def _run_kubectl(self, *args: str, input_text: str | None = None, ignore_not_found: bool = False) -> None:
        logger.debug("kubectl %s", " ".join(args))
        completed = subprocess.run(
            self._kubectl_command(*args),
            input=input_text,
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode == 0:
            return
        stderr = completed.stderr.strip()
        if ignore_not_found and "not found" in stderr.lower():
            logger.debug("kubectl %s -> not found (ignored)", " ".join(args))
            return
        logger.error("kubectl %s failed: %s", " ".join(args), stderr or completed.stdout.strip())
        raise RuntimeError(stderr or completed.stdout.strip() or f"kubectl {' '.join(args)} failed")

    def _ensure_namespace(self) -> None:
        manifest = dedent(
            f"""
            apiVersion: v1
            kind: Namespace
            metadata:
              name: {self._settings.kubernetes_namespace}
            """
        ).strip()
        self._run_kubectl("apply", "-f", "-", input_text=f"{manifest}\n")

    def ensure_worker(self, deployment_name: str, api_key: str) -> None:
        if not self._settings.kubectl_enabled:
            logger.debug("ensure_worker(%s) skipped (kubectl disabled)", deployment_name)
            return
        logger.info("ensure_worker(%s) applying deployment manifest", deployment_name)
        self._ensure_namespace()
        manifest = dedent(
            f"""
            apiVersion: apps/v1
            kind: Deployment
            metadata:
              name: {deployment_name}
              namespace: {self._settings.kubernetes_namespace}
              labels:
                app: pyfuse-cloud-worker
                pyfuse-user: {deployment_name}
            spec:
              replicas: 0
              selector:
                matchLabels:
                  app: pyfuse-cloud-worker
                  pyfuse-user: {deployment_name}
              template:
                metadata:
                  labels:
                    app: pyfuse-cloud-worker
                    pyfuse-user: {deployment_name}
                spec:
                  automountServiceAccountToken: false
                  containers:
                    - name: worker
                      image: {self._settings.worker_image}
                      imagePullPolicy: IfNotPresent
                      args:
                        - python
                        - -m
                        - pyfuse
                        - worker
                        - --backend
                        - {self._settings.broker_internal_base_url}?api_key={api_key}
                        - -c
                        - "1"
                      resources:
                        requests:
                          cpu: "100m"
                          memory: "256Mi"
                        limits:
                          cpu: "1000m"
                          memory: "1Gi"
                      securityContext:
                        allowPrivilegeEscalation: false
                        capabilities:
                          drop: ["ALL"]
                        seccompProfile:
                          type: RuntimeDefault
            """
        ).strip()
        self._run_kubectl("apply", "-f", "-", input_text=f"{manifest}\n")

    def scale_worker(self, deployment_name: str, replicas: int) -> None:
        if not self._settings.kubectl_enabled:
            logger.debug("scale_worker(%s, %d) skipped (kubectl disabled)", deployment_name, replicas)
            return
        logger.info("scale_worker(%s) -> replicas=%d", deployment_name, replicas)
        self._run_kubectl(
            "scale",
            f"deployment/{deployment_name}",
            "-n",
            self._settings.kubernetes_namespace,
            f"--replicas={replicas}",
            ignore_not_found=replicas == 0,
        )
