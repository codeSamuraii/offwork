# Signing & Pairing

Cryptographically sign serialized tasks so that workers only execute code from trusted clients. The signing system uses a PIN-based pairing protocol — no manual key management required.

## Overview

By default, pyfuse workers execute any task they receive from the backend. When signing is enabled:

1. A client and worker **pair** once using a short PIN code.
2. Both sides derive a shared HMAC key from the PIN.
3. The client **signs** every task with HMAC-SHA256 before submitting it.
4. The worker **verifies** the signature before executing the task.

Tasks with missing or invalid signatures are rejected.

## Quick start

### 1. Pair the worker and client

On the **worker** machine:

```bash
pyfuse pair --backend redis://localhost:6379 --role worker
```

This generates a 6-digit PIN and waits:

```
  Pairing PIN:  482913

  Enter this PIN on the client side.
  Waiting for client...
```

On the **client** machine (within 60 seconds):

```bash
pyfuse pair --backend redis://localhost:6379 --role client
```

```
  Enter pairing PIN: 482913
  Waiting for worker...

  ✓ Paired successfully as 'client'.
    Peer role: worker
    Key saved to ~/.pyfuse/client.key
```

Both machines now share a cryptographic key stored in `~/.pyfuse/`.

### 2. Start the worker with signing enabled

```bash
pyfuse worker --backend redis://localhost:6379 --require-signing
```

The worker loads `~/.pyfuse/worker.key` and rejects unsigned tasks.

### 3. Run tasks (no changes needed)

```bash
python examples/remote_execution.py
```

The client automatically loads `~/.pyfuse/client.key` and signs tasks before submission. No code changes are needed.

## How it works

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
    Save ~/.pyfuse/worker.key          Save ~/.pyfuse/client.key
```

**Security properties:**
- A passive eavesdropper observing the challenge and response cannot recover the PIN or derive the shared secret.
- The challenge nonce is random per session, preventing replay attacks.
- Response verification uses constant-time comparison to prevent timing attacks.
- The shared key is never transmitted — both sides derive it independently.

### Task signing

Once paired, the client signs tasks with HMAC-SHA256:

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

### `pyfuse pair`

```bash
pyfuse pair --backend URL --role {client|worker} [--pin PIN] [--timeout SECS] [--force] [--clear]
```

| Flag | Description |
|------|-------------|
| `--backend` | Backend URL for the pairing channel |
| `--role` | `client` or `worker` |
| `--pin` | Specify a PIN (auto-generated for worker, prompted for client) |
| `--timeout` | Seconds to wait for the peer (default: 60) |
| `--force` | Overwrite an existing shared key |
| `--clear` | Remove the shared key for this role |

### `pyfuse worker --require-signing`

```bash
pyfuse worker --backend URL --require-signing
```

When `--require-signing` is set, the worker loads `~/.pyfuse/worker.key` and rejects any task that is unsigned or has an invalid signature. If no key file is found, the worker exits with an error.

## Programmatic usage

### Signing tasks manually

```python
from pyfuse.core.signing import derive_key, compute_signature, verify_signature

# After pairing, both sides have the same shared_key
signing_key = derive_key(shared_key)

# Sign
signature = compute_signature(payload, signing_key)

# Verify
is_valid = verify_signature(payload, signature, signing_key)
```

### Using Task signing

```python
from pyfuse.core.task import Task
from pyfuse.core.signing import derive_key

key = derive_key(shared_secret)

# Client: sign on serialization
task = Task(graph_json=graph, function_name="my.func", args=(1, 2))
signed_json = task.to_json(signing_key=key)

# Worker: verify on deserialization
task = Task.from_json(signed_json, signing_key=key)  # raises SignatureError on failure
```

### Pairing programmatically

```python
from pyfuse.core.pairing import (
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
| `~/.pyfuse/client.key` | Client's shared key (32 bytes) |
| `~/.pyfuse/worker.key` | Worker's shared key (32 bytes) |

Both files are created with `0600` permissions (owner-only read/write).

To re-pair, use `--force`:

```bash
pyfuse pair --backend redis://localhost:6379 --role worker --force
```

To remove keys:

```bash
pyfuse pair --role client --backend redis://localhost:6379 --clear
pyfuse pair --role worker --backend redis://localhost:6379 --clear
```

## Troubleshooting

**"Signing is enabled but no shared key found"**
- Run `pyfuse pair` first to establish a shared key.

**"Task is unsigned but signing is enabled"**
- The client is not signing tasks. Ensure `~/.pyfuse/client.key` exists.

**"Task signature verification failed"**
- The client and worker have different keys. Re-pair both sides.

**"PIN mismatch — pairing failed"**
- The PINs entered on client and worker don't match. Try again.

**"Pairing timed out"**
- Both sides must run `pyfuse pair` within the timeout window (default: 60s).
