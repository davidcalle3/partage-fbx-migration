"""Crash, retry, selection persistence and duplicate-handling regression tests."""
import json
import sqlite3
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import engine


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        patch = mock.patch.object(engine, "ROOT", root)
        patch.start()
        self.addCleanup(patch.stop)
        self.state = engine.State()
        self.catalog = root / "partage.sqlite3"
        with sqlite3.connect(self.catalog) as con:
            con.executescript("""
                CREATE TABLE folders(id TEXT PRIMARY KEY,parent_id TEXT,name TEXT,created_at TEXT);
                CREATE TABLE files(id TEXT PRIMARY KEY,folder_id TEXT,name TEXT,
                    stored_name TEXT,size INTEGER,created_at TEXT,storage_backend TEXT);
                CREATE TABLE uploads(id TEXT,folder_id TEXT,name TEXT,state TEXT);
                CREATE TABLE remote_imports(id TEXT,folder_id TEXT,name TEXT,state TEXT);
                CREATE TABLE metadata_jobs(id TEXT,batch_id TEXT,file_id TEXT,
                    source TEXT,state TEXT,created_at TEXT);
                INSERT INTO folders VALUES ('root',NULL,'Root','now');
            """)
        self.partage = engine.Partage.__new__(engine.Partage)
        self.partage.path = self.catalog

    def sample(self, jobid="job1", folder_id="root", name="photo.jpg"):
        return {"id":jobid,"stored_name":"object-"+jobid,
                "source":"c291cmNlLXBhdGg=", "name":name,
                "size":42,"modified":7,"folder_id":folder_id}

    def manifest(self, name="photo.jpg"):
        return {"files":[{"parts":[name],"source":"c291cmNlLXBhdGg=",
                          "size":42,"modified":7}], "folders":[], "bytes":42}

    def test_selection_survives_restart(self):
        items=[{"path":"L0ZCWERTSy9QaG90b3M=","name":"Photos",
                "type":"dir","size":0}]
        self.state.save_selection(items,"root")
        self.assertEqual(engine.State().selection(),{"items":items,"target":"root"})

    def test_transient_failure_gets_bounded_exponential_retry(self):
        self.state.queue([self.sample()])
        self.assertEqual(self.state.next_job()["id"],"job1")
        self.state.retry_later("job1","Connexion interrompue")
        current=self.state.job("job1")
        self.assertEqual(current["state"],"queued")
        self.assertEqual(current["attempts"],1)
        self.assertGreater(current["retry_at"],time.time()+20)
        self.assertIsNone(self.state.next_job())  # not retried immediately
        with engine.transaction(self.state.path) as con:
            con.execute("UPDATE jobs SET retry_at=0 WHERE id='job1'")
        self.assertEqual(self.state.next_job()["id"],"job1")
        self.state.retry_later("job1","Connexion interrompue")
        self.assertEqual(self.state.job("job1")["attempts"],2)

    def test_non_network_validation_is_not_retried(self):
        self.assertTrue(engine.transient_network_error(urllib.error.URLError("timeout")))
        self.assertTrue(engine.transient_network_error(TimeoutError("timeout")))
        self.assertFalse(engine.transient_network_error(ValueError("Wrong file length")))
        self.assertFalse(engine.transient_network_error(engine.FreeboxError("invalid_request")))

    def test_confirmed_migration_is_skipped_without_duplicate(self):
        previous=self.sample()
        self.state.queue([previous])
        with engine.transaction(self.state.path) as con:
            con.execute("UPDATE jobs SET state='done' WHERE id='job1'")
        with sqlite3.connect(self.catalog) as con:
            con.execute("""INSERT INTO files VALUES
                ('job1','root','photo.jpg','object-job1',42,'today','s3')""")
        new,skipped=self.partage.prepare(self.manifest(),"root",self.state.known())
        self.assertEqual(new,[])
        self.assertEqual(skipped,{"done":1,"pending":0})

    def test_pending_migration_is_not_duplicated(self):
        self.state.queue([self.sample()])
        new,skipped=self.partage.prepare(self.manifest(),"root",self.state.known())
        self.assertEqual(new,[])
        self.assertEqual(skipped,{"done":0,"pending":1})

    def test_external_file_same_name_is_not_silently_skipped(self):
        with sqlite3.connect(self.catalog) as con:
            con.execute("""INSERT INTO files VALUES
                ('other','root','photo.jpg','some-other-object',42,'today','s3')""")
        with self.assertRaisesRegex(ValueError,"Un autre fichier"):
            self.partage.prepare(self.manifest(),"root",self.state.known())

    def test_modified_source_is_conflict_not_false_duplicate(self):
        self.state.queue([self.sample()])
        manifest=self.manifest()
        manifest["files"][0]["size"]=43
        with self.assertRaisesRegex(ValueError,"Conflit"):
            self.partage.prepare(manifest,"root",self.state.known())


if __name__ == "__main__":
    unittest.main()
