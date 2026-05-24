"""Ed25519 sign/verify + canonical bytes for jobs.

The signing key is loaded once at startup via :func:`load_signing_key`; subsequent
calls use the cached value. Canonical bytes are deterministic JSON so the same
payload always signs to the same bytes regardless of dict iteration order.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from nacl.exceptions import BadSignatureError
from nacl.signing import SigningKey, VerifyKey

from sum_server.core.errors import SignatureError

_signing_key: SigningKey | None = None
_verify_key: VerifyKey | None = None

def load_signing_key(source: str) -> None:
    """Load the Ed25519 signing key from a path or inline encoding.

    Accepted ``source`` forms:

    - ``inline:<base64-32-bytes>`` — for dev/test only (rejected in prod by
      :class:`sum_server.settings.Settings`).
    - A filesystem path to a file containing the raw 32-byte seed.
    """
    global _signing_key, _verify_key
    if source.startswith("inline:"):
        seed = base64.b64decode(source.removeprefix("inline:"))
    else:
        seed = Path(source).read_bytes()
    if len(seed) != 32:
        raise ValueError(f"signing key must be 32 raw bytes (got {len(seed)})")
    _signing_key = SigningKey(seed)
    _verify_key = _signing_key.verify_key

def is_loaded() -> bool:
    return _signing_key is not None

def get_public_key_bytes() -> bytes:
    if _verify_key is None:
        raise RuntimeError("signing key not loaded; call load_signing_key() first")
    return bytes(_verify_key)

def get_public_key_b64() -> str:
    return base64.b64encode(get_public_key_bytes()).decode()

def canonical_bytes(payload: dict[str, Any]) -> bytes:
    """Deterministic JSON bytes for signing.
    
    Sorted keys, no whitespace, ASCII only. UUIDs and datetimes must already be
    serialized to strings by the caller.
    """
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()

def sign(payload: dict[str, Any]) -> bytes:
    if _signing_key is None:
        raise RuntimeError("signing key not loaded")
    return _signing_key.sign(canonical_bytes(payload)).signature

def verify(payload: dict[str, Any], signature: bytes) -> bool:
    if _verify_key is None:
        raise RuntimeError("signing key not loaded")
    try:
        _verify_key.verify(canonical_bytes(payload), signature)
    except BadSignatureError:
        return False
    return True

def verify_or_raise(payload: dict[str, Any], signature: bytes) -> None:
    if not verify(payload, signature):
        raise SignatureError()