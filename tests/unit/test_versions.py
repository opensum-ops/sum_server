"""Semantic-version helpers + N-1 compat window."""

from __future__ import annotations

import pytest

from sum_server.core.versions import is_newer, parse_version, within_one_minor


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0.2.0", (0, 2, 0)),
        ("v0.2.0", (0, 2, 0)),
        ("sum-agent/1.4.9", (1, 4, 9)),
        ("v10.0.3-rc1", (10, 0, 3)),
        ("", None),
        (None, None),
        ("garbage", None),
    ],
)
def test_parse_version(raw: str | None, expected: tuple[int, int, int] | None) -> None:
    assert parse_version(raw) == expected


def test_is_newer() -> None:
    assert is_newer("0.3.0", "0.2.0")
    assert is_newer("0.2.1", "0.2.0")
    assert is_newer("1.0.0", "0.9.9")
    assert not is_newer("0.2.0", "0.2.0")
    assert not is_newer("0.1.0", "0.2.0")
    assert not is_newer(None, "0.2.0")
    assert not is_newer("0.3.0", None)


def test_within_one_minor() -> None:
    # same version, one behind, and newer are all supported
    assert within_one_minor("0.3.0", "0.3.0")
    assert within_one_minor("0.2.0", "0.3.0")
    assert within_one_minor("0.2.5", "0.3.1")
    assert within_one_minor("0.4.0", "0.3.0")
    # two minors behind is out of the window
    assert not within_one_minor("0.1.0", "0.3.0")
    # major mismatch is never supported
    assert not within_one_minor("1.0.0", "0.3.0")
    assert not within_one_minor("0.3.0", "1.0.0")
    # unparseable -> not supported by this predicate
    assert not within_one_minor("x", "0.3.0")
