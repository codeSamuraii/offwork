# Signing & Pairing

Cryptographically sign serialized tasks so that workers only execute code from trusted clients.  Two key-distribution methods are available:

- **Token-based** (recommended, works for CI/CD): a shared 32-byte token serves as an *enrollment credential*; each client additionally has its own Ed25519 keypair.  The worker pins each client's public key on first contact (TOFU).
- **PIN-based pairing** (interactive): client and worker exchange a shared key via a 6-digit PIN.  The pairing key is then used like a token.

Both methods produce the same on-the-wire envelope.

## Why it's strong even with a shared token

The shared token is **never** the only thing that authenticates a task:

1. The client signs each envelope with an HMAC derived from `(token, client_id)` — different clients sign with different keys.
2. The client also signs with its **own Ed25519 private key**.  The matching public key is pinned by the worker on first sight (TOFU) and locked in.
3. Each envelope carries an issued-at timestamp and a nonce.  The worker enforces a clock-skew window and remembers nonces to reject replays.

Consequences:

- A leaked token cannot impersonate an **already-enrolled** client — the attacker would also need that client's Ed25519 private key.
- Revoking a single client (`offwork clients revoke <id>`) never forces a global token rotation.
- Captured envelopes cannot be replayed.

## Quick start — Token

### 1. Generate a token (once)

```bash
offwork token generate
```

```
  Token generated and saved to ~/.offwork/token

  Token: a1b2c3d4e5f6...

  Set this on both client and worker:
    export OFFWORK_SIGNING_TOKEN=a1b2c3d4e5f6...
```

### 2. Distribute the token

```bash
# Set on every client and worker
export OFFWORK_SIGNING_TOKEN=a1b2c3d4e5f6...
```

For CI/CD, store the token as a secret in your CI provider and inject it as `OFFWORK_SIGNING_TOKEN`.

### 3. Start the worker

```bash
offwork worker --backend redis://localhost:6379 --require-signing
```

### 4. Run tasks (no code changes)

```bash
python examples/remote_execution.py
```

The client auto-generates a `~/.offwork/client_id` and `~/.offwork/identity.key` on first use and signs each envelope with both.  The worker pins the client's public key on first contact.

## Quick start — PIN-based pairing

On the **worker**:

```bash
offwork worker --backend redis://localhost:6379 --pair
```

A 6-digit PIN appears; type it on the **client** within the timeout:

```bash
offwork pair --backend redis://localhost:6379
```

Both sides now hold the same shared key in `~/.offwork/`.  Submissions are signed automatically.

## Envelope on the wire

```json
{
  "id": "<task id>",
  "graph": "...",
  "function": "module.func",
  "args": [...], "kwargs": {...},
  "client_id":  "<32 hex chars>",
  "iat":        1716480000.123,
  "nonce":      "<16 hex chars>",
  "pubkey":     "<64 hex chars>",
  "ed_sig":     "<128 hex chars>",
  "signature":  "<HMAC hex>"
}
```

The HMAC and Ed25519 signatures both cover the canonical JSON of every other field (sorted, no whitespace), with the two signature fields stripped.

## Worker verification order

For each incoming envelope:

1. Reject if `client_id` is on the denylist (`offwork clients revoke`).
2. Reject if `|now − iat| > clock_skew` (default 300 s; tunable via `--clock-skew`).
3. Reject if `(client_id, nonce)` has already been seen (in-memory TTL cache).
4. Verify HMAC under `derive_key(token, "offwork-v1|client:" + client_id)`.
5. Look up `client_id` in `~/.offwork/known_clients.json`:
   - **First sight (TOFU)**: verify Ed25519 against the embedded `pubkey`, then store.
   - **Known**: verify Ed25519 against the *stored* pubkey; reject if the envelope's `pubkey` doesn't match.
6. Record the nonce and update `last_seen`.

Each rejection raises a specific subclass of `SignatureError`: `ReplayError`, `StaleTaskError`, `ClientRevokedError`, `IdentityMismatchError`.

## CLI reference

### `offwork token`

```bash
offwork token generate [--force]    # Generate ~/.offwork/token
offwork token show                  # Token source + local client_id/fingerprint
offwork token clear                 # Remove ~/.offwork/token
```

### `offwork worker --require-signing`

```bash
offwork worker --backend URL --require-signing [--clock-skew SECONDS]
```

| Flag             | Description                                                            |
|------------------|------------------------------------------------------------------------|
| `--require-signing` | Enforce signed envelopes                                            |
| `--clock-skew`      | Max `\|now − iat\|` (default 300s)                                  |

### `offwork pair`

```bash
offwork worker --backend URL --pair          # Worker side
offwork pair --backend URL [--pin PIN]       # Client side
offwork pair --backend URL --clear           # Remove pairing key
```

### `offwork clients` (worker-side)

```bash
offwork clients list                # Table of all enrolled clients
offwork clients show <client_id>    # Full record (pubkey, fingerprint, seen times, revoked)
offwork clients revoke <client_id>  # Reject future submissions from this client
offwork clients approve <client_id> # Un-revoke
```

## Programmatic usage

### Sign / verify with the new envelope

```python
import offwork
from offwork.core.envelope import build_signed_envelope, verify_task_envelope
from offwork.core.signing import NonceLRU
from offwork.core.clients import KnownClients
from offwork.core.identity import (
    get_client_id, get_identity_seed, get_public_key,
)
from offwork.core.token import resolve_root_token
from offwork.core.task import Task

# Client side
task = Task(graph_json=offwork.serialize(my_func),
            function_name="m.my_func", args=(1, 2))
envelope = build_signed_envelope(
    task,
    root_token=resolve_root_token("client"),
    client_id=get_client_id(),
    identity_seed=get_identity_seed(),
    public_key=get_public_key(),
)

# Worker side
known = KnownClients()
nonces = NonceLRU()
restored = verify_task_envelope(
    envelope,
    root_token=resolve_root_token("worker"),
    known_clients=known,
    nonce_lru=nonces,
)
```

### Token management

```python
from offwork.core.token import (
    generate_token, save_token, load_token, clear_token, resolve_root_token,
)

token = generate_token()
save_token(token)
loaded = load_token()                # checks env var first
raw = resolve_root_token("client")   # raw 32 bytes, ready for derive_key
clear_token()
```

### Pairing programmatically

```python
from offwork.core.pairing import (
    generate_pin, initiate_pairing, respond_to_pairing, save_shared_key,
)

# Worker (initiator)
result = await initiate_pairing(backend, generate_pin(), timeout=60.0)
save_shared_key(result.shared_key, "worker")

# Client (responder)
result = await respond_to_pairing(backend, pin, timeout=60.0)
save_shared_key(result.shared_key, "client")
```

## Files and environment

| File                              | Purpose                                                    |
|-----------------------------------|------------------------------------------------------------|
| `~/.offwork/token`                | Pre-shared root token (hex, 64 chars)                      |
| `~/.offwork/client_id`            | This machine's stable 32-hex client id (auto-generated)    |
| `~/.offwork/identity.key`         | This machine's Ed25519 seed (raw 32 bytes, auto-generated) |
| `~/.offwork/client.key`           | Pairing key, client role                                   |
| `~/.offwork/worker.key`           | Pairing key, worker role                                   |
| `~/.offwork/known_clients.json`   | Worker-side TOFU registry + denylist                       |

All files are created with `0600` permissions.

| Environment variable        | Purpose                                       |
|-----------------------------|-----------------------------------------------|
| `OFFWORK_SIGNING_TOKEN`     | Hex-encoded root token (overrides file)       |

### CI/CD example (GitHub Actions)

```yaml
jobs:
  run-task:
    runs-on: ubuntu-latest
    env:
      OFFWORK_SIGNING_TOKEN: ${{ secrets.OFFWORK_SIGNING_TOKEN }}
      OFFWORK_BACKEND: redis://your-redis-host:6379
    steps:
      - uses: actions/checkout@v4
      - run: pip install offwork[redis]
      - run: python my_task.py
```

### Rotating credentials

```bash
offwork token generate --force          # New token; update CI secret and restart workers
offwork pair --clear                    # Remove pairing key
offwork clients revoke <client_id>      # Revoke a single client
```

## Troubleshooting

| Error                       | Likely cause                                                          |
|-----------------------------|-----------------------------------------------------------------------|
| `SignatureError`            | Token mismatch, tampered envelope, or invalid Ed25519 signature.      |
| `StaleTaskError`            | Client and worker clocks differ by more than `--clock-skew`.          |
| `ReplayError`               | The same envelope was submitted twice.                                |
| `IdentityMismatchError`     | A pinned `client_id` is now presenting a different public key (e.g. the user deleted `~/.offwork/identity.key`). Re-enrol by picking a new `client_id` (delete `~/.offwork/client_id`) or remove the worker's pin. |
| `ClientRevokedError`        | The client has been revoked with `offwork clients revoke`.            |
| `Signing is enabled but no key material found` | Set `OFFWORK_SIGNING_TOKEN`, run `offwork token generate`, or run `offwork pair`. |
