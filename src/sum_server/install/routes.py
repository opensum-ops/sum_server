"""Public installer endpoints: the script, the binary, and its checksum.

Unauthenticated by design (see the package docstring). Responses are plain text
or octet-stream, never the JSON error envelope: the script endpoint is piped
straight into a shell, so a failure must read as a shell comment rather than
arrive as markup.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response
from fastapi.responses import FileResponse, PlainTextResponse

from sum_server.core.db import SessionDep
from sum_server.install import service as svc
from sum_server.settings import get_settings

router = APIRouter(tags=["install"], include_in_schema=False)


def _server_url(request: Request) -> str:
    configured = get_settings().external_url.strip()
    return configured.rstrip("/") if configured else str(request.base_url).rstrip("/")


def _unavailable(reason: str) -> PlainTextResponse:
    """503 shaped so that `curl | sh` cannot mistake it for a script.

    `curl -f` means the pipe fails before this is ever executed; the body is
    for a human who fetched the URL directly, and every line is a comment so
    that even an unguarded pipe is inert.
    """
    return PlainTextResponse(
        f"# OpenSUM agent installer is not available:\n# {reason}\n",
        status_code=503,
        media_type="text/x-shellscript",
    )


@router.get("/install.sh")
async def install_script(request: Request, session: SessionDep) -> Response:
    try:
        version = await svc.installable_version(session)
        await svc.staged_binary(session, version)
    except svc.InstallerUnavailableError as exc:
        return _unavailable(str(exc))
    return PlainTextResponse(
        svc.render_script(server_url=_server_url(request), version=version),
        media_type="text/x-shellscript",
    )


@router.get("/uninstall.sh")
async def uninstall_script(request: Request) -> Response:
    """Manual agent removal, for the cases the button cannot reach.

    Takes no session and cannot fail: it only removes files whose paths are
    compile-time constants, so unlike the installer there is no release to
    resolve and nothing to stage. That also means an agent whose server is
    unreachable can still be removed with a script fetched from anywhere.
    """
    return PlainTextResponse(
        svc.render_uninstall_script(server_url=_server_url(request)),
        media_type="text/x-shellscript",
    )


# Declared before the binary route on purpose: `{arch}` would otherwise match
# "linux-amd64.sha256" and serve 20MB where a checksum was asked for.
@router.get("/install/sum-agent/{version}/{arch}.sha256")
async def install_checksum(version: str, arch: str, session: SessionDep) -> Response:
    if arch != svc.ARCH:
        return _unavailable(f"no {arch} build is published; only {svc.ARCH}")
    try:
        cached = await svc.staged_binary(session, version)
    except svc.InstallerUnavailableError as exc:
        return _unavailable(str(exc))
    return PlainTextResponse(f"{cached.sha256}  sum-agent-{version}-{arch}\n")


@router.get("/install/sum-agent/{version}/{arch}")
async def install_binary(version: str, arch: str, session: SessionDep) -> Response:
    if arch != svc.ARCH:
        return _unavailable(f"no {arch} build is published; only {svc.ARCH}")
    try:
        cached = await svc.staged_binary(session, version)
    except svc.InstallerUnavailableError as exc:
        return _unavailable(str(exc))
    return FileResponse(
        cached.path,
        media_type="application/octet-stream",
        filename=f"sum-agent-{version}-{arch}",
    )
