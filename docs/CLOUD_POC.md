# Cloud proof-of-concept

This proof-of-concept adds a hosted-style path for `pyfuse` where clients point at an `http://` or `https://` broker URL instead of running Redis, RabbitMQ, or the local TCP broker themselves.

## Plan

1. Add an HTTP(S) backend to the core package so clients and workers can talk to a remotely hosted broker API with no pyfuse API changes.
2. Use API keys as the tenant identity so each request can be attributed to one user and routed to that user's dedicated worker deployment.
3. Keep transport traffic JSON over HTTPS, preserve existing task signing for worker-side authenticity checks, and isolate execution in hardened Kubernetes pods.
4. Provide a local development stack with MongoDB for users/tasks, a FastAPI control plane and broker, a React dashboard, and Kubernetes manifests for dedicated worker pods.

## Protocol

The hosted broker protocol is intentionally small and maps directly onto the existing `Backend` ABC:

- `POST /api/v1/broker/tasks` — submit task JSON
- `POST /api/v1/broker/tasks/claim` — worker long-poll for the next queued task
- `POST /api/v1/broker/tasks/{task_id}/result` — publish a result envelope
- `GET /api/v1/broker/tasks/{task_id}/result` — long-poll for a result
- `POST`/`GET .../heartbeat`, `.../progress`, `.../cancel`
- `POST`/`GET /api/v1/broker/schedules/{schedule_id}/cancel`
- `GET /api/v1/broker/throttle/check` and `POST /api/v1/broker/throttle/record`

`pyfuse.connect("https://host/api/v1/broker?api_key=...")` now routes through `HttpBackend`, which moves `api_key` into the `X-Pyfuse-API-Key` header for every request.

## Identity and usage tracking

- User registration lives in `cloud_poc/backend/app/main.py`.
- Every user gets a generated API key.
- Tasks, results, heartbeats, progress updates, and throttle windows are stored in MongoDB and keyed by `user_id`.
- The React dashboard reads `/api/v1/users/me`, `/api/v1/usage/summary`, and `/api/v1/usage/tasks` to show broker URL, aggregate counts, and recent executions.

## Security model

This is still a development proof-of-concept, but it establishes the main guardrails:

- **Transport security**: use HTTPS in hosted environments; the backend already supports `https://` URLs.
- **Tenant identity**: API keys are carried in a header, never embedded in task JSON.
- **Task authenticity**: the existing HMAC signing flow still works, so hosted workers can require signed tasks.
- **Isolation**: workers run in dedicated Kubernetes deployments with non-root users, dropped Linux capabilities, `RuntimeDefault` seccomp, read-only root filesystem, no service account token, and restrictive network policy.
- **Future hardening path**: replace plain API keys with short-lived tokens, move worker orchestration behind the Kubernetes API instead of `kubectl`, and use stronger sandboxes such as gVisor, Kata, or microVM-based workers.

## Local development stack

### Components

- **MongoDB**: user, task, schedule, and throttle state
- **FastAPI**: registration, usage API, broker API, worker scaler/reaper
- **React + Vite**: simple dashboard for registration and monitoring
- **Kubernetes**: dedicated per-user worker deployments that scale up on demand and are scaled back to zero after inactivity

### Files

- `cloud_poc/backend/app/` — FastAPI app and worker orchestration helpers
- `cloud_poc/backend/requirements.txt` — backend dependencies
- `cloud_poc/frontend/` — React dashboard
- `cloud_poc/kubernetes/` — namespace, MongoDB, API, frontend, and network policy manifests
- `cloud_poc/worker/Dockerfile` — worker image for the dedicated execution pods

### Suggested local flow

```bash
# 1) Install backend deps
python -m pip install -r cloud_poc/backend/requirements.txt

# 2) Run the API locally
uvicorn cloud_poc.backend.app.main:app --reload

# 3) Run the dashboard locally
cd cloud_poc/frontend
npm install
npm run dev

# 4) Apply the local Kubernetes assets
kubectl apply -f cloud_poc/kubernetes/namespace.yaml
kubectl apply -f cloud_poc/kubernetes/mongodb.yaml
kubectl apply -f cloud_poc/kubernetes/api.yaml
kubectl apply -f cloud_poc/kubernetes/frontend.yaml
kubectl apply -f cloud_poc/kubernetes/network-policy.yaml
```

### Example client usage

After registering in the dashboard, copy the returned broker URL and use it directly:

```python
import pyfuse
from pyfuse import trace

pyfuse.connect("http://localhost:8000/api/v1/broker?api_key=<your key>")

@trace
def hello(name: str) -> str:
    return f"hello {name}"
```

The client API stays the same; only the backend URL changes.
