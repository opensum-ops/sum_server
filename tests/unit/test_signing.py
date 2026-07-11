from __future__ import annotations

import base64

from nacl.signing import SigningKey

from sum_server.core.security import signing


def setup_module() -> None:
    seed = SigningKey.generate().encode()
    signing.load_signing_key("inline:" + base64.b64encode(seed).decode())


def test_canonical_bytes_is_deterministic() -> None:
    a = signing.canonical_bytes({"b": 2, "a": 1})
    b = signing.canonical_bytes({"a": 1, "b": 2})
    assert a == b


def test_sign_then_verify_round_trip() -> None:
    payload = {"hello": "world", "n": 42}
    sig = signing.sign(payload)
    assert signing.verify(payload, sig) is True


def test_tampered_payload_fails_verify() -> None:
    payload = {"hello": "world"}
    sig = signing.sign(payload)
    assert signing.verify({"hello": "WORLD"}, sig) is False


def test_public_key_b64_is_44_chars() -> None:
    pub = signing.get_public_key_b64()
    assert len(pub) == 44  # 32 bytes base64
