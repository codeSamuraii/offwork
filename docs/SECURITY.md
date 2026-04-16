# Security: Task Signing and Automated Pairing

pyfuse supports Ed25519 cryptographic task signing so that workers can verify the identity of clients submitting tasks. Unsigned or untrusted tasks are rejected before execution.

## Overview

| Concept | Description |
|---------|-------------|
| **Key pair** | An Ed25519 private + public key pair that identifies a client |
| **Trust store** | A directory of `.pub` files on the worker; only clients whose key is present can submit tasks |
| **Signing** | The client signs each task payload with its private key |
| **Verification** | The worker checks the signature against its trust store before executing |
| **Pairing** | A SPAKE2-based key exchange that registers a client's public key with a worker using only a short PIN |

## Installation

```bash
pip install pyfuse[signing]   # Ed25519 key generation and task signing
pip install pyfuse[pairing]   # Automated SPAKE2 pairing (includes signing)
```

## Quick setup

### Step 1 — Generate a key pair (client machine)

```bash
pyfuse keypair generate -o ~/.pyfuse/my_key.pem
# Private key: ~/.pyfuse/my_key.pem  (keep secret, mode 0600)
# Public key:  ~/.pyfuse/my_key.pub  (safe to share)
# Fingerprint: a1b2c3d4e5f6...
```

Print the fingerprint of an existing key:

```bash
pyfuse keypair fingerprint ~/.pyfuse/my_key.pem
```

### Step 2 — Register the client with the worker

#### Option A: Automated pairing (recommended)

Pairing derives a shared session key via [SPAKE2](https://www.ietf.org/archive/id/draft-irtf-cfrg-spake2-26.txt) using a short PIN as the password. The PIN is **never transmitted**; only its cryptographic commitment is exchanged over the backend.

```bash
# Worker (Terminal 1): generate a pairing code and wait
pyfuse pair accept \
    --backend redis://localhost:6379 \
    --trusted-keys /etc/pyfuse/keys
# Pairing code: 847291
# Waiting for client... (expires in 60s)

# Client (Terminal 2): enter the code
pyfuse pair request \
    --backend redis://localhost:6379 \
    --code 847291 \
    -o ~/.pyfuse/my_key.pem
# ✓ Paired! Fingerprint: a1b2c3d4e5f6...
#   Key saved to /etc/pyfuse/keys/
```

The worker saves the client's public key to `--trusted-keys` automatically. No file copying needed.

**Pairing protocol (SPAKE2 + AES-256-GCM):**

1. Both sides derive a session key via SPAKE2 using the PIN as the shared password.
2. The client encrypts its Ed25519 public key with the session key (AES-256-GCM).
3. The encrypted key is sent over a hashed channel on the backend (SHA-256 of the PIN — the raw PIN is never stored).
4. The worker decrypts the key, validates it, writes a `.pub` file, and sends the fingerprint as acknowledgement.
5. The client verifies the fingerprint matches its own key.

#### Option B: Manual key copy

```bash
# Generate a key pair
pyfuse keypair generate -o client.pem

# Copy client.pub to the worker's trusted-keys directory
scp client.pub worker-host:/etc/pyfuse/keys/
```

### Step 3 — Start a trusted worker

```bash
pyfuse worker \
    --backend redis://localhost:6379 \
    --trusted-keys /etc/pyfuse/keys
```

The worker loads every `.pub` file from the directory at startup. Unsigned tasks or tasks signed by an unknown key are rejected with `TrustError` before any user code runs.

### Step 4 — Sign tasks (client)

Pass the key pair to `.run()`, `.start()`, or `.map()` using the `_keypair` keyword argument:

```python
import asyncio
import pyfuse
from pyfuse import trace
from pyfuse.core.signing import KeyPair

pyfuse.connect("redis://localhost:6379")

keypair = KeyPair.from_file("~/.pyfuse/my_key.pem")

@trace
def process(data: str) -> str:
    return data.upper()

async def main() -> None:
    result = await process.run("hello", _keypair=keypair)
    print(result)

asyncio.run(main())
```

## Programmatic API

### Key pair management

```python
from pyfuse.core.signing import KeyPair

# Generate a new key pair
kp = KeyPair.generate()

# Save to disk (private: 0600 permissions, public: world-readable)
kp.save("client.pem")
kp.save_public("client.pub")

# Load from disk
kp = KeyPair.from_file("client.pem")

# Inspect
print(kp.fingerprint)   # SHA-256 hex digest of the public key
print(kp.public_bytes)  # Raw 32-byte Ed25519 public key
```

### Trust store

```python
from pyfuse.core.signing import TrustStore

# Load all .pub files from a directory
trust = TrustStore.from_directory("/etc/pyfuse/keys")

# Add individual keys
trust.add_public_key_file("client.pub")
trust.add_public_bytes(raw_32_bytes)

# Query
print(len(trust))                       # number of trusted keys
print(trust.is_trusted(fingerprint))    # True / False
print(trust.fingerprints)               # frozenset of all fingerprints
```

### Serving with a trust store

```python
import asyncio
import pyfuse
from pyfuse.core.signing import TrustStore

trust = TrustStore.from_directory("/etc/pyfuse/keys")

asyncio.run(pyfuse.serve(
    "redis://localhost:6379",
    trust_store=trust,
))
```

### Automated pairing (Python API)

```python
import asyncio
from pyfuse.core.pairing import (
    RedisPairingTransport,
    MemoryPairingTransport,  # for testing
    accept_pairing,
    request_pairing,
    generate_pairing_code,
)

# Worker side
async def worker_pair(backend_url: str, trusted_keys_dir: str) -> None:
    transport = RedisPairingTransport(backend_url)
    code = generate_pairing_code()          # 6-digit random PIN
    print(f"Pairing code: {code}")

    result = await accept_pairing(
        transport,
        code,
        trusted_keys_dir=trusted_keys_dir,  # saves .pub here
        timeout=60.0,
    )
    print(f"Paired! Fingerprint: {result.fingerprint}")

# Client side
async def client_pair(backend_url: str, code: str, key_path: str) -> None:
    transport = RedisPairingTransport(backend_url)

    result = await request_pairing(
        transport,
        code,
        save_path=key_path,   # generates and saves keypair here
        timeout=60.0,
    )
    print(f"Paired! Fingerprint: {result.fingerprint}")
```

## Error handling

```python
from pyfuse import TrustError

try:
    result = await my_function.run(..., _keypair=keypair)
except TrustError as e:
    print(f"Task rejected by worker: {e}")
```

`TrustError` is raised when:
- The task is unsigned and the worker requires signatures.
- The task is signed by a key not in the worker's trust store.
- The signature fails cryptographic verification.

## CLI reference

```
pyfuse keypair generate [-o OUTPUT]     Generate a new Ed25519 key pair
pyfuse keypair fingerprint KEY_FILE     Print the fingerprint of a key file

pyfuse pair accept                      Worker: display pairing code, wait for client
    --backend URL                       Backend URL (or $PYFUSE_BACKEND)
    --code CODE                         Use a specific code instead of generating one
    --trusted-keys DIR                  Save the client .pub file here
    --timeout SECONDS                   Timeout (default: 60)

pyfuse pair request                     Client: register with a worker
    --backend URL                       Backend URL (or $PYFUSE_BACKEND)
    --code CODE                         Pairing code from the worker
    -o / --output PATH                  Save the generated keypair here
    --timeout SECONDS                   Timeout (default: 60)

pyfuse worker                           Start a worker
    --trusted-keys DIR                  Reject tasks not signed by a key from this dir
    ...
```

## Security considerations

- **Keep the private key secret.** It is saved with mode `0600`. Never commit it to version control.
- **The pairing PIN is single-use.** Each `pair accept` / `pair request` exchange uses a fresh SPAKE2 session. Reusing a code for a second pairing requires a new `pair accept` invocation.
- **Pairing requires the backend to be reachable.** The SPAKE2 messages are exchanged over Redis (or another configured backend). Use TLS (`rediss://`) for production deployments.
- **Trust store is loaded at worker startup.** Adding a new key requires restarting the worker (or using automated pairing, which persists the key for the next restart).
- **Unsigned tasks are rejected entirely when a trust store is configured.** There is no fallback to unsigned execution.
