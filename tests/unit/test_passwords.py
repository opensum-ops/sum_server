from __future__ import annotations

from sum_server.core.security.passwords import hash_password, verify_password


def test_hash_then_verify() -> None:
    h = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", h)
    assert not verify_password("wrong", h)


def test_hashes_are_salted_so_two_hashes_of_same_pw_differ() -> None:
    a = hash_password("hello")
    b = hash_password("hello")
    assert a != b
