# Quick start

Operational reference for running the local pyfuse cloud proof-of-concept. All
commands assume the repository root as the working directory.

## Prerequisites

- Python 3.13 with the project virtualenv (`pyfuse-vDS75-62-py3.13`) activated
- MongoDB on `localhost:27017`
- Docker Desktop with Kubernetes enabled (context `docker-desktop`)
- Node.js 20+ for the frontend

## One-time setup

```bash
# 1. Install backend deps
python -m pip install -r cloud_poc/backend/requirements.txt

# 2. Install frontend deps
cd cloud_poc/frontend && npm install && cd -

# 3. Build the worker image and load it into the cluster
docker build -t pyfuse-cloud-worker:dev -f cloud_poc/worker/Dockerfile .
docker save pyfuse-cloud-worker:dev | docker exec -i desktop-control-plane ctr -n=k8s.io images import -

# 4. Create the namespace (the orchestrator does this on demand too)
kubectl apply -f cloud_poc/kubernetes/namespace.yaml
```

## Start the services

Open three terminals.

```bash
# Terminal 1 — control plane (FastAPI)
source ~/.poetry/cache/virtualenvs/pyfuse-vDS75-62-py3.13/bin/activate
uvicorn cloud_poc.backend.app.main:app --reload
```

```bash
# Terminal 2 — frontend
cd cloud_poc/frontend
npm run dev
```

```bash
# Terminal 3 — interact with the broker
source ~/.poetry/cache/virtualenvs/pyfuse-vDS75-62-py3.13/bin/activate
```

The dashboard is at http://localhost:5173. Register a user there and copy the
broker URL it returns.

## Stop / restart

```bash
# Stop the FastAPI server: Ctrl-C in terminal 1
# Restart: re-run the uvicorn command above
# uvicorn --reload picks up code changes automatically

# Restart the frontend: Ctrl-C in terminal 2 then `npm run dev`

# Wipe MongoDB state (users, tasks, throttles, schedules)
python -c "
from pymongo import MongoClient
db = MongoClient('mongodb://localhost:27017')['pyfuse_cloud']
for c in ('users', 'tasks', 'throttles', 'schedules'):
    db[c].delete_many({})
print('cleared')
"

# Tear down all worker deployments
kubectl delete deploy --all -n pyfuse-cloud
```

## Configuration

Backend reads its configuration from environment variables (defaults shown):

| Variable                              | Default                                                | Purpose                                  |
|---------------------------------------|--------------------------------------------------------|------------------------------------------|
| `PYFUSE_CLOUD_LOG_LEVEL`              | `INFO`                                                 | Backend log level (`DEBUG`, `INFO`, …)   |
| `PYFUSE_CLOUD_MONGODB_URI`            | `mongodb://localhost:27017`                            | MongoDB connection string                |
| `PYFUSE_CLOUD_MONGODB_DB`             | `pyfuse_cloud`                                         | Database name                            |
| `PYFUSE_CLOUD_KUBE_CONTEXT`           | `docker-desktop`                                       | Local kube-context (validated)           |
| `PYFUSE_CLOUD_NAMESPACE`              | `pyfuse-cloud`                                         | Worker namespace                         |
| `PYFUSE_CLOUD_PUBLIC_BROKER_URL`      | `http://localhost:8000/api/v1/broker`                  | URL given to clients                     |
| `PYFUSE_CLOUD_INTERNAL_BROKER_URL`    | `http://host.docker.internal:8000/api/v1/broker`       | URL injected into worker pods            |
| `PYFUSE_CLOUD_WORKER_IMAGE`           | `pyfuse-cloud-worker:dev`                              | Worker container image                   |
| `PYFUSE_CLOUD_IDLE_SECONDS`           | `300`                                                  | Inactivity before scaling worker to 0    |
| `PYFUSE_CLOUD_TASK_POLL_INTERVAL`     | `1.0`                                                  | Task / reaper poll interval (seconds)    |
| `PYFUSE_CLOUD_DISABLE_KUBECTL`        | `0`                                                    | Set to `1` to skip Kubernetes calls      |

For a faster reaper while testing:

```bash
PYFUSE_CLOUD_IDLE_SECONDS=15 uvicorn cloud_poc.backend.app.main:app --reload
```

## Smoke tests

```bash
# 1. Health
curl -s http://localhost:8000/api/v1/health

# 2. Register and submit a task end-to-end
python cloud_poc/smoke_test.py "alice$(date +%s)@example.com"

# 3. Re-submit using an existing API key (verifies cold start from replicas=0)
python cloud_poc/wake_test.py <api_key>

# 4. Run all examples against the cloud broker
BROKER_URL="http://localhost:8000/api/v1/broker?api_key=<key>" \
    bash cloud_poc/run_all_examples.sh
```

## Logs

### Control plane (FastAPI)

Log lines are tagged with `cloud_poc.backend.app.main` or `…orchestrator`.
Notable events:

- `control-plane starting / ready / shutting down`
- `register user=<email> id=<user_id>`
- `submit user=<id> task=<id> fn=<name> bytes=<n>`
- `claim user=<id> task=<id> fn=<name>`
- `result user=<id> task=<id> status=<completed|error|cancelled|throttled>`
- `cancel user=<id> task=<id>`
- `reaper found <n> idle user(s)` / `reaper scaled user=<id> to 0`
- `orchestrator init`, `ensure_worker(...)`, `scale_worker(...) -> replicas=N`
- `kubectl <args> failed: <stderr>` on orchestration errors

Increase verbosity:

```bash
PYFUSE_CLOUD_LOG_LEVEL=DEBUG uvicorn cloud_poc.backend.app.main:app --reload
```

### Worker pods

```bash
# Live logs of every worker (per-user pods)
kubectl logs -n pyfuse-cloud -l app=pyfuse-cloud-worker -f --max-log-requests=20

# A specific user's worker
kubectl logs -n pyfuse-cloud deploy/pyfuse-worker-<first 12 chars of user id> -f

# All recent worker output (no follow)
kubectl logs -n pyfuse-cloud -l app=pyfuse-cloud-worker --tail=200
```

The worker emits one line per task (`✓ <fn>  <ms>ms  <hash>  <build|cached>`)
plus signing/throttle/cancel markers.

### Kubernetes events and pod state

```bash
# Pods
kubectl get pod -n pyfuse-cloud -o wide

# Recent events
kubectl get events -n pyfuse-cloud --sort-by=.lastTimestamp | tail -20

# Why a pod isn't running
kubectl describe pod -n pyfuse-cloud <pod-name>

# Watch deployments scale up/down in real time
kubectl get deploy -n pyfuse-cloud -w
```

### Common issues

- **`ImagePullBackOff` on worker pods** — image not loaded into the cluster.
  Re-run the `docker save | docker exec ... ctr images import` command.
- **`connection refused` to `127.0.0.1:55…`** — Docker Desktop's Kubernetes
  isn't running. Enable it from Docker Desktop → Settings → Kubernetes.
- **Worker pod stays at 0 replicas** — the reaper scaled it down. Submit a
  task and the orchestrator will scale it back to 1.
- **Tasks never claimed** — pod can't reach the host. Verify
  `host.docker.internal` resolves inside the pod:
  ```bash
  kubectl exec -n pyfuse-cloud <pod> -- python -c "import socket; print(socket.gethostbyname('host.docker.internal'))"
  ```

## Quick triage commands

```bash
# Show every cloud-poc moving part in one screen
clear; \
echo '=== api ==='     ; curl -s http://localhost:8000/api/v1/health; \
echo; \
echo '=== mongo ==='   ; nc -z localhost 27017 && echo up || echo down; \
echo '=== k8s ns ==='  ; kubectl get all -n pyfuse-cloud
```
