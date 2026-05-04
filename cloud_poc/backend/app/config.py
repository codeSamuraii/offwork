"""Configuration for the local cloud proof-of-concept."""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    mongodb_uri: str = os.environ.get("PYFUSE_CLOUD_MONGODB_URI", "mongodb://localhost:27017")
    mongodb_database: str = os.environ.get("PYFUSE_CLOUD_MONGODB_DB", "pyfuse_cloud")
    kubectl_enabled: bool = os.environ.get("PYFUSE_CLOUD_DISABLE_KUBECTL", "0") != "1"
    kubectl_binary: str = os.environ.get("PYFUSE_CLOUD_KUBECTL", "kubectl")
    kubernetes_context: str = os.environ.get("PYFUSE_CLOUD_KUBE_CONTEXT", "docker-desktop")
    kubernetes_namespace: str = os.environ.get("PYFUSE_CLOUD_NAMESPACE", "pyfuse-cloud")
    broker_public_base_url: str = os.environ.get("PYFUSE_CLOUD_PUBLIC_BROKER_URL", "http://localhost:8000/api/v1/broker")
    # When the API runs on the host and workers run in a local kind/docker-desktop
    # cluster, ``host.docker.internal`` resolves to the host. For an in-cluster
    # API deployment, override with ``http://pyfuse-cloud-api.pyfuse-cloud.svc.cluster.local:8000/api/v1/broker``.
    broker_internal_base_url: str = os.environ.get(
        "PYFUSE_CLOUD_INTERNAL_BROKER_URL",
        "http://host.docker.internal:8000/api/v1/broker",
    )
    worker_image: str = os.environ.get("PYFUSE_CLOUD_WORKER_IMAGE", "pyfuse-cloud-worker:dev")
    worker_idle_seconds: int = int(os.environ.get("PYFUSE_CLOUD_IDLE_SECONDS", "300"))
    task_poll_interval: float = float(os.environ.get("PYFUSE_CLOUD_TASK_POLL_INTERVAL", "1.0"))
