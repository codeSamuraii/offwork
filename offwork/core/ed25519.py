"""Pure-Python Ed25519 (RFC 8032) — no external dependencies.

Adapted from the RFC 8032 reference implementation (public domain).
Used by :mod:`offwork.core.identity` to give each client a stable
asymmetric identity that is bound to the machine, independent of the
shared signing token.

Performance is modest (~5–20 ms per sign/verify), which is acceptable
for offwork's one-signature-per-task usage pattern.  Hot paths can be
re-implemented later against ``cryptography`` if needed.
"""

import os
import hashlib

# Curve parameters (RFC 8032, section 5.1)
_P = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _P - 2, _P) % _P
_I = pow(2, (_P - 1) // 4, _P)


def _sha512(data: bytes) -> bytes:
    return hashlib.sha512(data).digest()


def _sha512_int(data: bytes) -> int:
    return int.from_bytes(_sha512(data), "little")


def _x_recover(y: int) -> int:
    xx = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P)
    x = pow(xx, (_P + 3) // 8, _P)
    if (x * x - xx) % _P != 0:
        x = (x * _I) % _P
    if x % 2 != 0:
        x = _P - x
    return x


_BY = 4 * pow(5, _P - 2, _P) % _P
_BX = _x_recover(_BY)
_B = (_BX % _P, _BY % _P, 1, (_BX * _BY) % _P)


def _point_add(
    P: tuple[int, int, int, int], Q: tuple[int, int, int, int]
) -> tuple[int, int, int, int]:
    A = ((P[1] - P[0]) * (Q[1] - Q[0])) % _P
    B = ((P[1] + P[0]) * (Q[1] + Q[0])) % _P
    C = 2 * P[3] * Q[3] * _D % _P
    D = 2 * P[2] * Q[2] % _P
    E = B - A
    F = D - C
    G = D + C
    H = B + A
    return (E * F % _P, G * H % _P, F * G % _P, E * H % _P)


def _scalar_mul(P: tuple[int, int, int, int], e: int) -> tuple[int, int, int, int]:
    if e == 0:
        return (0, 1, 1, 0)
    Q = _scalar_mul(P, e // 2)
    Q = _point_add(Q, Q)
    if e & 1:
        Q = _point_add(Q, P)
    return Q


def _encode_point(P: tuple[int, int, int, int]) -> bytes:
    zinv = pow(P[2], _P - 2, _P)
    x = P[0] * zinv % _P
    y = P[1] * zinv % _P
    encoded = y | ((x & 1) << 255)
    return encoded.to_bytes(32, "little")


def _decode_point(s: bytes) -> tuple[int, int, int, int] | None:
    if len(s) != 32:
        return None
    y = int.from_bytes(s, "little")
    sign = (y >> 255) & 1
    y &= (1 << 255) - 1
    if y >= _P:
        return None
    x = _x_recover(y)
    if x & 1 != sign:
        x = _P - x
    P = (x, y, 1, (x * y) % _P)
    if not _is_on_curve(P):
        return None
    return P


def _is_on_curve(P: tuple[int, int, int, int]) -> bool:
    x, y, z, _t = P
    zinv = pow(z, _P - 2, _P)
    xn = x * zinv % _P
    yn = y * zinv % _P
    lhs = (-xn * xn + yn * yn) % _P
    rhs = (1 + _D * xn * xn * yn * yn) % _P
    return lhs == rhs


def _clamp(h: bytes) -> int:
    a = bytearray(h[:32])
    a[0] &= 248
    a[31] &= 127
    a[31] |= 64
    return int.from_bytes(bytes(a), "little")


def generate_seed() -> bytes:
    """Return 32 random bytes suitable as an Ed25519 seed."""
    return os.urandom(32)


def seed_to_public(seed: bytes) -> bytes:
    """Derive the 32-byte public key from a 32-byte seed."""
    if len(seed) != 32:
        raise ValueError("seed must be 32 bytes")
    h = _sha512(seed)
    a = _clamp(h)
    A = _scalar_mul(_B, a)
    return _encode_point(A)


def sign(message: bytes, seed: bytes) -> bytes:
    """Return the 64-byte Ed25519 signature of *message* under *seed*."""
    if len(seed) != 32:
        raise ValueError("seed must be 32 bytes")
    h = _sha512(seed)
    a = _clamp(h)
    prefix = h[32:]
    A = _encode_point(_scalar_mul(_B, a))
    r = _sha512_int(prefix + message) % _L
    R = _scalar_mul(_B, r)
    R_enc = _encode_point(R)
    k = _sha512_int(R_enc + A + message) % _L
    s = (r + k * a) % _L
    return R_enc + s.to_bytes(32, "little")


def verify(message: bytes, signature: bytes, public_key: bytes) -> bool:
    """Return ``True`` iff *signature* is a valid Ed25519 signature."""
    if len(signature) != 64 or len(public_key) != 32:
        return False
    R = _decode_point(signature[:32])
    if R is None:
        return False
    A = _decode_point(public_key)
    if A is None:
        return False
    s = int.from_bytes(signature[32:], "little")
    if s >= _L:
        return False
    k = _sha512_int(signature[:32] + public_key + message) % _L
    # Check [s]B == R + [k]A
    sB = _scalar_mul(_B, s)
    kA = _scalar_mul(A, k)
    RkA = _point_add(R, kA)
    return _encode_point(sB) == _encode_point(RkA)
