"""Restart a running migration service when its active S3 job stops checkpointing.

Standalone stdlib-only watchdog; run periodically from a systemd user timer.
It never modifies SQLite or starts an intentionally stopped migration service.
"""
import argparse
import datetime as dt
import logging
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

UNIT = "partage-fbx-migration.service"
DEFAULT_STALL_SECONDS = 10 * 60
LOG = logging.getLogger("migration-watchdog")


def database_path():
    return Path(os.environ.get("FREEBOX_SECRET_DIR", str(Path.home() / "partage-freebox-migration"))) / "migration.sqlite3"


def stalled_jobs(path, *, now=None, threshold_seconds=DEFAULT_STALL_SECONDS):
    """Return running jobs without a durable update for the threshold.

    engine.State updates jobs.updated_at when a job starts, when it records
    an S3 part (8 MiB), or when it changes state. Idle, queued, paused, done,
    and errored jobs must never trigger a restart.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    # mode=ro ensures this independent monitor cannot create or alter the DB.
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=3) as con:
        rows = con.execute(
            "SELECT id, transferred, updated_at FROM jobs WHERE state='running'"
        ).fetchall()
    result = []
    for jid, transferred, updated_at in rows:
        updated = dt.datetime.fromisoformat(updated_at)
        if updated.tzinfo is None:
            raise ValueError("Unexpected naive jobs.updated_at timestamp")
        elapsed = (now - updated).total_seconds()
        if elapsed >= threshold_seconds:
            result.append((jid, transferred, int(elapsed)))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Check and log only; never restart")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    # Don't resurrect the service after an intentional systemctl stop.
    active = subprocess.run(["systemctl", "--user", "is-active", "--quiet", UNIT], check=False)
    if active.returncode != 0:
        return 0
    try:
        stale = stalled_jobs(database_path())
    except (OSError, sqlite3.Error, ValueError) as exc:
        LOG.error("Cannot inspect migration progress; refusing restart: %s", exc)
        return 1
    if not stale:
        return 0

    jid, transferred, elapsed = stale[0]
    LOG.warning("No durable transfer progress for %ss (job=%s, checkpoint=%s bytes); %s",
                elapsed, jid, transferred,
                "dry run, no restart" if args.dry_run else "restarting migration service")
    if args.dry_run:
        return 0
    try:
        restarted = subprocess.run(["systemctl", "--user", "restart", UNIT],
                                   timeout=120, check=False)
    except subprocess.TimeoutExpired:
        LOG.error("Migration service restart timed out")
        return 1
    if restarted.returncode:
        LOG.error("Migration service restart failed (status %s)", restarted.returncode)
        return 1
    LOG.info("Migration service restarted; multipart checkpoints preserved")
    return 0


if __name__ == "__main__":
    sys.exit(main())
