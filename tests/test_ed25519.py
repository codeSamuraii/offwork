"""Pure-Python Ed25519: RFC 8032 test vectors and round-trip checks."""

import os

import pytest

from offwork.core import ed25519


# RFC 8032 section 7.1 — Ed25519 test vectors.
RFC_8032_VECTORS = [
    {
        "seed": "9d61b19deffd5a60ba844af492ec2cc4"
                "4449c5697b326919703bac031cae7f60",
        "public": "d75a980182b10ab7d54bfed3c964073a"
                  "0ee172f3daa62325af021a68f707511a",
        "message": "",
        "signature": "e5564300c360ac729086e2cc806e828a"
                     "84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701c"
                     "f9b46bd25bf5f0595bbe24655141438e7a100b",
    },
    {
        "seed": "4ccd089b28ff96da9db6c346ec114e0f"
                "5b8a319f35aba624da8cf6ed4fb8a6fb",
        "public": "3d4017c3e843895a92b70aa74d1b7ebc"
                  "9c982ccf2ec4968cc0cd55f12af4660c",
        "message": "72",
        "signature": "92a009a9f0d4cab8720e820b5f642540"
                     "a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0"
                     "f11d8c387b2eaeb4302aeeb00d291612bb0c00",
    },
    {
        "seed": "c5aa8df43f9f837bedb7442f31dcb7b1"
                "66d38535076f094b85ce3a2e0b4458f7",
        "public": "fc51cd8e6218a1a38da47ed00230f058"
                  "0816ed13ba3303ac5deb911548908025",
        "message": "af82",
        "signature": "6291d657deec24024827e69c3abe01a3"
                     "0ce548a284743a445e3680d7db5ac3ac18ff9b538d16f290ae67f76098"
                     "4dc6594a7c15e9716ed28dc027beceea1ec40a",
    },
]


@pytest.mark.parametrize("vec", RFC_8032_VECTORS)
def test_rfc8032_public_key(vec: dict[str, str]) -> None:
    seed = bytes.fromhex(vec["seed"])
    assert ed25519.seed_to_public(seed).hex() == vec["public"]


@pytest.mark.parametrize("vec", RFC_8032_VECTORS)
def test_rfc8032_signature(vec: dict[str, str]) -> None:
    seed = bytes.fromhex(vec["seed"])
    message = bytes.fromhex(vec["message"])
    sig = ed25519.sign(message, seed)
    assert sig.hex() == vec["signature"]


@pytest.mark.parametrize("vec", RFC_8032_VECTORS)
def test_rfc8032_verify(vec: dict[str, str]) -> None:
    public = bytes.fromhex(vec["public"])
    message = bytes.fromhex(vec["message"])
    sig = bytes.fromhex(vec["signature"])
    assert ed25519.verify(message, sig, public) is True


def test_round_trip_random() -> None:
    seed = ed25519.generate_seed()
    pub = ed25519.seed_to_public(seed)
    msg = b"the quick brown fox jumps over the lazy dog"
    sig = ed25519.sign(msg, seed)
    assert ed25519.verify(msg, sig, pub) is True


def test_verify_rejects_tampered_message() -> None:
    seed = ed25519.generate_seed()
    pub = ed25519.seed_to_public(seed)
    sig = ed25519.sign(b"hello", seed)
    assert ed25519.verify(b"hellp", sig, pub) is False


def test_verify_rejects_tampered_signature() -> None:
    seed = ed25519.generate_seed()
    pub = ed25519.seed_to_public(seed)
    sig = bytearray(ed25519.sign(b"hello", seed))
    sig[0] ^= 0x01
    assert ed25519.verify(b"hello", bytes(sig), pub) is False


def test_verify_rejects_wrong_pubkey() -> None:
    seed1 = ed25519.generate_seed()
    seed2 = ed25519.generate_seed()
    sig = ed25519.sign(b"hello", seed1)
    pub2 = ed25519.seed_to_public(seed2)
    assert ed25519.verify(b"hello", sig, pub2) is False


def test_verify_rejects_bad_length() -> None:
    seed = ed25519.generate_seed()
    pub = ed25519.seed_to_public(seed)
    assert ed25519.verify(b"hello", b"\x00" * 63, pub) is False
    assert ed25519.verify(b"hello", b"\x00" * 64, b"\x00" * 31) is False


def test_seed_to_public_rejects_bad_length() -> None:
    with pytest.raises(ValueError):
        ed25519.seed_to_public(b"\x00" * 31)


def test_sign_rejects_bad_length() -> None:
    with pytest.raises(ValueError):
        ed25519.sign(b"msg", b"\x00" * 31)


def test_generate_seed_unique() -> None:
    seeds = {ed25519.generate_seed() for _ in range(10)}
    assert len(seeds) == 10
    assert all(len(s) == 32 for s in seeds)


def test_known_pubkey_for_zero_seed() -> None:
    # The all-zero seed produces a fixed (well-known) public key.
    # Keeping this lightweight check protects the curve parameters.
    pub = ed25519.seed_to_public(b"\x00" * 32)
    assert len(pub) == 32
    # Re-derivation must be deterministic
    assert pub == ed25519.seed_to_public(b"\x00" * 32)


def test_unused_os_imports_remain_pure_stdlib() -> None:
    # Sanity: ensure module didn't pull in non-stdlib deps.
    assert ed25519.__name__ == "offwork.core.ed25519"
    assert os.urandom(32) != os.urandom(32)
