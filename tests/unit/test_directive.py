"""Signed agent-update directive round-trips against the server key."""

from __future__ import annotations

import base64
import uuid

from nacl.signing import SigningKey

from sum_server.core.security import signing
from sum_server.updates.directive import (
    build_directive,
    build_removal_directive,
    removal_signing_payload,
    signing_payload,
)


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


# --- Removal ----------------------------------------------------------------


def test_removal_directive_signature_verifies() -> None:
    host_id = uuid.uuid4()
    d = build_removal_directive(host_id=host_id, requested_at="2026-08-11T12:00:00+00:00")
    payload = removal_signing_payload(host_id, "2026-08-11T12:00:00+00:00")
    assert signing.verify(payload, base64.b64decode(d["signature"]))


def test_removal_directive_rejects_wrong_host() -> None:
    """The reason this is signed at all: an unsigned removal is a kill switch."""
    host_id = uuid.uuid4()
    d = build_removal_directive(host_id=host_id, requested_at="2026-08-11T12:00:00+00:00")
    other = removal_signing_payload(uuid.uuid4(), "2026-08-11T12:00:00+00:00")
    assert not signing.verify(other, base64.b64decode(d["signature"]))


def test_removal_directive_rejects_replayed_timestamp() -> None:
    """`requested_at` binds the signature to one request, not to the host forever."""
    host_id = uuid.uuid4()
    d = build_removal_directive(host_id=host_id, requested_at="2026-08-11T12:00:00+00:00")
    later = removal_signing_payload(host_id, "2026-09-01T09:00:00+00:00")
    assert not signing.verify(later, base64.b64decode(d["signature"]))


def test_update_signature_cannot_be_replayed_as_a_removal() -> None:
    """`action` is inside the signed payload, so the two kinds cannot cross over."""
    host_id = uuid.uuid4()
    upd = build_directive(
        host_id=host_id, target_version="0.3.0", sha256="abc123", binary_url="https://x"
    )
    sig = base64.b64decode(upd["signature"])
    assert not signing.verify(removal_signing_payload(host_id, "0.3.0"), sig)
