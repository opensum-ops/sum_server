"""Agent version negotiation (best-effort N-1 backwards compat)."""

from __future__ import annotations

from starlette.requests import Request

from sum_server import __version__
from sum_server.agents.compat import agent_version_from_request, is_supported
from sum_server.core.versions import parse_version


def _request(user_agent: str = "") -> Request:
    headers = [(b"user-agent", user_agent.encode())] if user_agent else []
    return Request({"type": "http", "headers": headers})


def test_version_from_user_agent() -> None:
    assert agent_version_from_request(_request("sum-agent/0.2.0")) == "0.2.0"


def test_explicit_field_wins() -> None:
    assert agent_version_from_request(_request("sum-agent/0.2.0"), "0.3.0") == "0.3.0"


def test_no_version_available() -> None:
    assert agent_version_from_request(_request("curl/8.0")) is None
    assert agent_version_from_request(_request("")) is None


def test_is_supported_matrix() -> None:
    major, minor, _ = parse_version(__version__)  # type: ignore[misc]
    # unknown version -> best-effort supported
    assert is_supported(None)
    # current + one behind supported
    assert is_supported(__version__)
    assert is_supported(f"{major}.{minor - 1}.0" if minor > 0 else __version__)
    # two behind not supported (when there is room to go two behind)
    if minor >= 2:
        assert not is_supported(f"{major}.{minor - 2}.0")
    # different major not supported
    assert not is_supported(f"{major + 1}.0.0")
