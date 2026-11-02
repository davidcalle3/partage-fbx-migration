import base64
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from freebox import decode_path, encode_path
from engine import Partage, State, Worker, manifest_from_selection


class FakeFreebox:
    def __init__(self):
        self.root = encode_path("/Disque dur")
        self.photos = encode_path("/Disque dur/Photos")
        self.photo = encode_path("/Disque dur/Photos/image.jpg")
        self.vid = encode_path("/Disque dur/Video.mp4")
        self.contents = {
            self.root: [
                {"path":self.photos, "name":"Photos", "type":"dir", "size":0, "modified":1},
                {"path":self.vid, "name":"Video.mp4", "type":"file", "size":123, "modified":2},
            ],
            self.photos: [
                {"path":self.photo,"name":"image.jpg","type":"file","size":12,"modified":3}
            ]
        }

    def partitions(self):
        return [{"path":self.root}]

    def allowed(self, path):
        value = decode_path(path)
        if not value.startswith("/Disque dur"):
            raise ValueError("outside")
        return value

    def list_dir(self, path):
        return self.contents[path]


class Paths(unittest.TestCase):
    def test_codec(self):
        path = "/Disque dur/Photos/Été 2026.jpg"
        self.assertEqual(decode_path(encode_path(path)), path)

    def test_rejects_noncanonical(self):
        with self.assertRaises(ValueError):
            decode_path(encode_path("/Disque dur/../private"))
        with self.assertRaises(ValueError):
            decode_path("%%%")


class Plans(unittest.TestCase):
    def test_recursive_selection(self):
        box = FakeFreebox()
        plan = manifest_from_selection(box,[box.photos,box.vid])
        self.assertEqual(plan["bytes"],135)
        self.assertEqual(len(plan["files"]),2)
        self.assertEqual(plan["files"][0]["parts"],["Photos","image.jpg"])

    def test_parent_selection_deduplicates(self):
        box = FakeFreebox()
        plan = manifest_from_selection(box,[box.root,box.photo])
        self.assertEqual(len(plan["files"]),2)

    def test_unknown_selection_fails(self):
        box = FakeFreebox()
        with self.assertRaises(ValueError):
            manifest_from_selection(box,[encode_path("/Disque dur/absent")])


class Persistence(unittest.TestCase):
    def test_queue_and_restart(self):
        import engine
        with tempfile.TemporaryDirectory() as tmp:
            prev = engine.ROOT
            engine.ROOT = Path(tmp)
            try:
                state = State()
                state.queue([{"id":"job1","stored_name":"key1","source":"base64",
                              "name":"x.txt","size":8,"modified":None,"folder_id":"root"}])
                job = state.next_job()
                self.assertEqual(job["id"],"job1")
                state.part("job1",1,"etag",8)
                state.recover()
                self.assertEqual(state.job("job1")["state"],"queued")
                self.assertEqual(state.job("job1")["transferred"],8)
            finally:
                engine.ROOT = prev

    def test_catalog_publication_and_collision(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"partage.sqlite3"
            con = sqlite3.connect(path)
            con.executescript("""
                CREATE TABLE folders (id TEXT PRIMARY KEY,parent_id TEXT,name TEXT,created_at TEXT);
                CREATE TABLE files (id TEXT PRIMARY KEY,folder_id TEXT,name TEXT,stored_name TEXT,size INTEGER,created_at TEXT,storage_backend TEXT);
                CREATE TABLE uploads (id TEXT,folder_id TEXT,name TEXT,state TEXT);
                CREATE TABLE remote_imports (id TEXT,folder_id TEXT,name TEXT,state TEXT);
                CREATE TABLE metadata_jobs (id TEXT,batch_id TEXT,file_id TEXT,source TEXT,state TEXT,created_at TEXT);
                INSERT INTO folders VALUES ('root',NULL,'Racine','today');
            """)
            con.commit()
            con.close()
            store = Partage.__new__(Partage)
            store.path = path
            store.head_size = lambda job:job["size"]
            job = {"id":"x","name":"video.mp4","stored_name":"abc","size":10,"folder_id":"root"}
            store.publish(job)
            store.publish(job)  # idempotent after crash
            with sqlite3.connect(path) as con:
                self.assertEqual(con.execute("SELECT count(*) FROM files").fetchone()[0],1)
                self.assertEqual(con.execute("SELECT count(*) FROM metadata_jobs").fetchone()[0],1)
            clash = {"id":"y","name":"VIDEO.MP4","stored_name":"def","size":10,"folder_id":"root"}
            with self.assertRaises(ValueError):
                store.publish(clash)


if __name__ == "__main__":
    unittest.main()
