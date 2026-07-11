from __future__ import annotations

from sum_server.core.security.tokens import hash_token, mint_token, verify_token


def test_mint_returns_distinct_tokens() -> None:
    a, _ = mint_token()
    b, _ = mint_token()
    assert a != b


def test_hash_is_constant_time_verifiable() -> None:
    raw, h = mint_token()
    assert verify_token(raw, h)
    assert not verify_token("tampered" + raw, h)


def test_hash_stable_for_same_input() -> None:
    raw = "abc123"
    assert hash_token(raw) == hash_token(raw)
