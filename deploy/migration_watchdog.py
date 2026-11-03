"""Restart a running migration service when its active S3 job stops checkpointing.

Standalone stdlib-only watchdog; run periodically from a systemd user timer.
It never modifies SQLite or starts an intentionally stopped migration service.
"""
import argparse
import json
import datetime as dt
import logging
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

UNIT = "partage-fbx-migration.service"
DEFAULT_STALL_SECONDS = 10 * 60
MAX_RESTARTS_WITHOUT_CHECKPOINT = 3
LOG = logging.getLogger("migration-watchdog")


def database_path():
    return Path(os.environ.get("FREEBOX_SECRET_DIR", str(Path.home() / "partage-freebox-migration"))) / "migration.sqlite3"


def stalled_jobs(path, *, now=None, threshold_seconds=DEFAULT_STALL_SECONDS):
    """Return running jobs without a durable update for the threshold.

    Prefer transfer_activity: actual Freebox bytes received (including
    HTTP 200 fallback replay) and worker phase changes. A durable S3 checkpoint
    can remain unchanged for hours during a legitimate replay.
    Idle, queued, paused, done, and errored jobs never trigger a restart.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    # mode=ro ensures this independent monitor cannot create or alter the DB.
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=3) as con:
        rows = con.execute("""
            SELECT j.id, j.transferred,
                   COALESCE(a.updated_at, j.updated_at),
                   COALESCE(a.phase, 'unknown'),
                   COALESCE(a.source_bytes, 0)
            FROM jobs j LEFT JOIN transfer_activity a ON a.job_id=j.id
            WHERE j.state='running'
        """).fetchall()
    result = []
    for jid, transferred, updated_at, phase, source_bytes in rows:
        updated = dt.datetime.fromisoformat(updated_at)
        if updated.tzinfo is None:
            raise ValueError("Unexpected naive jobs.updated_at timestamp")
        elapsed = (now - updated).total_seconds()
        if elapsed >= threshold_seconds:
            result.append((jid, transferred, int(elapsed), phase, source_bytes))
    return result

def restart_state_path():
    return database_path().with_name("watchdog-restarts.json")


def restart_history(path, job_id, checkpoint):
    """Count restarts against one job/checkpoint; never loop indefinitely."""
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        state = {}
    if state.get("job_id") != job_id or state.get("checkpoint") != checkpoint:
        return 0
    return max(0, min(100, int(state.get("count", 0))))


def record_restart(path, job_id, checkpoint, count):
    """Atomic, private bookkeeping before attempting a process restart."""
    temp = path.with_name(path.name + ".tmp")
    try:
        fd = os.open(temp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump({"job_id": job_id, "checkpoint": checkpoint,
                       "count": count}, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp, path)
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


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

    jid, transferred, elapsed, phase, source_bytes = stale[0]
    LOG.warning("No worker activity for %ss (job=%s, phase=%s, source_bytes=%s, S3 checkpoint=%s)",
                elapsed, jid, phase, source_bytes, transferred)
    try:
        prior = restart_history(restart_state_path(), jid, transferred)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        LOG.error("Cannot read restart history; refusing restart: %s", exc)
        return 1
    if prior >= MAX_RESTARTS_WITHOUT_CHECKPOINT:
        LOG.error("Stop: job %s stalled after %s restarts with no new S3 checkpoint. "
                  "Investigate Freebox HTTP Range, source read, and S3 logs; manual action required.",
                  jid, prior)
        return 0
    if args.dry_run:
        LOG.info("Dry run: would restart service (attempt %s/%s)", prior + 1,
                 MAX_RESTARTS_WITHOUT_CHECKPOINT)
        return 0
    try:
        record_restart(restart_state_path(), jid, transferred, prior + 1)
    except OSError as exc:
        LOG.error("Cannot persist restart history; refusing restart: %s", exc)
        return 1
    LOG.warning("Restarting migration service (attempt %s/%s for same checkpoint)",
                prior + 1, MAX_RESTARTS_WITHOUT_CHECKPOINT)
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
