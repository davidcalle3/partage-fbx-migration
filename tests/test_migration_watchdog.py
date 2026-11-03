"""No network access or systemd required for watchdog tests."""
import datetime as dt
import importlib.util
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

PATH = Path(__file__).resolve().parents[1] / "deploy" / "migration_watchdog.py"
spec = importlib.util.spec_from_file_location("migration_watchdog", PATH)
watchdog = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watchdog)


class WatchdogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "migration.sqlite3"
        with sqlite3.connect(self.db) as con:
            con.execute("CREATE TABLE jobs (id TEXT, state TEXT, transferred INT, updated_at TEXT)")
            con.execute("CREATE TABLE transfer_activity (job_id TEXT PRIMARY KEY, phase TEXT, source_bytes INT, updated_at TEXT)")
        self.now = dt.datetime(2026, 11, 3, 9, 0, tzinfo=dt.timezone.utc)

    def insert(self, state, minutes_ago=60, transferred=0):
        with sqlite3.connect(self.db) as con:
            con.execute("INSERT INTO jobs VALUES (?, ?, ?, ?)",
                        (state, state, transferred,
                         (self.now - dt.timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")))

    def check(self):
        return watchdog.stalled_jobs(self.db, now=self.now)

    def test_running_job_stalled(self):
        self.insert("running", minutes_ago=11, transferred=8388608)
        self.assertEqual(self.check(), [("running", 8388608, 660, "unknown", 0)])

    def test_running_job_recently_progressed(self):
        self.insert("running", minutes_ago=9)
        self.assertEqual(self.check(), [])

    def test_exact_threshold(self):
        self.insert("running", minutes_ago=10)
        self.assertEqual(len(self.check()), 1)

    def test_recent_replay_prevents_false_stall(self):
        self.insert("running", minutes_ago=60, transferred=8388608)
        with sqlite3.connect(self.db) as con:
            con.execute("INSERT INTO transfer_activity VALUES (?,?,?,?)", (
                "running", "replaying", 6 * 1024 * 1024,
                (self.now - dt.timedelta(minutes=1)).isoformat(timespec="seconds")))
        self.assertEqual(self.check(), [])

    def test_stale_replay_reports_phase_and_bytes(self):
        self.insert("running", minutes_ago=60, transferred=8388608)
        with sqlite3.connect(self.db) as con:
            con.execute("INSERT INTO transfer_activity VALUES (?,?,?,?)", (
                "running", "replaying", 6 * 1024 * 1024,
                (self.now - dt.timedelta(minutes=11)).isoformat(timespec="seconds")))
        self.assertEqual(self.check(), [
            ("running", 8388608, 660, "replaying", 6 * 1024 * 1024)])

    def test_restart_counter_resets_after_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "watchdog-restarts.json"
            self.assertEqual(watchdog.restart_history(path, "job", 0), 0)
            watchdog.record_restart(path, "job", 0, 1)
            self.assertEqual(watchdog.restart_history(path, "job", 0), 1)
            self.assertEqual(watchdog.restart_history(path, "job", 8388608), 0)
            self.assertEqual(watchdog.restart_history(path, "other", 0), 0)

    def test_no_restart_for_nonrunning_jobs(self):
        for state in ("queued", "paused", "done", "error"):
            self.insert(state)
        self.assertEqual(self.check(), [])

    def test_missing_database_fails_closed(self):
        with self.assertRaises(sqlite3.OperationalError):
            watchdog.stalled_jobs(self.db.parent / "missing.sqlite3", now=self.now)

    @patch.object(watchdog, "stalled_jobs")
    @patch.object(watchdog.subprocess, "run")
    @patch.object(watchdog, "restart_state_path")
    def test_active_stall_restarts(self, statepath, run, stalled):
        statepath.return_value = self.db.parent / "watchdog.json"
        run.return_value.returncode = 0
        stalled.return_value = [("id", 8388608, 660, "reading", 8388608)]
        self.assertEqual(watchdog.main([]), 0)
        self.assertEqual(run.call_args_list[-1].args[0],
                         ["systemctl", "--user", "restart", watchdog.UNIT])
        self.assertEqual(watchdog.restart_history(statepath.return_value, "id", 8388608), 1)
        self.assertEqual(watchdog.main([]), 0)
        self.assertEqual(watchdog.main([]), 0)
        self.assertEqual(watchdog.main([]), 0)
        self.assertEqual(run.call_count, 1 + 3)  # is-active checks + 3 restarts

    @patch.object(watchdog, "stalled_jobs")
    @patch.object(watchdog.subprocess, "run")
    def test_inactive_service_never_restarts(self, run, stalled):
        run.return_value.returncode = 3
        self.assertEqual(watchdog.main([]), 0)
        stalled.assert_not_called()
        self.assertEqual(run.call_count, 1)

    @patch.object(watchdog, "stalled_jobs")
    @patch.object(watchdog.subprocess, "run")
    def test_active_idle_never_restarts(self, run, stalled):
        run.return_value.returncode = 0
        stalled.return_value = []
        self.assertEqual(watchdog.main([]), 0)
        self.assertEqual(run.call_count, 1)

    @patch.object(watchdog, "stalled_jobs")
    @patch.object(watchdog.subprocess, "run")
    @patch.object(watchdog, "restart_state_path")
    def test_dry_run_never_restarts(self, statepath, run, stalled):
        statepath.return_value = self.db.parent / "watchdog.json"
        run.return_value.returncode = 0
        stalled.return_value = [("id", 0, 660, "opening", 0)]
        self.assertEqual(watchdog.main(["--dry-run"]), 0)
        self.assertEqual(run.call_count, 1)


if __name__ == "__main__":
    unittest.main()
