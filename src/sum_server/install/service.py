"""Render the install script and resolve the binary it downloads."""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from sqlalchemy.ext.asyncio import AsyncSession

from sum_server.updates import agent_binary
from sum_server.updates.agent_binary import BinaryUnavailableError, CachedBinary
from sum_server.updates.models import COMPONENT_AGENT
from sum_server.updates.service import get_release_cache

# The only artifact the agent's release workflow publishes today.
ARCH = "linux-amd64"

# Paths the installer owns on a managed host. The state dir must match what the
# systemd unit passes, or `enroll` writes state somewhere `run` will not find it.
BIN_PATH = "/usr/local/bin/sum-agent"
ENV_FILE = "/etc/sum-agent/agent.env"
STATE_DIR = "/var/lib/sum-agent"
UNIT_PATH = "/etc/systemd/system/sum-agent.service"
SERVICE_NAME = "sum-agent"

# StrictUndefined: a typo in the template should fail the request loudly rather
# than silently emit an empty string into a script that gets piped to sh.
_env = Environment(
    loader=FileSystemLoader(str(Path(__file__).parent / "templates")),
    undefined=StrictUndefined,
    keep_trailing_newline=True,
    autoescape=False,  # noqa: S701 - shell script, not markup; escaping would corrupt it
)


class InstallerUnavailableError(Exception):
    """No agent binary can be offered (no known release, or it cannot be staged)."""


async def installable_version(session: AsyncSession) -> str:
    """The agent version a fresh host should install.

    The latest release the server knows about, so a new host lands on the same
    version the update path would immediately offer it.
    """
    cache = await get_release_cache(session, COMPONENT_AGENT)
    version = cache.latest_version if cache is not None else None
    if not version:
        raise InstallerUnavailableError(
            "the server does not know of any agent release yet; "
            "open Settings and run Check for updates"
        )
    return version


async def staged_binary(session: AsyncSession, version: str) -> CachedBinary:
    """The cached binary for ``version``, downloading it from GitHub if needed."""
    cached = agent_binary.cached_binary_if_present(version)
    if cached is not None:
        return cached
    try:
        return await agent_binary.ensure_cached(session, version)
    except BinaryUnavailableError as exc:
        raise InstallerUnavailableError(f"agent {version} could not be staged: {exc}") from exc


def render_script(*, server_url: str, version: str) -> str:
    """Render install.sh for this server."""
    return _env.get_template("install.sh.j2").render(
        server_url=server_url.rstrip("/"),
        version=version,
        arch=ARCH,
        bin_path=BIN_PATH,
        env_file=ENV_FILE,
        state_dir=STATE_DIR,
        unit_path=UNIT_PATH,
        service_name=SERVICE_NAME,
    )


def render_uninstall_script(*, server_url: str) -> str:
    """Render uninstall.sh for this server.

    Same path constants as the installer, which is the point: the two scripts
    are the two halves of one contract about what lives where on a host.
    Needs no version or arch, because removing files does not depend on which
    build put them there.
    """
    return _env.get_template("uninstall.sh.j2").render(
        server_url=server_url.rstrip("/"),
        bin_path=BIN_PATH,
        env_file=ENV_FILE,
        state_dir=STATE_DIR,
        unit_path=UNIT_PATH,
        service_name=SERVICE_NAME,
    )


def uninstall_command(*, server_url: str) -> str:
    """The copy-pasteable manual removal command.

    ``-f`` for the same reason as the installer: without it an HTTP error body
    would be piped into sh.
    """
    return f"curl -fsSL {server_url.rstrip('/')}/uninstall.sh | sudo sh"


def install_command(*, server_url: str, token: str | None = None) -> str:
    """The copy-pasteable command shown in the enrollment wizard.

    ``-f`` matters: without it an HTTP error body would be piped into sh.
    """
    base = f"curl -fsSL {server_url.rstrip('/')}/install.sh | sudo sh"
    return f"{base} -s -- --token {token}" if token else base
