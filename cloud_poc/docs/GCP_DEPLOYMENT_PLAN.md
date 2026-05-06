# GCP deployment plan

A concrete, ordered plan for taking the `cloud_poc` from a single-machine
Docker Desktop deployment to a multi-tenant production deployment on Google
Cloud Platform. Each phase has a goal, the commands to run, and the
acceptance criterion that lets you move on.

> Conventions used below
> - `${PROJECT_ID}` — your GCP project id, e.g. `pyfuse-prod`
> - `${REGION}` — preferred GCP region, e.g. `us-central1`
> - `${DOMAIN}` — your registered domain, e.g. `pyfuse.example`
> - `${REPO}` — Artifact Registry repository name, default `pyfuse`

---

## Phase 0 — Publish `pyfuse` to PyPI

The hosted control plane and worker images both `pip install pyfuse`. PyPI is
the source of truth.

### 0.1 Final pre-publish checks
```bash
# All tests pass against every supported backend
pytest -q
mypy pyfuse

# Build a wheel and sdist
python -m pip install --upgrade build twine
python -m build
twine check dist/*
```

Acceptance: `dist/pyfuse-X.Y.Z-py3-none-any.whl` and
`pyfuse-X.Y.Z.tar.gz` produced; `twine check` reports `PASSED`.

### 0.2 Reserve the project name on TestPyPI first
```bash
twine upload --repository testpypi dist/*
python -m pip install --index-url https://test.pypi.org/simple/ \
                     --extra-index-url https://pypi.org/simple/ \
                     pyfuse
python -c "import pyfuse; print(pyfuse.__version__)"
```

### 0.3 Publish to PyPI
1. Create a [PyPI Trusted Publisher](https://docs.pypi.org/trusted-publishers/)
   binding for `codeSamuraii/pyfuse` so GitHub Actions can publish without an
   API token.
2. Add `.github/workflows/publish.yml` that runs on every tag matching
   `v*` and:
   - runs the test matrix
   - builds with `python -m build`
   - uses `pypa/gh-action-pypi-publish` with `id-token: write` permission
3. Tag and push:
   ```bash
   git tag v0.5.0 -m "First public release"
   git push origin v0.5.0
   ```

Acceptance: `pip install pyfuse==0.5.0` works from a clean machine.

### 0.4 Pin the cloud images to a published version
Both Dockerfiles currently `pip install .` from the source tree. After
publishing, switch to a pinned PyPI install:

```dockerfile
# cloud_poc/worker/Dockerfile  (and cloud_poc/backend/Dockerfile, when added)
FROM python:3.13-slim
ARG PYFUSE_VERSION=0.5.0
RUN pip install --no-cache-dir "pyfuse==${PYFUSE_VERSION}"
```

This makes the worker image reproducible and decouples its build from a
checkout of the repository.

---

## Phase 1 — GCP project bootstrap

### 1.1 Create or pick a project
```bash
gcloud projects create ${PROJECT_ID} --set-as-default
gcloud config set project ${PROJECT_ID}
gcloud config set compute/region ${REGION}
gcloud beta billing projects link ${PROJECT_ID} --billing-account=<ID>
```

### 1.2 Enable APIs
```bash
gcloud services enable \
    container.googleapis.com \
    artifactregistry.googleapis.com \
    cloudbuild.googleapis.com \
    secretmanager.googleapis.com \
    certificatemanager.googleapis.com \
    compute.googleapis.com \
    iam.googleapis.com \
    iamcredentials.googleapis.com \
    monitoring.googleapis.com \
    logging.googleapis.com \
    cloudresourcemanager.googleapis.com
```

### 1.3 Identity baseline
- Create a Google Cloud Identity / Workspace group `pyfuse-admins@…` and
  grant it `roles/owner` on the project.
- Disable default service accounts (`-compute@…`).

Acceptance: `gcloud projects describe ${PROJECT_ID}` returns the project,
APIs are listed in `gcloud services list --enabled`.

---

## Phase 2 — Artifact Registry

### 2.1 Create a Docker repository
```bash
gcloud artifacts repositories create ${REPO} \
    --repository-format=docker \
    --location=${REGION} \
    --description="pyfuse cloud images"

gcloud auth configure-docker ${REGION}-docker.pkg.dev
```

### 2.2 Image names
- `${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/cloud-api:${TAG}`
- `${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/cloud-worker:${TAG}`
- `${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/cloud-frontend:${TAG}`

`${TAG}` is the short git SHA in CI; tag `latest` on `main` only for
human-friendly local pulls.

### 2.3 Build and push (manual, first time)
```bash
TAG=$(git rev-parse --short HEAD)
BASE=${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}

docker buildx build --platform linux/amd64 \
    -t ${BASE}/cloud-worker:${TAG} \
    -f cloud_poc/worker/Dockerfile . --push

docker buildx build --platform linux/amd64 \
    -t ${BASE}/cloud-api:${TAG} \
    -f cloud_poc/backend/Dockerfile . --push

docker buildx build --platform linux/amd64 \
    -t ${BASE}/cloud-frontend:${TAG} \
    -f cloud_poc/frontend/Dockerfile cloud_poc/frontend --push
```

> The frontend Dockerfile currently doesn't exist as a production image; add
> a multi-stage build (`node` → `nginx:alpine`) before running the command
> above. The nginx config should serve `dist/` and proxy `/api/*` to the API
> Service via `proxy_pass`.

Acceptance: all three images visible via
`gcloud artifacts docker images list ${BASE}`.

---

## Phase 3 — Data plane (MongoDB)

Two viable options — pick **one**, then move on:

### Option A (recommended): MongoDB Atlas
1. Create an Atlas project; deploy an M10 dedicated cluster in the same GCP
   region.
2. Use Private Service Connect (PSC) to expose the cluster to the GKE VPC.
3. Create a database user `pyfuse-api` with `readWrite` on `pyfuse_cloud`.
4. Store the SRV connection string in Secret Manager:
   ```bash
   gcloud secrets create pyfuse-mongo-uri --replication-policy=automatic
   echo -n "mongodb+srv://pyfuse-api:<pwd>@…/pyfuse_cloud" | \
       gcloud secrets versions add pyfuse-mongo-uri --data-file=-
   ```

### Option B: rewrite the data layer to Firestore
Skip Mongo entirely; use `google-cloud-firestore` in the API. Lower ops cost,
but requires non-trivial code changes (collection-based, no transactions
across collections). Out of scope for the first cut.

Acceptance (option A):
`gcloud secrets versions access latest --secret pyfuse-mongo-uri` returns
the URI, and a one-shot pod in GKE can connect.

---

## Phase 4 — GKE cluster

### 4.1 Cluster
GKE Autopilot eliminates node ops and matches the per-user-pod model well.

```bash
gcloud container clusters create-auto pyfuse \
    --location=${REGION} \
    --release-channel=regular \
    --enable-private-nodes \
    --enable-private-endpoint=false \
    --master-ipv4-cidr=172.16.0.32/28 \
    --network=default --subnetwork=default

gcloud container clusters get-credentials pyfuse --location=${REGION}
```

### 4.2 Namespace and quotas
```bash
kubectl apply -f - <<EOF
apiVersion: v1
kind: Namespace
metadata:
  name: pyfuse-cloud
---
apiVersion: v1
kind: ResourceQuota
metadata:
  name: workers
  namespace: pyfuse-cloud
spec:
  hard:
    pods: "200"
    requests.cpu: "50"
    requests.memory: "100Gi"
    limits.cpu: "200"
    limits.memory: "400Gi"
EOF
```

### 4.3 Workload Identity binding for the orchestrator
The control plane needs to manage Deployments in `pyfuse-cloud` only.

```bash
GSA=pyfuse-cloud-api@${PROJECT_ID}.iam.gserviceaccount.com
gcloud iam service-accounts create pyfuse-cloud-api

# Kubernetes ServiceAccount
kubectl -n pyfuse-cloud create sa api

# Bind GSA <-> KSA
gcloud iam service-accounts add-iam-policy-binding ${GSA} \
    --role roles/iam.workloadIdentityUser \
    --member "serviceAccount:${PROJECT_ID}.svc.id.goog[pyfuse-cloud/api]"
kubectl -n pyfuse-cloud annotate sa api \
    iam.gke.io/gcp-service-account=${GSA}

# In-cluster RBAC for the orchestrator
kubectl apply -f - <<EOF
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  namespace: pyfuse-cloud
  name: worker-admin
rules:
  - apiGroups: ["apps"]
    resources: ["deployments", "deployments/scale"]
    verbs: ["get", "list", "create", "patch", "update", "delete"]
  - apiGroups: [""]
    resources: ["pods"]
    verbs: ["get", "list"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  namespace: pyfuse-cloud
  name: api-can-admin-workers
subjects:
  - kind: ServiceAccount
    namespace: pyfuse-cloud
    name: api
roleRef:
  kind: Role
  name: worker-admin
  apiGroup: rbac.authorization.k8s.io
EOF
```

### 4.4 Replace the kubectl orchestrator with the Python client
The current `WorkerOrchestrator` shells out to `kubectl`. In production:

- `pip install kubernetes` in the API image.
- Replace `subprocess.run([kubectl, ...])` with
  `kubernetes.config.load_incluster_config()` and
  `kubernetes.client.AppsV1Api`.
- Map `_run_kubectl("apply", "-f", "-", input_text=manifest)` →
  `apps_v1.create_namespaced_deployment` (with try/except on `409
  AlreadyExists`).
- Map `_run_kubectl("scale", ...)` → `apps_v1.patch_namespaced_deployment_scale`.
- Keep the public surface (`ensure_worker`, `scale_worker`,
  `_deployment_exists`, `_scale_cache`) unchanged so `main.py` is untouched.

Acceptance: `kubectl auth can-i create deployments -n pyfuse-cloud
--as=system:serviceaccount:pyfuse-cloud:api` returns `yes`; the orchestrator
unit tests pass against a kind cluster using the new client.

---

## Phase 5 — Control plane Deployment

### 5.1 Manifest
```yaml
# cloud_poc/kubernetes/api.gcp.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: pyfuse-cloud-api
  namespace: pyfuse-cloud
spec:
  replicas: 2
  selector: { matchLabels: { app: pyfuse-cloud-api } }
  template:
    metadata: { labels: { app: pyfuse-cloud-api } }
    spec:
      serviceAccountName: api
      containers:
        - name: api
          image: REGION-docker.pkg.dev/PROJECT/REPO/cloud-api:TAG
          ports: [{ containerPort: 8000 }]
          env:
            - name: PYFUSE_CLOUD_PUBLIC_BROKER_URL
              value: https://api.${DOMAIN}/api/v1/broker
            - name: PYFUSE_CLOUD_INTERNAL_BROKER_URL
              value: http://pyfuse-cloud-api.pyfuse-cloud.svc.cluster.local:8000/api/v1/broker
            - name: PYFUSE_CLOUD_NAMESPACE
              value: pyfuse-cloud
            - name: PYFUSE_CLOUD_WORKER_IMAGE
              value: REGION-docker.pkg.dev/PROJECT/REPO/cloud-worker:TAG
            - name: PYFUSE_CLOUD_LOG_LEVEL
              value: INFO
            - name: PYFUSE_CLOUD_MONGODB_URI
              valueFrom: { secretKeyRef: { name: mongo-uri, key: uri } }
          readinessProbe: { httpGet: { path: /api/v1/health, port: 8000 } }
          livenessProbe:  { httpGet: { path: /api/v1/health, port: 8000 } }
          resources:
            requests: { cpu: "200m", memory: "256Mi" }
            limits:   { cpu: "1",    memory: "512Mi" }
---
apiVersion: v1
kind: Service
metadata:
  name: pyfuse-cloud-api
  namespace: pyfuse-cloud
spec:
  selector: { app: pyfuse-cloud-api }
  ports: [{ port: 8000, targetPort: 8000 }]
```

### 5.2 Switch the orchestrator off the `kubectl` binary path
Remove the `_kubectl_command` "local context" guard in production builds —
or keep it but extend the allowlist with `gke_*` contexts. The cleanest path
is the Python-client refactor in 4.4.

### 5.3 Run uvicorn properly
The `Dockerfile` for the API should use:

```dockerfile
CMD ["uvicorn", "cloud_poc.backend.app.main:app",
     "--host", "0.0.0.0", "--port", "8000",
     "--workers", "4", "--proxy-headers", "--forwarded-allow-ips=*"]
```

Acceptance: `kubectl -n pyfuse-cloud rollout status deploy/pyfuse-cloud-api`
reports ready, and `kubectl -n pyfuse-cloud port-forward svc/pyfuse-cloud-api
8000:8000` allows `curl localhost:8000/api/v1/health`.

---

## Phase 6 — Worker pods in production

### 6.1 Image arguments
The orchestrator currently embeds the API key directly in the worker pod's
`args`. In GCP, store it instead as a per-deployment environment variable
sourced from a Kubernetes Secret named after the user, or pass it through a
short-lived JWT minted by the control plane.

Minimum change for v1: keep the api_key in args (still per-pod, never on
disk), but:
- Set `imagePullSecrets` is *not* needed — Workload Identity covers Artifact
  Registry pulls automatically.
- Pin `image:` to a commit-tagged worker image, never `:latest`.

### 6.2 Sandbox upgrade (optional but recommended)
Add a `RuntimeClass` named `gvisor` and set `runtimeClassName: gvisor` on
the worker pod template. GKE Autopilot supports gVisor out of the box.

### 6.3 Worker resource model
Per pod (default plan):
- requests: 100m CPU, 256 MiB
- limits:   1 CPU, 1 GiB

Higher-tier plans get bumped limits — gate on a `plan` field on the user
document.

### 6.4 Network policy
Restrict worker pods to only egress to:
- the in-cluster API Service
- `pkg.dev` (for `pip install`) — or pre-bake all known third-party packages
  in the base image and disable egress entirely.

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: { name: workers-egress, namespace: pyfuse-cloud }
spec:
  podSelector: { matchLabels: { app: pyfuse-cloud-worker } }
  policyTypes: [Egress]
  egress:
    - to:
        - podSelector: { matchLabels: { app: pyfuse-cloud-api } }
      ports: [{ port: 8000, protocol: TCP }]
    - to: [{ ipBlock: { cidr: 0.0.0.0/0 } }]
      ports: [{ port: 443, protocol: TCP }]   # PyPI / Artifact Registry
```

Acceptance: a registered user submits a task via the production URL and the
orchestrator scales a pod up; `kubectl logs` shows the task line.

---

## Phase 7 — Frontend hosting

Two acceptable patterns:

### A. Serve from GKE behind the same Ingress
- Multi-stage `cloud_poc/frontend/Dockerfile` builds with `node:20-alpine`
  and serves with `nginx:alpine`.
- Inject `VITE_API_BASE=https://api.${DOMAIN}` at build time.
- One Deployment + Service in `pyfuse-cloud`.

### B. Cloud Storage + Cloud CDN
- `npm run build` in CI, sync `dist/` to a GCS bucket fronted by Cloud CDN.
- Lower latency, no pods to operate.
- Pick this if you want clear separation between static and dynamic.

Either way, configure CORS on the API to allow `https://app.${DOMAIN}`.

Acceptance: visiting `https://app.${DOMAIN}` shows the login screen, and
sign-in works against the production API.

---

## Phase 8 — Public ingress, TLS, DNS

### 8.1 Reserve a static IP
```bash
gcloud compute addresses create pyfuse-ingress --global
```

### 8.2 Managed certificate
```yaml
apiVersion: networking.gke.io/v1
kind: ManagedCertificate
metadata: { name: pyfuse-cert, namespace: pyfuse-cloud }
spec: { domains: [api.${DOMAIN}, app.${DOMAIN}] }
```

### 8.3 Ingress (or Gateway API)
```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: pyfuse
  namespace: pyfuse-cloud
  annotations:
    kubernetes.io/ingress.global-static-ip-name: pyfuse-ingress
    networking.gke.io/managed-certificates: pyfuse-cert
    kubernetes.io/ingress.class: gce
spec:
  rules:
    - host: api.${DOMAIN}
      http: { paths: [{ path: /*, pathType: ImplementationSpecific,
                        backend: { service: { name: pyfuse-cloud-api, port: { number: 8000 }}}}]}
    - host: app.${DOMAIN}
      http: { paths: [{ path: /*, pathType: ImplementationSpecific,
                        backend: { service: { name: pyfuse-cloud-frontend, port: { number: 80 }}}}]}
```

### 8.4 Cloud DNS
Create A records for `api.${DOMAIN}` and `app.${DOMAIN}` pointing to the
ingress IP.

Acceptance: `curl -v https://api.${DOMAIN}/api/v1/health` returns 200 with a
valid certificate.

---

## Phase 9 — Observability

### 9.1 Logs
GKE pipes stdout/stderr to Cloud Logging automatically. Switch the API to
JSON log lines for queryable fields:

```python
# cloud_poc/backend/app/main.py
import logging, json, sys
class _JsonFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps({
            "level": record.levelname,
            "name": record.name,
            "msg": record.getMessage(),
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
        })
handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(_JsonFormatter())
logging.basicConfig(level=os.environ.get("PYFUSE_CLOUD_LOG_LEVEL", "INFO"),
                    handlers=[handler], force=True)
```

Saved log queries to bookmark in Cloud Logging:
- `resource.labels.container_name="api" AND jsonPayload.msg=~"submit "`
- `resource.labels.container_name="worker"`

### 9.2 Metrics
Add a `/metrics` endpoint with `prometheus-client`. Expose:
- `pyfuse_cloud_tasks_total{status=…}` (counter)
- `pyfuse_cloud_task_duration_seconds` (histogram, from finished_at - started_at)
- `pyfuse_cloud_active_workers` (gauge)
- `pyfuse_cloud_kubectl_calls_total{verb=…}` (counter)

Enable Google Managed Prometheus on the cluster, add a `PodMonitoring` for
the API.

### 9.3 Alerts (Cloud Monitoring)
- API 5xx rate > 1% for 5 min
- Reaper failures (log-based metric) > 0 in 5 min
- p95 cold-start latency > 30 s
- Worker OOMKilled count > 0

---

## Phase 10 — Security hardening

Each item below is independently shippable; ship them in order.

1. **Hash API keys at rest.** Store `sha256(api_key)` in Mongo, return the
   raw key only at registration. Authentication compares hashes.
2. **API key rotation.** Add `POST /api/v1/users/me/keys/rotate` that
   creates a new key, marks the old one as `revoked_at`, and updates the
   per-user worker deployment's `args` (triggers one controlled rollout).
3. **Rate limiting.** Add `slowapi` or a Redis-backed rate limiter at the
   API edge: 60 req/s per API key, 5 register/login per IP per minute.
4. **Cloud Armor.** Attach a security policy to the ingress: rate-limit
   per-IP, block known bad ASNs, OWASP rule set in preview-then-enforce.
5. **gVisor sandbox** for worker pods (Phase 6.2).
6. **Secrets via External Secrets Operator** so Mongo URI and signing keys
   live only in Secret Manager.
7. **Workload Identity Federation for CI** so GitHub Actions never holds a
   service-account key.

---

## Phase 11 — CI/CD

Recommended pipeline (Cloud Build or GitHub Actions):

1. **PR**: lint → `mypy pyfuse` → `pytest` → build all three images with
   `--load` only (no push).
2. **Push to `main`**: build & push images tagged `${SHA}`; deploy to a
   `staging` namespace in the same GKE cluster; run
   `cloud_poc/smoke_test.py` against the staging URL.
3. **Tag `v*`** (PyPI release): publishes to PyPI (Phase 0); rebuilds images
   pinned to the new PyPI version; deploys to `pyfuse-cloud` (production).

Promotion is a `kubectl set image deploy/pyfuse-cloud-api api=…:${SHA}`
followed by `kubectl rollout status`.

---

## Phase 12 — Cost & capacity tuning

After two weeks in production, revisit:

- `PYFUSE_CLOUD_IDLE_SECONDS` — lower for the free tier (e.g. 60 s) so cold
  workers don't accumulate.
- Per-tier pod CPU/memory requests; consider Spot pods for free-tier
  workers.
- `--workers N` count on the API (4 is a good start; add HPA on CPU).
- Pre-baked dependency images for popular workloads
  (`pyfuse-cloud-worker-pandas:…`) chosen via a per-user `image_variant`
  field.
- Cache the worker venv on a per-user PersistentVolumeClaim for warm pip
  installs across pod restarts.

---

## Quick checklist

A condensed view of everything above, in order:

- [ ] Phase 0: pyfuse on PyPI, images pinned to a published version
- [ ] Phase 1: GCP project + APIs
- [ ] Phase 2: Artifact Registry + first image pushes
- [ ] Phase 3: MongoDB Atlas + Secret Manager
- [ ] Phase 4: GKE Autopilot + Workload Identity for orchestrator
- [ ] Phase 4.4: replace `kubectl` shell-out with Python kubernetes client
- [ ] Phase 5: API Deployment + Service running in cluster
- [ ] Phase 6: worker pods provisioned per user
- [ ] Phase 7: frontend served (GKE or GCS+CDN)
- [ ] Phase 8: HTTPS ingress + DNS records
- [ ] Phase 9: structured logs, `/metrics`, alerts
- [ ] Phase 10: API-key hashing, rate limiting, Cloud Armor, gVisor
- [ ] Phase 11: CI/CD with PyPI tag → image build → cluster deploy
- [ ] Phase 12: cost tuning
