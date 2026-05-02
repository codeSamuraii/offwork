"""Kubernetes worker orchestration helpers for the local proof-of-concept."""

import subprocess
from textwrap import dedent

from .config import Settings


class WorkerOrchestrator:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def ensure_worker(self, deployment_name: str, api_key: str) -> None:
        if not self._settings.kubectl_enabled:
            return
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
                        readOnlyRootFilesystem: true
                        runAsNonRoot: true
                        runAsUser: 10001
                        runAsGroup: 10001
                        capabilities:
                          drop: ["ALL"]
                        seccompProfile:
                          type: RuntimeDefault
            """
        ).strip()
        subprocess.run(
            [self._settings.kubectl_binary, "apply", "-f", "-"],
            input=f"{manifest}\n",
            text=True,
            check=True,
        )

    def scale_worker(self, deployment_name: str, replicas: int) -> None:
        if not self._settings.kubectl_enabled:
            return
        subprocess.run(
            [
                self._settings.kubectl_binary,
                "scale",
                f"deployment/{deployment_name}",
                "-n",
                self._settings.kubernetes_namespace,
                f"--replicas={replicas}",
            ],
            check=True,
        )
