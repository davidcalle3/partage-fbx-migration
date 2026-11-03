"""Regression tests for resumable source downloads and true activity tracking."""
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import engine


class HTTPBody(io.BytesIO):
    def __init__(self, data, status, headers):
        super().__init__(data)
        self.status = status
        self.headers = headers
        self.read1_calls = 0

    def read1(self, count):
        self.read1_calls += 1
        # Simulate a stream returning less than the requested 1 MiB.
        return super().read(min(count, 37))


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.worker = engine.Worker.__new__(engine.Worker)
        self.worker.freebox = mock.Mock()
        self.job = {"id": "job1", "source": "encoded", "size": 200}

    def test_range_ignored_replays_and_reports_real_bytes(self):
        data = bytes(range(200))
        body = HTTPBody(data, 200, {"Content-Length": "200", "Content-Type": "application/octet-stream"})
        self.worker.freebox.download.return_value = body
        progress = []
        src = self.worker._open_source(self.job, 150, on_skip=progress.append)
        self.assertEqual(progress[-1], 150)
        self.assertEqual(src.read(), data[150:])
        self.assertGreater(body.read1_calls, 1)

    def test_range_honored_does_not_replay(self):
        data = bytes(range(150, 200))
        body = HTTPBody(data, 206, {"Content-Range": "bytes 150-199/200", "Content-Type": "application/octet-stream"})
        self.worker.freebox.download.return_value = body
        progress = []
        src = self.worker._open_source(self.job, 150, on_skip=progress.append)
        self.assertEqual(progress, [])
        self.assertEqual(src.read(), data)

    def test_exact_read_reports_short_reads(self):
        body = HTTPBody(bytes(range(200)), 200, {})
        sizes = []
        result = self.worker._read_exact(body, 200, sizes.append)
        self.assertEqual(result, bytes(range(200)))
        self.assertEqual(sum(sizes), 200)
        self.assertGreater(len(sizes), 1)


class ActivityStateTests(unittest.TestCase):
    def test_activity_does_not_falsify_s3_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(engine, "ROOT", Path(tmp)):
                state = engine.State()
                state.queue([{"id": "job", "stored_name": "stored",
                              "source": "encoded", "name": "movie.mkv",
                              "size": 10000000, "modified": None, "folder_id": "root"}])
                state.next_job()
                before = state.job("job")
                state.activity("job", "replaying", 8388608)
                after = state.job("job")
                self.assertEqual(after["transferred"], 0)
                self.assertEqual(after["updated_at"], before["updated_at"])
                with sqlite3.connect(state.path) as con:
                    self.assertEqual(con.execute(
                        "SELECT phase,source_bytes FROM transfer_activity WHERE job_id='job'"
                    ).fetchone(), ("replaying", 8388608))


if __name__ == "__main__":
    unittest.main()
