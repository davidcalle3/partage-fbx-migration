"""Durable migration plans, Partage catalog adapter, and streaming S3 worker."""
import contextlib
import datetime
import fcntl
import json
import logging
import mimetypes
import os
import posixpath
import re
import sqlite3
import threading
import time
import unicodedata
import uuid
from pathlib import Path

from botocore.client import Config
import boto3
from dotenv import dotenv_values

from freebox import ROOT, FreeboxError, decode_path, encode_path

LOG = logging.getLogger("migration")
CHUNK = 8 * 1024 * 1024
MAX_FILES = 20000
MAX_DEPTH = 48
MEDIA_EXTENSIONS = {".mp4", ".m4v", ".mov", ".mkv", ".avi", ".webm",
                    ".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def name_ok(value):
    n = unicodedata.normalize("NFC", value if isinstance(value, str) else "")
    if not n or n in (".", "..") or len(n) > 180 or any(ord(c) < 32 or c in "/\\" for c in n):
        raise ValueError("Nom incompatible avec Partage")
    return n


def db(path):
    con = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=30000")
    return con


@contextlib.contextmanager
def transaction(path):
    con = db(path)
    try:
        con.execute("BEGIN IMMEDIATE")
        yield con
        con.commit()
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()


def _same_name(con, folder_id, name):
    needle = name.casefold()
    for table, field in (("folders", "parent_id"), ("files", "folder_id")):
        for row in con.execute("SELECT name FROM " + table + " WHERE " + field + "=?", (folder_id,)):
            if row["name"].casefold() == needle:
                return True
    for row in con.execute("SELECT name FROM uploads WHERE folder_id=? AND state='active'", (folder_id,)):
        if row["name"].casefold() == needle:
            return True
    for row in con.execute("SELECT name FROM remote_imports WHERE folder_id=? AND state NOT IN ('done','cancelled')", (folder_id,)):
        if row["name"].casefold() == needle:
            return True
    return False


class Partage:
    def __init__(self):
        envpath = os.environ.get("PARTAGE_ENV_FILE")
        if not envpath:
            envpath = next((str(p) for p in (Path("/opt/partage/.env"), Path("/opt/partage/app/.env")) if p.is_file()), "")
        if not envpath or not Path(envpath).is_file():
            raise RuntimeError("PARTAGE_ENV_FILE doit pointer vers le fichier .env Partage EXISTANT")
        # Read only; never rewrite, copy, print, or commit the existing .env.
        settings = {**dotenv_values(envpath), **os.environ}
        required = ("S3_ENDPOINT", "S3_BUCKET", "S3_ACCESS_KEY", "S3_SECRET_KEY")
        if not all(settings.get(k) for k in required):
            raise RuntimeError("Configuration S3 incomplète dans le .env existant")
        if not settings["S3_ENDPOINT"].startswith("https://"):
            raise RuntimeError("S3_ENDPOINT doit être HTTPS")
        self.bucket = settings["S3_BUCKET"]
        self.s3 = boto3.client(
            "s3", endpoint_url=settings["S3_ENDPOINT"],
            region_name=settings.get("S3_REGION") or "gra",
            aws_access_key_id=settings["S3_ACCESS_KEY"],
            aws_secret_access_key=settings["S3_SECRET_KEY"],
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"},
                          connect_timeout=7, read_timeout=70,
                          retries={"max_attempts": 3, "mode": "standard"}),
        )
        self.path = Path(os.environ.get("PARTAGE_DB_PATH", "/opt/partage/db/partage.sqlite3"))
        if not self.path.is_file():
            raise RuntimeError("Base Partage inexistante : " + str(self.path))
        with db(self.path) as con:
            cols = {r["name"] for r in con.execute("PRAGMA table_info(files)")}
            if not {"id", "stored_name", "folder_id", "name", "size", "created_at", "storage_backend"} <= cols:
                raise RuntimeError("Schéma Partage incompatible")
            if not con.execute("SELECT 1 FROM folders WHERE id='root'").fetchone():
                raise RuntimeError("Dossier racine Partage absent")

    def destinations(self):
        with db(self.path) as con:
            rows = [dict(row) for row in con.execute(
                "SELECT id,parent_id,name FROM folders ORDER BY name COLLATE NOCASE")]
        return rows

    def prepare(self, manifest, target, existing_jobs=()):
        """Avoid only PROVEN duplicates from this migration, never just matching names.

        An existing Partage file not imported by this tool remains a hard conflict.
        Previous errored or paused transfers must be resumed, not duplicated.
        """
        by_name = {}
        for old in existing_jobs:
            by_name[(old["folder_id"], old["name"].casefold())] = old
        skipped = {"done": 0, "pending": 0}
        dirs = {tuple()}; files = manifest["files"]
        for f in manifest["folders"]:
            pieces = tuple(f)
            for i in range(1, len(pieces)+1):
                dirs.add(pieces[:i])
        for f in files:
            pieces = tuple(f["parts"][:-1])
            for i in range(1, len(pieces)+1):
                dirs.add(pieces[:i])
        if len(dirs) > MAX_FILES:
            raise ValueError("Trop de dossiers")
        with transaction(self.path) as con:
            if not con.execute("SELECT 1 FROM folders WHERE id=?", (target,)).fetchone():
                raise ValueError("Destination Partage supprimée")
            folders = {(): target}
            for parts in sorted(dirs, key=lambda p: (len(p), p)):
                if not parts:
                    continue
                parent = folders[parts[:-1]]
                name = name_ok(parts[-1])
                sub = next((r for r in con.execute("SELECT id,name FROM folders WHERE parent_id=?", (parent,))
                            if r["name"].casefold() == name.casefold()), None)
                if sub:
                    folders[parts] = sub["id"]
                    continue
                if _same_name(con, parent, name):
                    raise ValueError("Conflit dossier Partage : " + name)
                folder_id = uuid.uuid4().hex
                con.execute("INSERT INTO folders (id,parent_id,name,created_at) VALUES (?,?,?,?)",
                            (folder_id, parent, name, now()))
                folders[parts] = folder_id
            mapped = []
            seen = set()
            for f in files:
                parent = folders[tuple(f["parts"][:-1])]
                name = name_ok(f["parts"][-1])
                key = (parent, name.casefold())
                if key in seen:
                    raise ValueError("Deux fichiers ont le même nom dans la sélection : " + name)
                seen.add(key)
                previous = by_name.get(key)
                if previous:
                    same_source = (
                        previous["source"] == f["source"]
                        and int(previous["size"]) == int(f["size"])
                        and previous["modified"] == json.dumps(f["modified"])
                    )
                    if not same_source:
                        raise ValueError("Conflit avec une migration antérieure : " + name)
                    if previous["state"] == "done":
                        published=con.execute("""SELECT id,folder_id,name,stored_name,size,storage_backend
                            FROM files WHERE id=?""",(previous["id"],)).fetchone()
                        if not published or published["stored_name"] != previous["stored_name"] or published["folder_id"] != parent or published["name"].casefold() != name.casefold() or int(published["size"]) != int(f["size"]) or published["storage_backend"] != "s3":
                            raise ValueError("Un ancien fichier migré est introuvable dans Partage : " + name)
                        skipped["done"] += 1
                        continue
                    skipped["pending"] += 1
                    continue
                if _same_name(con, parent, name):
                    raise ValueError("Un autre fichier occupe déjà ce nom dans Partage : " + name)
                mapped.append({
                    "id": uuid.uuid4().hex, "stored_name": uuid.uuid4().hex,
                    "source": f["source"], "size": f["size"], "modified": f["modified"],
                    "folder_id": parent, "name": name,
                })
        return mapped, skipped

    def key(self, job):
        return "media/" + job["stored_name"]

    def head_size(self, job):
        from botocore.exceptions import ClientError
        try:
            head = self.s3.head_object(Bucket=self.bucket, Key=self.key(job))
            return int(head["ContentLength"])
        except ClientError as exc:
            if exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 404:
                return None
            raise

    def publish(self, job):
        """Only publish a fully verified S3 object into the existing Partage catalog."""
        if self.head_size(job) != job["size"]:
            raise ValueError("Vérification de la taille S3 impossible")
        with transaction(self.path) as con:
            existing = con.execute("SELECT id,stored_name FROM files WHERE id=?", (job["id"],)).fetchone()
            if existing:
                if existing["stored_name"] != job["stored_name"]:
                    raise ValueError("Conflit d'identifiant Partage")
                return
            if not con.execute("SELECT 1 FROM folders WHERE id=?", (job["folder_id"],)).fetchone():
                raise ValueError("Le dossier de destination a été supprimé")
            if _same_name(con, job["folder_id"], job["name"]):
                raise ValueError("Conflit de nom survenu pendant le transfert")
            con.execute(
                """INSERT INTO files (id,folder_id,name,stored_name,size,created_at,storage_backend)
                   VALUES (?,?,?,?,?,?,'s3')""",
                (job["id"], job["folder_id"], job["name"], job["stored_name"], job["size"], now()),
            )
            if Path(job["name"]).suffix.lower() in MEDIA_EXTENSIONS:
                con.execute(
                    """INSERT INTO metadata_jobs
                       (id,batch_id,file_id,source,state,created_at)
                       VALUES (?,?,?,'auto','queued',?)""",
                    (uuid.uuid4().hex, uuid.uuid4().hex, job["id"], now()),
                )


class State:
    def __init__(self):
        ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(ROOT, 0o700)
        self.path = ROOT / "migration.sqlite3"
        with db(self.path) as con:
            con.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS plans (
                    id TEXT PRIMARY KEY, manifest TEXT NOT NULL, target TEXT NOT NULL,
                    created REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, stored_name TEXT UNIQUE NOT NULL,
                    source TEXT NOT NULL, name TEXT NOT NULL, size INTEGER NOT NULL,
                    modified TEXT, folder_id TEXT NOT NULL, state TEXT NOT NULL,
                    transferred INTEGER NOT NULL DEFAULT 0, upload_id TEXT,
                    error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS parts (
                    job_id TEXT NOT NULL, number INTEGER NOT NULL,
                    etag TEXT NOT NULL, size INTEGER NOT NULL,
                    PRIMARY KEY(job_id,number)
                );
                CREATE TABLE IF NOT EXISTS browser_selection (
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    selection_json TEXT NOT NULL, destination TEXT NOT NULL
                );
            """)
            columns = {r[1] for r in con.execute("PRAGMA table_info(jobs)")}
            if "attempts" not in columns:
                con.execute("ALTER TABLE jobs ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0")
            if "retry_at" not in columns:
                con.execute("ALTER TABLE jobs ADD COLUMN retry_at REAL NOT NULL DEFAULT 0")
            con.commit()
        os.chmod(self.path, 0o600)

    def selection(self):
        with db(self.path) as con:
            row = con.execute("SELECT selection_json,destination FROM browser_selection WHERE id=1").fetchone()
        return ({"items":json.loads(row["selection_json"]), "target":row["destination"]}
                if row else {"items":[], "target":"root"})

    def save_selection(self, items, target):
        if not isinstance(items,list) or len(items)>200 or not isinstance(target,str):
            raise ValueError("Sélection invalide")
        clean=[]
        for entry in items:
            if not isinstance(entry,dict) or not all(k in entry for k in ("path","name","type")):
                raise ValueError("Élément de sélection invalide")
            if entry["type"] not in ("file","dir") or not name_ok(entry["name"]):
                raise ValueError("Élément de sélection invalide")
            clean.append({k:entry.get(k) for k in ("path","name","type","size")})
        with transaction(self.path) as con:
            con.execute("""INSERT INTO browser_selection(id,selection_json,destination)
                VALUES(1,?,?) ON CONFLICT(id) DO UPDATE SET
                selection_json=excluded.selection_json,destination=excluded.destination""",
                (json.dumps(clean,ensure_ascii=False),target))

    def plan(self, manifest, target):
        pid = uuid.uuid4().hex
        with transaction(self.path) as con:
            con.execute("DELETE FROM plans WHERE created < ?", (time.time()-24*3600,))
            con.execute("INSERT INTO plans VALUES (?,?,?,?)",
                        (pid, json.dumps(manifest, ensure_ascii=False), target, time.time()))
        return pid

    def take_plan(self, pid):
        with transaction(self.path) as con:
            row = con.execute("SELECT * FROM plans WHERE id=? AND created>?",
                              (pid, time.time()-24*3600)).fetchone()
            if not row:
                raise ValueError("Prévisualisation expirée ; recommence")
            con.execute("DELETE FROM plans WHERE id=?", (pid,))
            return json.loads(row["manifest"]), row["target"]

    def known(self):
        with db(self.path) as con:
            return [dict(row) for row in con.execute("""SELECT id,stored_name,source,name,size,modified,folder_id,state FROM jobs""")]

    def queue(self, jobs):
        with transaction(self.path) as con:
            for j in jobs:
                for old in con.execute("SELECT name FROM jobs WHERE folder_id=?", (j["folder_id"],)):
                    if old["name"].casefold() == j["name"].casefold():
                        raise ValueError("Ce fichier est déjà dans la file de migration : " + j["name"])
                con.execute("""INSERT INTO jobs
                    (id,stored_name,source,name,size,modified,folder_id,state,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,'queued',?,?)""",
                    (j["id"],j["stored_name"],j["source"],j["name"],j["size"],
                     json.dumps(j["modified"]),j["folder_id"],now(),now()))

    def jobs(self):
        with db(self.path) as con:
            return [dict(r) for r in con.execute(
                """SELECT id,name,size,transferred,state,error,updated_at
                   ,retry_at,attempts FROM jobs ORDER BY created_at DESC,id DESC LIMIT 500""")]

    def stats(self):
        with db(self.path) as con:
            row = con.execute("""SELECT
                COUNT(*) AS count,
                COALESCE(SUM(size),0) AS bytes,
                COALESCE(SUM(MIN(transferred,size)),0) AS transferred,
                COALESCE(SUM(CASE WHEN state='done' THEN 1 ELSE 0 END),0) AS done,
                COALESCE(SUM(CASE WHEN state='queued' THEN 1 ELSE 0 END),0) AS queued,
                COALESCE(SUM(CASE WHEN state='running' THEN 1 ELSE 0 END),0) AS running,
                COALESCE(SUM(CASE WHEN state='error' THEN 1 ELSE 0 END),0) AS error
                FROM jobs""").fetchone()
            return dict(row)

    def next_job(self):
        with transaction(self.path) as con:
            row = con.execute("""SELECT * FROM jobs WHERE state='queued'
                AND retry_at<=? ORDER BY created_at,id LIMIT 1""",(time.time(),)).fetchone()
            if row:
                con.execute("UPDATE jobs SET state='running',error=NULL,updated_at=? WHERE id=?",
                            (now(),row["id"]))
            return dict(row) if row else None

    def job(self, jid):
        with db(self.path) as con:
            row = con.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
            return dict(row) if row else None

    def set(self, jid, **changes):
        if not changes or set(changes) - {"state","transferred","upload_id","error"}:
            raise ValueError("Modification de transfert invalide")
        changes["updated_at"] = now()
        with transaction(self.path) as con:
            con.execute("UPDATE jobs SET " + ",".join(k+"=?" for k in changes) + " WHERE id=?",
                        (*changes.values(), jid))

    def parts(self, jid):
        with db(self.path) as con:
            return [dict(row) for row in con.execute(
                "SELECT number,etag,size FROM parts WHERE job_id=? ORDER BY number",(jid,))]

    def part(self, jid, number, etag, size):
        with transaction(self.path) as con:
            con.execute("INSERT OR REPLACE INTO parts VALUES (?,?,?,?)", (jid, number, etag, size))
            transferred = con.execute("SELECT COALESCE(SUM(size),0) FROM parts WHERE job_id=?",(jid,)).fetchone()[0]
            con.execute("UPDATE jobs SET transferred=?,updated_at=? WHERE id=?",(transferred,now(),jid))

    def retry_later(self, jid, message):
        with transaction(self.path) as con:
            row=con.execute("SELECT attempts,state FROM jobs WHERE id=?",(jid,)).fetchone()
            if not row or row["state"]!="running":
                return
            attempts=int(row["attempts"])+1
            delay=min(300,30*2**min(attempts-1,4))
            con.execute("""UPDATE jobs SET state='queued',attempts=?,retry_at=?,
                error=?,updated_at=? WHERE id=? AND state='running'""",
                (attempts,time.time()+delay,message[:180],now(),jid))

    def recover(self):
        with transaction(self.path) as con:
            con.execute("UPDATE jobs SET state='queued',updated_at=? WHERE state='running'", (now(),))

    def action(self, jid, action):
        with transaction(self.path) as con:
            row = con.execute("SELECT state FROM jobs WHERE id=?", (jid,)).fetchone()
            if not row:
                raise ValueError("Transfert inconnu")
            status = row["state"]
            if action == "retry" and status == "error":
                state = "queued"
            elif action == "pause" and status in ("queued","running"):
                state = "paused"
            elif action == "resume" and status == "paused":
                state = "queued"
            else:
                raise ValueError("Action impossible dans cet état")
            con.execute("""UPDATE jobs SET state=?,retry_at=0,
                            attempts=CASE WHEN ?='retry' THEN 0 ELSE attempts END,
                            error=NULL,updated_at=? WHERE id=?""",
                        (state,action,now(),jid))


def manifest_from_selection(freebox, paths):
    if not isinstance(paths, list) or not paths or len(paths) > 200:
        raise ValueError("Sélection vide ou trop importante")
    selected = []
    for path in paths:
        decoded = freebox.allowed(path)
        roots = [p["path"] for p in freebox.partitions()]
        if path in roots:
            selected.append({"path":path,"name":posixpath.basename(decoded),"type":"dir"})
        else:
            parent = encode_path(posixpath.dirname(decoded))
            entry = next((x for x in freebox.list_dir(parent) if x["path"] == path), None)
            if not entry:
                raise ValueError("Élément Freebox introuvable")
            selected.append(entry)
    # If a parent was selected, disregard redundant descendants.
    selected.sort(key=lambda e: len(decode_path(e["path"])))
    clean = []
    for e in selected:
        path = decode_path(e["path"])
        if not any(path == decode_path(s["path"]) or path.startswith(decode_path(s["path"])+"/") for s in clean):
            clean.append(e)
    manifest = {"folders": [], "files": [], "bytes": 0}
    seen_dirs = set()
    seen_files = set()

    def descend(entry, parts, depth):
        if depth > MAX_DEPTH or len(manifest["files"]) > MAX_FILES:
            raise ValueError("Arborescence Freebox trop volumineuse")
        if entry["type"] == "dir":
            if tuple(parts) in seen_dirs:
                return
            seen_dirs.add(tuple(parts))
            manifest["folders"].append(parts)
            for child in freebox.list_dir(entry["path"]):
                descend(child, parts+[name_ok(child["name"])], depth+1)
        else:
            if tuple(parts) in seen_files:
                raise ValueError("Deux fichiers aboutissent au même chemin")
            seen_files.add(tuple(parts))
            size = int(entry["size"])
            if size < 0:
                raise ValueError("Taille Freebox invalide")
            manifest["bytes"] += size
            manifest["files"].append({
                "parts":parts, "source":entry["path"], "size":size,
                "modified":entry.get("modified"),
            })

    for entry in clean:
        descend(entry, [name_ok(entry["name"])], 0)
    if not manifest["files"] and not manifest["folders"]:
        raise ValueError("Aucun élément à migrer")
    if len(manifest["files"]) > MAX_FILES:
        raise ValueError("Trop de fichiers")
    return manifest


class Worker:
    """One local worker. S3 multipart checkpoints survive app and VPS restarts."""
    def __init__(self, freebox, partage, state):
        self.freebox, self.partage, self.state = freebox, partage, state
        self._thread = None
        self._lock_file = None
        self.stop = threading.Event()

    def start(self):
        fd = os.open(str(ROOT / ".worker.lock"), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise RuntimeError("Un autre processus de migration est déjà actif")
        self._lock_file = fd
        self.state.recover()
        self._thread = threading.Thread(target=self.run, name="migration-worker", daemon=True)
        self._thread.start()

    def shutdown(self):
        self.stop.set()
        if self._thread:
            self._thread.join(timeout=90)
        if self._lock_file is not None:
            fcntl.flock(self._lock_file, fcntl.LOCK_UN)
            os.close(self._lock_file)
            self._lock_file = None

    @staticmethod
    def _skip(body, count):
        while count:
            block = body.read(min(count, 1024*1024))
            if not block:
                raise ValueError("Flux Freebox interrompu avant la reprise")
            count -= len(block)

    def _open_source(self, job, offset):
        source = self.freebox.download(job["source"], offset)
        status = source.status
        content_type = source.headers.get("Content-Type", "").split(";")[0].lower()
        if content_type in ("application/json", "text/html"):
            source.close()
            raise FreeboxError("Téléchargement Freebox renvoie une page, pas des octets")
        if status == 206:
            m = re.match(r"bytes (\d+)-(\d+)/(\d+)", source.headers.get("Content-Range",""))
            if not m or int(m.group(1)) != offset or int(m.group(3)) != job["size"]:
                source.close()
                raise FreeboxError("Réponse Range Freebox incorrecte")
        elif status == 200:
            length = source.headers.get("Content-Length")
            if length and int(length) != job["size"]:
                source.close()
                raise FreeboxError("Longueur Freebox inattendue")
            if offset:
                self._skip(source, offset)  # Freebox that ignores Range: one sequential redownload
        else:
            source.close()
            raise FreeboxError("Statut téléchargement Freebox inattendu")
        return source

    @staticmethod
    def _read_exact(source, size):
        result = bytearray()
        while len(result) < size:
            chunk = source.read(min(1024*1024, size-len(result)))
            if not chunk:
                raise ValueError("Fichier source interrompu avant la fin")
            result.extend(chunk)
        return bytes(result)

    def transfer(self, job):
        key = self.partage.key(job)
        s3 = self.partage.s3
        bucket = self.partage.bucket
        # Crash after multipart completion but before SQLite publication:
        # a completed object remains publishable even if Freebox has been retired.
        existing_size = self.partage.head_size(job)
        if existing_size == job["size"]:
            self.partage.publish(job)
            return
        if existing_size is not None:
            raise ValueError("Objet S3 inattendu : copie non réécrite")
        self.freebox.verify_file(job["source"], job["size"], json.loads(job["modified"]))
        if job["size"] == 0:
            s3.put_object(Bucket=bucket, Key=key, Body=b"")
            self.partage.publish(job)
            return
        if job["size"] <= CHUNK:
            with self._open_source(job, 0) as src:
                payload = self._read_exact(src, job["size"])
                if src.read(1):
                    raise ValueError("Données source plus grandes que prévu")
            s3.put_object(Bucket=bucket, Key=key, Body=payload)
            self.partage.publish(job)
            return

        upload_id = job["upload_id"]
        if not upload_id:
            result = s3.create_multipart_upload(Bucket=bucket, Key=key,
                ContentType=mimetypes.guess_type(job["name"])[0] or "application/octet-stream")
            upload_id = result["UploadId"]
            self.state.set(job["id"], upload_id=upload_id)
        remote = s3.list_parts(Bucket=bucket, Key=key, UploadId=upload_id)
        parts = list(remote.get("Parts", []))
        marker = remote.get("NextPartNumberMarker")
        while remote.get("IsTruncated"):
            remote = s3.list_parts(Bucket=bucket, Key=key, UploadId=upload_id, PartNumberMarker=marker)
            parts.extend(remote.get("Parts", []))
            marker = remote.get("NextPartNumberMarker")
        offset = 0
        ordered = []
        for number, p in enumerate(parts, 1):
            size = int(p["Size"])
            if p["PartNumber"] != number or size != min(CHUNK, job["size"]-offset):
                raise ValueError("Parties S3 incohérentes ; reprise interrompue")
            ordered.append({"PartNumber":number, "ETag":p["ETag"]})
            self.state.part(job["id"],number,p["ETag"],size)
            offset += size
        if offset < job["size"]:
            with self._open_source(job, offset) as src:
                while offset < job["size"]:
                    if self.stop.is_set() or self.state.job(job["id"])["state"] == "paused":
                        return
                    count = min(CHUNK, job["size"]-offset)
                    payload = self._read_exact(src, count)
                    number = len(ordered)+1
                    etag = s3.upload_part(Bucket=bucket, Key=key, UploadId=upload_id,
                                          PartNumber=number, Body=payload,
                                          ContentLength=count)["ETag"]
                    ordered.append({"PartNumber":number, "ETag":etag})
                    self.state.part(job["id"], number, etag, count)
                    offset += count
                if src.read(1):
                    raise ValueError("Données source supplémentaires")
        if self.stop.is_set() or self.state.job(job["id"])["state"] == "paused":
            return
        self.freebox.verify_file(job["source"], job["size"], json.loads(job["modified"]))
        s3.complete_multipart_upload(Bucket=bucket, Key=key, UploadId=upload_id,
                                     MultipartUpload={"Parts":ordered})
        self.partage.publish(job)

    def run(self):
        while not self.stop.is_set():
            job = self.state.next_job()
            if not job:
                self.stop.wait(1)
                continue
            try:
                self.transfer(job)
                current = self.state.job(job["id"])
                if current["state"] == "running":
                    self.state.set(job["id"], state="done", transferred=job["size"])
            except Exception as exc:
                # Do not include URLs, secrets, Freebox tokens or S3 request details in UI.
                LOG.exception("Transfer failed for job %s", job["id"])
                if self.state.job(job["id"])["state"] == "running":
                    safe = str(exc) if isinstance(exc,(ValueError,FreeboxError)) else "Erreur réseau ou S3 ; réessayer"
                    self.state.set(job["id"], state="error", error=safe[:180])
