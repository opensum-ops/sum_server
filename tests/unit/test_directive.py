"""Signed agent-update directive round-trips against the server key."""

from __future__ import annotations

import base64
import uuid

from nacl.signing import SigningKey

from sum_server.core.security import signing
from sum_server.updates.directive import build_directive, signing_payload


def setup_module() -> None:
    seed = SigningKey.generate().encode()
    signing.load_signing_key("inline:" + base64.b64encode(seed).decode())


def test_directive_signature_verifies() -> None:
    host_id = uuid.uuid4()
    d = build_directive(
        host_id=host_id,
        target_version="0.3.0",
        sha256="abc123",
        binary_url="https://sum/api/v1/agents/binary/0.3.0",
    )
    payload = signing_payload(host_id, "0.3.0", "abc123")
    sig = base64.b64decode(d["signature"])
    assert signing.verify(payload, sig)


def test_directive_rejects_wrong_host() -> None:
    host_id = uuid.uuid4()
    d = build_directive(
        host_id=host_id, target_version="0.3.0", sha256="abc123", binary_url="https://x"
    )
    sig = base64.b64decode(d["signature"])
    # An attacker replaying the directive at a different host must fail.
    other = signing_payload(uuid.uuid4(), "0.3.0", "abc123")
    assert not signing.verify(other, sig)


def test_directive_rejects_tampered_hash() -> None:
    host_id = uuid.uuid4()
    d = build_directive(
        host_id=host_id, target_version="0.3.0", sha256="abc123", binary_url="https://x"
    )
    sig = base64.b64decode(d["signature"])
    tampered = signing_payload(host_id, "0.3.0", "deadbeef")
    assert not signing.verify(tampered, sig)
