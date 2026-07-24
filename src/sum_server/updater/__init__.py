"""Out-of-process server self-updater.

Runs as ``python -m sum_server.updater`` in its own systemd transient unit so
``systemctl restart sum-server`` can't kill it mid-update. Drives a git
checkout + uv sync + alembic upgrade + restart, with a pg_dump snapshot taken
first and a full code+DB rollback on any failure. Status is written straight to
Postgres (durable across the restart) for the UI to poll.
"""
