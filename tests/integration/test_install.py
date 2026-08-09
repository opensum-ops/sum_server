"""Server-hosted agent installer: script, binary, checksum, and the wizard.

The script endpoint is piped straight into a root shell on a managed host, so
these lean hard on two things: it must be syntactically valid, and it must never
serve something that is not a script.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient

from sum_server.install import service as install_svc
from sum_server.settings import get_settings
from tests.integration.test_ui import _ui_login

BIN = b"fake-agent-binary-for-install-tests"
SHA = hashlib.sha256(BIN).hexdigest()
VERSION = "9.9.9"


@pytest.fixture(autouse=True)
def _tmp_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)


def _stage_binary(version: str = VERSION) -> Path:
    """Put a binary in the cache, as if it had already been pulled from GitHub."""
    path = get_settings().data_dir / "agent-binaries" / version / "sum-agent-linux-amd64"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(BIN)
    path.with_suffix(".sha256").write_text(SHA, encoding="utf-8")
    return path


async def _known_release(db_session: Any, version: str = VERSION) -> None:
    from sum_server.updates.models import COMPONENT_AGENT, ReleaseCache

    db_session.add(ReleaseCache(repo=COMPONENT_AGENT, latest_version=version, assets=[]))
    await db_session.commit()


# --- the script --------------------------------------------------------------


def test_rendered_script_is_valid_shell() -> None:
    """It gets piped into a root shell on a real host; a syntax error must never ship.

    Sync on purpose: this checks the template, not the route, so it needs no
    database and can call the shell without blocking an event loop.
    """
    script = install_svc.render_script(server_url="https://s.example.com", version=VERSION)
    assert script.startswith("#!/bin/sh")
    assert "{{" not in script, "unrendered Jinja placeholder reached the script"

    sh = shutil.which("sh")
    assert sh is not None
    check = subprocess.run(  # noqa: S603 - fixed argv, input is our own template
        [sh, "-n"], input=script, text=True, capture_output=True, check=False
    )
    assert check.returncode == 0, check.stderr


async def test_install_script_is_served_as_a_script(client: AsyncClient, db_session: Any) -> None:
    await _known_release(db_session)
    _stage_binary()

    r = await client.get("/install.sh")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/x-shellscript")
    assert r.text.startswith("#!/bin/sh")
    assert "{{" not in r.text


async def test_install_script_targets_this_server(client: AsyncClient, db_session: Any) -> None:
    await _known_release(db_session)
    _stage_binary()
    script = (await client.get("/install.sh")).text

    assert f'VERSION="{VERSION}"' in script
    assert 'SERVER_URL="http://testserver"' in script
    # The state dir the unit passes and the one enroll is given must be the
    # same, or the service comes up claiming the host is not enrolled.
    assert f'STATE_DIR="{install_svc.STATE_DIR}"' in script
    assert 'SUM_AGENT_STATE_DIR="$STATE_DIR"' in script
    assert "EnvironmentFile=${ENV_FILE}" in script


def test_rendered_script_points_the_agent_at_the_system_ca_bundle() -> None:
    """httpx trusts certifi, not the OS store, so a private CA needs SSL_CERT_FILE.

    Without this the agent fails TLS on its first enroll even though the curl
    that fetched it succeeded, because curl does use the OS store. See the
    2026-07-22 deployment notes.
    """
    script = install_svc.render_script(server_url="https://s.example.com", version=VERSION)

    assert 'echo "SSL_CERT_FILE=${bundle}" >> "$ENV_FILE"' in script
    assert "/etc/ssl/certs/ca-certificates.crt" in script  # debian, ubuntu
    assert "/etc/pki/tls/certs/ca-bundle.crt" in script  # rhel, fedora
    # Appended, not written into the heredoc, so the probe cannot clobber the
    # two settings the unit depends on.
    assert script.index("SUM_AGENT_STATE_DIR=${STATE_DIR}") < script.index("SSL_CERT_FILE")


async def test_install_script_needs_no_auth(client: AsyncClient, db_session: Any) -> None:
    """A host being installed has no agent token yet."""
    await _known_release(db_session)
    _stage_binary()
    r = await client.get("/install.sh")
    assert r.status_code == 200
    assert "sum_session" not in r.headers.get("set-cookie", "")


# --- binary and checksum -----------------------------------------------------


async def test_binary_and_checksum_agree(client: AsyncClient, db_session: Any) -> None:
    await _known_release(db_session)
    _stage_binary()

    b = await client.get(f"/install/sum-agent/{VERSION}/linux-amd64")
    assert b.status_code == 200
    assert b.content == BIN

    c = await client.get(f"/install/sum-agent/{VERSION}/linux-amd64.sha256")
    assert c.status_code == 200
    assert c.text.split()[0] == SHA
    # The checksum has to describe the bytes actually served, or the script
    # aborts on every host.
    assert hashlib.sha256(b.content).hexdigest() == c.text.split()[0]


async def test_checksum_route_wins_over_binary_route(client: AsyncClient, db_session: Any) -> None:
    """`{arch}` would happily swallow "linux-amd64.sha256" and serve the binary."""
    await _known_release(db_session)
    _stage_binary()
    c = await client.get(f"/install/sum-agent/{VERSION}/linux-amd64.sha256")
    assert c.content != BIN
    assert len(c.text.split()[0]) == 64


async def test_unknown_arch_is_refused(client: AsyncClient, db_session: Any) -> None:
    await _known_release(db_session)
    _stage_binary()
    r = await client.get(f"/install/sum-agent/{VERSION}/linux-arm64")
    assert r.status_code == 503
    assert "arm64" in r.text


# --- degraded server ---------------------------------------------------------


async def test_no_known_release_serves_inert_comment(client: AsyncClient) -> None:
    """Every line must be a comment: an unguarded pipe should do nothing."""
    r = await client.get("/install.sh")
    assert r.status_code == 503
    assert "Check for updates" in r.text
    for line in r.text.splitlines():
        assert not line.strip() or line.lstrip().startswith("#"), line


# --- the wizard --------------------------------------------------------------


async def test_wizard_leads_with_the_one_liner(
    client: AsyncClient, admin_token: str, db_session: Any
) -> None:
    await _known_release(db_session)
    _stage_binary()
    await _ui_login(client, "admin@example.com", "admin-pw-1234")
    csrf = client.cookies["sum_csrf"]

    r = await client.post(
        "/hosts/enroll", data={"csrf_token": csrf, "name": "installer-node", "ttl_seconds": "3600"}
    )
    assert r.status_code == 200, r.text
    assert "/install.sh | sudo sh -s -- --token" in r.text
    assert VERSION in r.text
    assert "curl -fsSL" in r.text  # -f, or an error page gets piped into sh


async def test_wizard_says_so_when_the_installer_cannot_run(
    client: AsyncClient, admin_token: str
) -> None:
    """No known release: show the reason, not a command that fails on the host."""
    await _ui_login(client, "admin@example.com", "admin-pw-1234")
    csrf = client.cookies["sum_csrf"]

    r = await client.post(
        "/hosts/enroll", data={"csrf_token": csrf, "name": "no-installer", "ttl_seconds": "3600"}
    )
    assert r.status_code == 200, r.text
    assert "installer is not available" in r.text
    assert "/install.sh | sudo sh" not in r.text
    # The manual path must still be usable.
    assert "sum-agent enroll --token" in r.text
    assert "systemctl enable --now" in r.text


async def test_install_command_helper() -> None:
    with_token = install_svc.install_command(server_url="https://s.example.com/", token="abc")
    assert with_token == "curl -fsSL https://s.example.com/install.sh | sudo sh -s -- --token abc"
    assert install_svc.install_command(server_url="https://s.example.com") == (
        "curl -fsSL https://s.example.com/install.sh | sudo sh"
    )
