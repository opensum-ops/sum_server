"""Opaque token mint + verify.

Tokens are 32 random bytes URL-safe base64-encoded. Storage uses SHA-256 hashes
so a DB leak does not expose live tokens. Comparisons are constant-time.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets

TOKEN_BYTES = 32

def mint_token() -> tuple[str, str]:
    """Return ``(raw_token, token_hash)``. Only ``raw_token`` is shown to callers."""
    raw = secrets.token_urlsafe(TOKEN_BYTES)
    return raw, hash_token(raw)

def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()

def verify_token(raw: str, expected_hash: str) -> bool:
    return hmac.compare_digest(hash_token(raw), expected_hash)