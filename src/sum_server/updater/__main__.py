"""``python -m sum_server.updater`` — run the queued server self-update.

Launched by the running server (via ``systemd-run``) in its own transient
unit. Reads the most recent ``queued`` ``server_updates`` row, drives the
state machine, and writes progress back to that row.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import sys
from pathlib import Path

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from sum_server import __version__
from sum_server.core.db import get_engine, init_engine
from sum_server.core.logging import configure_logging
from sum_server.settings import get_settings
from sum_server.updater.orchestrator import run_update
from sum_server.updater.shell import ShellRunner
from sum_server.updater.status import DbReporter
from sum_server.updates.models import ServerUpdate

log = structlog.get_logger(__name__)


async def _main(dry_run: bool) -> int:
    settings = get_settings()
    configure_logging(settings)
    init_engine(settings.database_url)
    sm = async_sessionmaker(get_engine(), expire_on_commit=False)

    async with sm() as session:
        row = (
            await session.execute(
                select(ServerUpdate)
                .where(ServerUpdate.status == "queued")
                .order_by(ServerUpdate.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    if row is None:
        log.warning("no_queued_update")
        return 0
    if not settings.install_dir:
        log.error("install_dir_not_set")
        return 2

    if dry_run:
        print(f"would update {row.from_version} -> {row.to_version} in {settings.install_dir}")
        return 0

    async with sm() as session, session.begin():
        await session.execute(
            update(ServerUpdate)
            .where(ServerUpdate.id == row.id)
            .values(started_at=dt.datetime.now(tz=dt.UTC))
        )

    health_url = settings.external_url or "https://127.0.0.1"
    runner = ShellRunner(
        install_dir=Path(settings.install_dir),
        service_name=settings.service_name,
        database_url=settings.database_url,
        health_url=health_url,
        uv_bin=settings.uv_bin,
    )
    reporter = DbReporter(sm, row.id)
    final = await run_update(
        target_version=row.to_version,
        current_version=row.from_version,
        runner=runner,
        report=reporter,
        dumps_dir=settings.data_dir / "dumps",
    )
    log.info("update_finished", status=final, to=row.to_version)
    return 0 if final == "success" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sum_server.updater")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    log.info("updater_start", version=__version__, dry_run=args.dry_run)
    return asyncio.run(_main(args.dry_run))


if __name__ == "__main__":
    sys.exit(main())
