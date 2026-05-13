# Signing & Pairing

Cryptographically sign serialized tasks so that workers only execute code from trusted clients. Two key distribution methods are available:

- **Token-based** (recommended for automation): Generate a shared token offline, distribute via environment variables or secrets management. No real-time coordination needed.
- **PIN-based pairing** (recommended for interactive use): A client and worker exchange keys via a 6-digit PIN displayed on one side and entered on the other.

Both methods use the same underlying HMAC-SHA256 signing — they differ only in how the shared secret is established.

## Overview

By default, away workers execute any task they receive from the backend. When signing is enabled:

1. A client and worker share a cryptographic key (via **token** or **pairing**).
2. The client **signs** every task with HMAC-SHA256 before submitting it.
3. The worker **verifies** the signature before executing the task.

Tasks with missing or invalid signatures are rejected.

## Quick start — Token (recommended for CI/CD)

### 1. Generate a token

```bash
away token generate
```

```
  Token generated and saved to ~/.away/token

  Token: a1b2c3d4e5f6...

  Set this on both client and worker:
    export AWAY_SIGNING_TOKEN=a1b2c3d4e5f6...
```

### 2. Distribute the token

Copy the token to both the client and worker machines. The recommended method is an environment variable:

```bash
# On both client and worker
export AWAY_SIGNING_TOKEN=a1b2c3d4e5f6...
```

For CI/CD, store the token as a secret in your CI provider (GitHub Actions secrets, GitLab CI variables, etc.) and inject it as `AWAY_SIGNING_TOKEN`.

Alternatively, copy the `~/.away/token` file to both machines.

### 3. Start the worker with signing

```bash
away worker --backend redis://localhost:6379 --require-signing
```

### 4. Run tasks (no changes needed)

```bash
python examples/remote_execution.py
```

The client automatically loads the token and signs tasks before submission. No code changes are needed.

## Quick start — PIN-based pairing

### 1. Start a worker with pairing

On the **worker** machine:

```bash
away worker --backend redis://localhost:6379 --pair
```

This generates a 6-digit PIN and waits for a client:

```
  Pairing PIN:  482913

  Enter this PIN on the client with:
    away pair --backend redis://localhost:6379

  Waiting for client...
```

Once paired, the worker starts automatically with signing enabled.

### 2. Pair the client

On the **client** machine (within 60 seconds):

```bash
away pair --backend redis://localhost:6379
```

```
  Enter pairing PIN: 482913
  Waiting for worker...

  ✓ Paired successfully as 'client'.
    Peer role: worker
    Key saved to ~/.away/client.key
```

Both machines now share a cryptographic key stored in `~/.away/`.

### 3. Run tasks (no changes needed)

```bash
python examples/remote_execution.py
```

The client automatically loads `~/.away/client.key` and signs tasks before submission. No code changes are needed.

### Alternative: manual pairing and worker start

If you need more control, you can pair and start the worker separately:

```bash
# Pair the worker
away pair --backend redis://localhost:6379 --role worker

# Pair the client (same PIN)
away pair --backend redis://localhost:6379

# Start the worker with signing enforcement
away worker --backend redis://localhost:6379 --require-signing
```

## How it works

### Key resolution

When signing is enabled, both client and worker resolve the signing key using the following precedence order:

1. **`AWAY_SIGNING_TOKEN` environment variable** — hex-encoded token (highest priority)
2. **`~/.away/token` file** — hex-encoded token written by `away token generate`
3. **`~/.away/{client,worker}.key` file** — raw bytes from PIN-based pairing

This means you can migrate from pairing to tokens without disruption: set the environment variable and it takes precedence over any existing pairing key.

### Token signing

```
Generate (once)                       Distribute
──────────────                        ──────────
away token generate                 Copy token to CI secrets,
    │                                 env vars, or config
    └─→ random 32-byte token          │
        saved to ~/.away/token       │
                                       ▼
Client                                Worker
──────                                ──────
Load token                            Load token
    │                                     │
    ├── HMAC-SHA256(key, task_json)       │
    ├── attach signature                  │
    ├── submit to backend ──────────────→ │
    │                                     ├── extract signature
    │                                     ├── HMAC-SHA256(key, task_json)
    │                                     ├── constant-time compare
    │                                     │
    │                                     ├── match? → execute
    │                                     └── mismatch? → reject
```

### Pairing protocol

The pairing protocol is inspired by SPAKE2 and SAS-based verification:

```
Worker (Initiator)                    Client (Responder)
──────────────────                    ──────────────────
Enter PIN: 482913                     Enter PIN: 482913
        │                                     │
        ├── derive intermediate key ──────────┤
        │   HMAC-SHA256(salt, PIN)            │
        │                                     │
        ├── generate random challenge         │
        │   (32 bytes)                        │
        │                                     │
        ├── publish challenge ───────────────→│
        │                                     ├── compute response
        │                                     │   HMAC(intermediate, challenge)
        │                                     │
        │←──────────────────── send response ─┤
        │                                     │
        ├── verify response                   │
        │   (constant-time compare)           │
        │                                     │
        ├── derive shared secret              │
        │   HMAC(intermediate,                │
        │        challenge ‖ "confirmed")     │
        │                                     │
        ├── send confirmation ───────────────→│
        │                                     ├── derive shared secret
        │                                     │   (same computation)
        │                                     │
    Save ~/.away/worker.key          Save ~/.away/client.key
```

**Security properties:**
- A passive eavesdropper observing the challenge and response cannot recover the PIN or derive the shared secret.
- The challenge nonce is random per session, preventing replay attacks.
- Response verification uses constant-time comparison to prevent timing attacks.
- The shared key is never transmitted — both sides derive it independently.

### Task signing

Once a shared key is established (via token or pairing), the client signs tasks with HMAC-SHA256:

```
Client                                Worker
──────                                ──────
Task JSON ──→ HMAC-SHA256(key, json)  │
         ──→ attach signature         │
         ──→ submit to backend ──────→│
                                      ├── extract signature
                                      ├── re-serialize payload
                                      ├── HMAC-SHA256(key, json)
                                      ├── constant-time compare
                                      │
                                      ├── match? → execute
                                      └── mismatch? → reject
```

The signature covers the entire task payload — graph JSON, function name, arguments, and metadata. Any tampering is detected.

## CLI reference

### `away token generate`

```bash
away token generate [--force]
```

Generates a random 32-byte signing token and saves it to `~/.away/token`. Prints the hex-encoded token and usage instructions.

| Flag | Description |
|------|-------------|
| `--force` | Overwrite an existing token |

### `away token show`

```bash
away token show
```

Displays the current token source (environment variable or file) and a truncated preview.

### `away token clear`

```bash
away token clear
```

Removes the saved `~/.away/token` file.

### `away worker --pair`

```bash
away worker --backend URL --pair
```

Generates a PIN, pairs with a client, then starts serving with signing automatically enabled. This is the recommended way to set up a signed worker interactively.

### `away pair`

```bash
away pair --backend URL [--pin PIN] [--timeout SECS] [--force] [--clear]
```

| Flag | Description |
|------|-------------|
| `--backend` | Backend URL for the pairing channel |
| `--pin` | Specify a PIN (prompted interactively if omitted) |
| `--timeout` | Seconds to wait for the peer (default: 60) |
| `--force` | Overwrite an existing shared key |
| `--clear` | Remove the shared key for this role |
| `--role` | `client` (default) or `worker` — use `away worker --pair` instead of `--role worker` |

### `away worker --require-signing`

```bash
away worker --backend URL --require-signing
```

When `--require-signing` is set, the worker loads signing key material using the standard resolution order (env var → token file → pairing key) and rejects any task that is unsigned or has an invalid signature. If no key material is found, the worker exits with an error.

## Programmatic usage

### Signing tasks manually

```python
from away.core.signing import derive_key, compute_signature, verify_signature

# After pairing or with a token, both sides have the same shared_key
signing_key = derive_key(shared_key)

# Sign
signature = compute_signature(payload, signing_key)

# Verify
is_valid = verify_signature(payload, signature, signing_key)
```

### Using Task signing

```python
from away.core.task import Task
from away.core.signing import derive_key

key = derive_key(shared_secret)

# Client: sign on serialization
task = Task(graph_json=graph, function_name="my.func", args=(1, 2))
signed_json = task.to_json(signing_key=key)

# Worker: verify on deserialization
task = Task.from_json(signed_json, signing_key=key)  # raises SignatureError on failure
```

### Resolving keys programmatically

```python
from away.core.token import resolve_signing_key

# Resolves from env var → token file → pairing key
key = resolve_signing_key("client")  # or "worker"
if key is not None:
    signed_json = task.to_json(signing_key=key)
```

### Token management

```python
from away.core.token import generate_token, save_token, load_token, clear_token

# Generate and save
token = generate_token()
save_token(token)

# Load (checks env var first, then file)
token = load_token()

# Clean up
clear_token()
```

### Pairing programmatically

```python
from away.core.pairing import (
    generate_pin,
    initiate_pairing,
    respond_to_pairing,
    save_shared_key,
)

# Worker side (initiator)
pin = generate_pin()
result = await initiate_pairing(backend, pin, timeout=60.0)
save_shared_key(result.shared_key, "worker")

# Client side (responder)
result = await respond_to_pairing(backend, pin, timeout=60.0)
save_shared_key(result.shared_key, "client")
```

## Key management

| File | Purpose |
|------|---------|
| `~/.away/token` | Pre-shared signing token (hex-encoded, 64 chars) |
| `~/.away/client.key` | Client's pairing key (32 bytes, from `away pair`) |
| `~/.away/worker.key` | Worker's pairing key (32 bytes, from `away pair`) |

All files are created with `0600` permissions (owner-only read/write).

| Environment variable | Purpose |
|---------------------|---------|
| `AWAY_SIGNING_TOKEN` | Hex-encoded signing token (overrides file) |

### CI/CD example (GitHub Actions)

```yaml
# .github/workflows/deploy.yml
jobs:
  run-task:
    runs-on: ubuntu-latest
    env:
      AWAY_SIGNING_TOKEN: ${{ secrets.AWAY_SIGNING_TOKEN }}
      AWAY_BACKEND: redis://your-redis-host:6379
    steps:
      - uses: actions/checkout@v4
      - run: pip install away[redis]
      - run: python my_task.py
```

### Regenerating tokens

```bash
away token generate --force
```

Then update the `AWAY_SIGNING_TOKEN` secret in your CI provider and restart workers.

### Clearing credentials

```bash
# Token
away token clear

# Pairing keys
away pair --clear
away pair --role worker --clear
```

## Troubleshooting

**"Signing is enabled but no key material found"**
- Set `AWAY_SIGNING_TOKEN`, run `away token generate`, or run `away pair` to establish key material.

**"Task is unsigned but signing is enabled"**
- The client is not signing tasks. Ensure the token is set via `AWAY_SIGNING_TOKEN` or `~/.away/token`, or that `~/.away/client.key` exists (from pairing).

**"Task signature verification failed"**
- The client and worker have different keys. Ensure both sides use the same token or re-pair.

**"PIN mismatch — pairing failed"**
- The PINs entered on client and worker don't match. Try again.

**"Pairing timed out"**
- Both sides must run pairing within the timeout window (default: 60s). Consider using tokens instead for automated setups.
