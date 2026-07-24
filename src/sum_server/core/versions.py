"""Semantic-version parsing + comparison (major.minor.patch, optional ``v``)."""

from __future__ import annotations

import re

_SEMVER_RE = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")


def parse_version(raw: str | None) -> tuple[int, int, int] | None:
    """Parse ``0.2.0`` / ``v0.2.0`` / ``sum-agent/0.2.0`` -> ``(0, 2, 0)``.

    Returns ``None`` if no version is found.
    """
    if not raw:
        return None
    m = _SEMVER_RE.search(raw)
    if m is None:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def is_newer(candidate: str | None, current: str | None) -> bool:
    """True if ``candidate`` is a strictly newer version than ``current``."""
    c, cur = parse_version(candidate), parse_version(current)
    if c is None or cur is None:
        return False
    return c > cur


def within_one_minor(agent: str | None, server: str | None) -> bool:
    """N-1 support window: same major, agent minor within one of the server's.

    An agent at exactly the server version (or newer minor) is also supported;
    the guarantee is specifically that the previous minor keeps working.
    """
    a, s = parse_version(agent), parse_version(server)
    if a is None or s is None:
        return False
    if a[0] != s[0]:
        return False
    return a[1] >= s[1] - 1
