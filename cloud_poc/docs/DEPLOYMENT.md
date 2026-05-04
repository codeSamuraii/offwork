# Deployment

This document describes the local proof-of-concept architecture and the path
to a production deployment on Google Cloud Platform.

## Local architecture

```
                 ┌─────────────────────────────────────────────┐
                 │ Developer host (macOS, Docker Desktop)      │
                 │                                             │
   browser ───►  │  React/Vite frontend  (npm run dev :5173)   │
                 │           │                                 │
                 │           ▼                                 │
   pyfuse client │  FastAPI control plane  (uvicorn :8000)     │
   ────────────► │     • /api/v1/users/*                       │
                 │     • /api/v1/broker/*  (HTTP backend)      │
                 │     • /api/v1/usage/*                       │
                 │           │                  │              │
                 │           ▼                  ▼              │
                 │  MongoDB :27017       kubectl ─► local k8s  │
                 │  (users, tasks,                ▲            │
                 │   schedules, throttles)        │            │
                 │                                │            │
                 │  ┌─────────────────────────────┴─────────┐  │
                 │  │ Namespace: pyfuse-cloud               │  │
                 │  │   Deployment per user (scale 0↔1)     │  │
                 │  │     pyfuse-worker-<userid12>          │  │
                 │  │       ▼ host.docker.internal:8000     │  │
                 │  │     pyfuse worker container           │  │
                 │  └───────────────────────────────────────┘  │
                 └─────────────────────────────────────────────┘
```

### Components

| Component        | Process                                   | Code                                                       |
|------------------|-------------------------------------------|------------------------------------------------------------|
| Control plane    | `uvicorn cloud_poc.backend.app.main:app`  | [cloud_poc/backend/app/](../backend/app/)                  |
| Orchestrator     | thin wrapper around `kubectl`             | [orchestrator.py](../backend/app/orchestrator.py)          |
| MongoDB          | host service on `:27017`                  | —                                                          |
| Frontend         | Vite dev server on `:5173`                | [cloud_poc/frontend/](../frontend/)                        |
| Worker pods      | `python -m pyfuse worker --backend …`     | [cloud_poc/worker/Dockerfile](../worker/Dockerfile)        |
| Worker image     | `pyfuse-cloud-worker:dev` in containerd   | preloaded with `ctr images import`                         |

### Request lifecycle (cold path)

1. Client calls `pyfuse.connect("http://localhost:8000/api/v1/broker?api_key=…")`
   then `await fn.run(...)`.
2. The `HttpBackend` POSTs to `/tasks` with the serialized task envelope.
3. Control plane stores the task in MongoDB, marks the user active, calls
   `orchestrator.ensure_worker(...)` (idempotent apply) and
   `orchestrator.scale_worker(..., replicas=1)`.
4. Kubernetes pulls the cached worker image, starts the pod, the worker
   long-polls `/tasks/claim` and the control plane hands over the task.
5. The worker reconstructs the function, `pip install`s any missing
   dependencies, executes, POSTs the result envelope to `/tasks/<id>/result`.
6. The client's `get_result` long-poll returns the value.

### Request lifecycle (sleep)

A background task (`worker_reaper` in
[main.py](../backend/app/main.py)) wakes once per `PYFUSE_CLOUD_TASK_POLL_INTERVAL`
seconds. Any user whose `last_worker_activity_at` predates
`now - PYFUSE_CLOUD_IDLE_SECONDS` has their deployment scaled to zero. New
submissions immediately scale the deployment back to one.

### Trust boundaries

- API keys travel in the `X-Pyfuse-API-Key` header (the `HttpBackend` strips
  them from the URL automatically).
- Worker pods are isolated per user via Kubernetes deployments labelled
  `pyfuse-user=<deployment-name>`.
- `automountServiceAccountToken: false` and dropped Linux capabilities limit
  the pod's reach into the cluster.
- HMAC task signing remains available end-to-end (see [docs/SIGNING.md](../../docs/SIGNING.md)).

### Known PoC limitations

- Control plane orchestrates Kubernetes via the local `kubectl` binary; not
  suitable beyond a single operator's machine.
- API keys are stored in cleartext in MongoDB.
- No TLS, no rate limiting, no audit log.
- Worker pods have a writable root filesystem (required by `pip install` at
  runtime). Tighten with a cached venv or a per-user PVC in production.

---

## GCP deployment

This section sketches a production layout on Google Cloud. All commands assume
a project `${PROJECT_ID}` and a region `${REGION}` (for example
`us-central1`).

### Target architecture

```
                ┌──────── Cloud Load Balancer (HTTPS, managed cert) ────────┐
                │                                                           │
                ▼                                                           ▼
   GKE Service: pyfuse-cloud-frontend          GKE Service: pyfuse-cloud-api
   (static, served by nginx or Cloud CDN)      (FastAPI, 2+ replicas, HPA)
                                                            │
                                                            │ kube-rbac
                                                            ▼
                                              GKE Deployments (per-user)
                                              namespace: pyfuse-cloud
                                              one Deployment per user, scale 0↔N
                                                            │
                                                            ▼
                              MongoDB Atlas (or Cloud Memorystore for valkey)
```

### Key swaps from the local PoC

| Concern                  | Local PoC                              | Production on GCP                                                 |
|--------------------------|----------------------------------------|-------------------------------------------------------------------|
| Cluster                  | Docker Desktop k8s                     | GKE Autopilot (managed) or GKE Standard with regional node pool   |
| Image registry           | local containerd                       | Artifact Registry (`${REGION}-docker.pkg.dev/${PROJECT_ID}/pyfuse`) |
| Frontend hosting         | Vite dev server                        | nginx + static build, behind Cloud CDN, optional Cloud Armor      |
| API hosting              | uvicorn `--reload`                     | uvicorn with `--workers N` in a Deployment, fronted by ClusterIP  |
| Database                 | MongoDB on host                        | MongoDB Atlas private endpoint, or Firestore (rewrite the DAL)    |
| Secrets                  | MongoDB cleartext                      | Secret Manager + Workload Identity                                |
| TLS                      | none                                   | Google-managed certificate on the HTTPS LB                        |
| DNS                      | localhost                              | Cloud DNS                                                         |
| Observability            | stderr                                 | Cloud Logging (JSON logs), Cloud Monitoring, error reporting      |
| Worker pod orchestration | `kubectl` from the API process         | kubernetes Python client + Workload Identity ServiceAccount       |
| Worker pod isolation     | namespace + capability drops           | additionally: gVisor (`runtimeClass: gvisor`) sandbox per worker  |

### Step-by-step

1. **Project + APIs**
   ```bash
   gcloud config set project ${PROJECT_ID}
   gcloud services enable \
       container.googleapis.com \
       artifactregistry.googleapis.com \
       secretmanager.googleapis.com \
       certificatemanager.googleapis.com \
       compute.googleapis.com
   ```
2. **Artifact Registry repository**
   ```bash
   gcloud artifacts repositories create pyfuse \
       --repository-format=docker --location=${REGION}
   gcloud auth configure-docker ${REGION}-docker.pkg.dev
   ```
3. **Build and push the worker image**
   ```bash
   IMAGE=${REGION}-docker.pkg.dev/${PROJECT_ID}/pyfuse/cloud-worker:$(git rev-parse --short HEAD)
   docker build -t ${IMAGE} -f cloud_poc/worker/Dockerfile .
   docker push ${IMAGE}
   ```
   Apply the same to a `cloud-api` image built from a small wrapper around
   `cloud_poc/backend`.
4. **GKE cluster (Autopilot is the simplest)**
   ```bash
   gcloud container clusters create-auto pyfuse \
       --location=${REGION} \
       --release-channel=regular
   gcloud container clusters get-credentials pyfuse --location=${REGION}
   ```
5. **Workload Identity for the control plane**

   Create a Google service account that can manage Deployments in the
   `pyfuse-cloud` namespace:
   ```bash
   gcloud iam service-accounts create pyfuse-cloud-api
   ```
   Bind a Kubernetes ServiceAccount to it (Workload Identity), then attach
   minimal RBAC: `Role` allowing `get,list,create,patch,delete` on
   `deployments.apps` in the `pyfuse-cloud` namespace, and a `RoleBinding`
   to that ServiceAccount.

   Replace the orchestrator's `subprocess.run("kubectl", ...)` with the
   Python `kubernetes` client and the in-cluster config (`load_incluster_config`).
   The shape of `ensure_worker` and `scale_worker` stays the same.

6. **MongoDB**

   Use MongoDB Atlas with VPC peering / Private Service Connect, or migrate
   the data layer to Firestore. Store the connection string in Secret Manager
   and inject it via a CSI driver:
   ```yaml
   env:
     - name: PYFUSE_CLOUD_MONGODB_URI
       valueFrom:
         secretKeyRef:
           name: mongo-uri
           key: uri
   ```

7. **Apply the cluster manifests**

   Reuse the manifests in [cloud_poc/kubernetes/](../kubernetes/) as a
   starting point. Edit `api.yaml` to:
   - point at the Artifact Registry image
   - set `PYFUSE_CLOUD_PUBLIC_BROKER_URL=https://api.pyfuse.example/api/v1/broker`
   - set `PYFUSE_CLOUD_INTERNAL_BROKER_URL` to the in-cluster Service DNS
     (`http://pyfuse-cloud-api.pyfuse-cloud.svc.cluster.local:8000/api/v1/broker`)
   - mount the Mongo URI from Secret Manager
   - assign the Workload-Identity-bound ServiceAccount

   Apply:
   ```bash
   kubectl apply -f cloud_poc/kubernetes/namespace.yaml
   kubectl apply -f cloud_poc/kubernetes/api.yaml
   kubectl apply -f cloud_poc/kubernetes/frontend.yaml
   kubectl apply -f cloud_poc/kubernetes/network-policy.yaml
   ```
8. **HTTPS load balancer**

   Use a `Gateway` (Gateway API) or an `Ingress` with a Google-managed
   certificate:
   ```yaml
   apiVersion: networking.gke.io/v1
   kind: ManagedCertificate
   metadata:
     name: pyfuse-cert
     namespace: pyfuse-cloud
   spec:
     domains: [api.pyfuse.example, app.pyfuse.example]
   ```
   Front the api Service and the frontend Service behind path-based routing.

9. **Worker hardening**

   In the orchestrator-rendered worker manifest:
   - `runtimeClassName: gvisor` for syscall isolation
   - explicit CPU/memory limits sized to the user's plan
   - `automountServiceAccountToken: false` (already set)
   - `seccompProfile: RuntimeDefault` (already set)
   - drop all capabilities (already set)
   - mount an `emptyDir` at `/home/worker/.cache/pip` for pip cache
   - consider a per-user `PersistentVolumeClaim` to keep the venv warm
     across pod restarts

10. **Observability**
    - Cloud Logging picks up stdout/stderr automatically; switch the backend
      to JSON logs (`uvicorn --log-config logconfig.json`) for structured
      fields.
    - Add a `/metrics` endpoint with Prometheus client and a
      `PodMonitoring` CR to scrape it via Google Managed Prometheus.
    - Alert on: API error rate, reaper failures, p95 cold-start latency,
      worker OOMKilled count.

11. **Continuous deployment**

    A typical pipeline (Cloud Build or GitHub Actions):
    1. lint + `mypy pyfuse` + `pytest`
    2. build & push `cloud-worker` and `cloud-api` images, tagged by commit
    3. `kubectl set image …` against staging, run smoke tests
       (`cloud_poc/smoke_test.py` against the staging URL)
    4. promote to production on success

### Cost levers

- Set `PYFUSE_CLOUD_IDLE_SECONDS` aggressively low (e.g. 60s) for cheap users.
- Use Spot nodes for worker pools; keep the control plane on standard nodes.
- Cache the worker image with `imagePullPolicy: IfNotPresent` and node-image
  preloading to keep cold-start under a few seconds.
- For users with sustained workloads, raise `replicas` and let the HPA scale
  on queue depth (`pyfuse_cloud_queued_tasks` metric).

### Migration checklist (PoC → GCP)

- [ ] Replace `subprocess` + `kubectl` with the Python kubernetes client
- [ ] Add `--workers` to uvicorn and remove `--reload`
- [ ] Switch logger to JSON output
- [ ] Move secrets to Secret Manager
- [ ] Hash / rotate API keys; consider short-lived JWTs
- [ ] Add per-user resource quotas (`ResourceQuota`, `LimitRange`)
- [ ] Add `runtimeClassName: gvisor` to worker pods
- [ ] Enable Cloud Armor on the public LB
- [ ] Restore `runAsNonRoot` + `readOnlyRootFilesystem` on worker pods,
      pre-baking deps into the image or using a writable `emptyDir` for the
      venv path
