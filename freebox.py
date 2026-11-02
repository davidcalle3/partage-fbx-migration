"""Read-only Freebox OS client. No filesystem write operations are implemented."""
import base64
import hashlib
import hmac
import json
import os
import posixpath
import re
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HOST = os.environ.get("FREEBOX_HOST", "https://b50xzsc1.fbxos.fr:56055").rstrip("/")
APP_ID = "fr.partage.migration"
ROOT = Path(os.environ.get("FREEBOX_SECRET_DIR", str(Path.home() / "partage-freebox-migration")))
EXPECTED_UID = "a1cb81bbd91049f153507557a6cc5d55"


class FreeboxError(RuntimeError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def decode_path(encoded):
    """Freebox filesystem paths are standard Base64 strings, not URLs."""
    if not isinstance(encoded, str) or not encoded or len(encoded) > 8192:
        raise ValueError("Chemin Freebox invalide")
    try:
        path = base64.b64decode(encoded, validate=True).decode("utf-8")
    except (ValueError, UnicodeError) as exc:
        raise ValueError("Chemin Freebox invalide") from exc
    if not path.startswith("/") or "\x00" in path or posixpath.normpath(path) != path:
        raise ValueError("Chemin Freebox non canonique")
    return path


def encode_path(path):
    return base64.b64encode(path.encode("utf-8")).decode("ascii")


def _safe_name(name):
    return (isinstance(name, str) and bool(name) and name not in (".", "..")
            and len(name) <= 180 and all(ord(c) >= 32 and c not in "/\\" for c in name))


class Freebox:
    def __init__(self):
        parsed = urllib.parse.urlsplit(HOST)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.path:
            raise FreeboxError("L'adresse Freebox doit être HTTPS, sans chemin ni identifiants")
        auth_path = ROOT / "authorization.json"
        if auth_path.stat().st_mode & 0o077:
            raise FreeboxError("Protéger authorization.json avec chmod 600")
        payload = json.loads(auth_path.read_text(encoding="utf-8"))
        self._app_token = payload["result"]["app_token"]
        tls = ssl.create_default_context(cafile=str(ROOT / "freebox-ca.pem"))
        # Python 3.14 strict X.509 rejects the legacy Freebox CA's missing AKI.
        # Hostname, root CA, validity, and signature checks remain enabled.
        tls.verify_flags &= ~ssl.VERIFY_X509_STRICT
        self._opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=tls), _NoRedirect())
        self._lock = threading.RLock()
        self._token = None
        version = self._json("/api_version", authenticated=False)
        if version.get("uid") != EXPECTED_UID:
            raise FreeboxError("UID Freebox inattendu ; connexion refusée")
        self.prefix = "/api/v" + version["api_version"].split(".")[0]
        self._login()

    def _open(self, endpoint, token=None, headers=None, timeout=75):
        if not endpoint.startswith("/") or endpoint.startswith("//"):
            raise FreeboxError("Chemin API invalide")
        request_headers = dict(headers or {})
        if token:
            request_headers["X-Fbx-App-Auth"] = token
        req = urllib.request.Request(HOST + endpoint, headers=request_headers, method="GET")
        try:
            return self._opener.open(req, timeout=timeout)
        except urllib.error.HTTPError as exc:
            try:
                data = json.loads(exc.read(4096).decode("utf-8"))
                code = data.get("error_code", "HTTP " + str(exc.code))
            except (ValueError, UnicodeError):
                code = "HTTP " + str(exc.code)
            exc.close()
            raise FreeboxError("Freebox : " + code) from None

    def _json(self, endpoint, body=None, authenticated=True):
        with self._lock:
            headers = {}
            if body is not None:
                headers["Content-Type"] = "application/json"
            if authenticated and not self._token:
                self._login()
            if authenticated:
                headers["X-Fbx-App-Auth"] = self._token
            req = urllib.request.Request(
                HOST + endpoint,
                headers=headers,
                data=json.dumps(body).encode("utf-8") if body is not None else None,
                method="POST" if body is not None else "GET",
            )
            try:
                with self._opener.open(req, timeout=30) as res:
                    reply = json.load(res)
            except urllib.error.HTTPError as exc:
                try:
                    error = json.loads(exc.read(4096)).get("error_code", "HTTP " + str(exc.code))
                except (ValueError, UnicodeError):
                    error = "HTTP " + str(exc.code)
                exc.close()
                if authenticated and error == "auth_required":
                    self._token = None
                    self._login()
                    return self._json(endpoint, body, authenticated)
                raise FreeboxError("Freebox : " + error) from None
            if endpoint == "/api_version":
                return reply
            if not isinstance(reply, dict) or not reply.get("success"):
                raise FreeboxError("Freebox : " + str(reply.get("error_code", "réponse invalide")))
            return reply.get("result")

    def _login(self):
        challenge = self._json(self.prefix + "/login/", authenticated=False)["challenge"]
        password = hmac.new(self._app_token.encode(), challenge.encode(), hashlib.sha1).hexdigest()
        response = self._json(self.prefix + "/login/session/", {
            "app_id": APP_ID, "app_version": "1.0", "password": password
        }, authenticated=False)
        if not response.get("permissions", {}).get("explorer"):
            raise FreeboxError("Permission Explorateur manquante")
        self._token = response["session_token"]

    def partitions(self):
        parts = self._json(self.prefix + "/storage/partition/")
        if not isinstance(parts, list):
            raise FreeboxError("Réponse partitions invalide")
        return [
            {"name": p.get("label") or "Sans nom", "path": p["path"],
             "used": int(p.get("used_bytes") or 0), "total": int(p.get("total_bytes") or 0)}
            for p in parts if p.get("state") == "mounted" and decode_path(p["path"])
        ]

    def allowed(self, encoded):
        path = decode_path(encoded)
        roots = [decode_path(p["path"]) for p in self.partitions()]
        if not any(path == root or path.startswith(root + "/") for root in roots):
            raise ValueError("Chemin hors des disques montés")
        return path

    def list_dir(self, encoded):
        self.allowed(encoded)
        result = self._json(self.prefix + "/fs/ls/" + urllib.parse.quote(encoded, safe=""))
        if isinstance(result, dict):
            result = next((result[k] for k in ("files", "entries", "items", "children", "listing")
                           if isinstance(result.get(k), list)), None)
        if not isinstance(result, list):
            raise FreeboxError("Format de liste Freebox inconnu")
        entries = []
        for item in result:
            if not isinstance(item, dict) or not _safe_name(item.get("name")):
                continue
            if item.get("type") not in ("file", "dir") or item.get("link"):
                continue  # never traverse symbolic links
            try:
                child = decode_path(item.get("path"))
                parent = decode_path(encoded)
                if posixpath.dirname(child) != parent or posixpath.basename(child) != item["name"]:
                    continue
            except (ValueError, TypeError):
                continue
            entries.append({
                "name": item["name"], "path": item["path"], "type": item["type"],
                "size": int(item.get("size") or 0), "modified": item.get("modification"),
            })
        return sorted(entries, key=lambda e: (e["type"] != "dir", e["name"].casefold()))

    def verify_file(self, encoded, expected_size, expected_modified):
        path = self.allowed(encoded)
        parent = encode_path(posixpath.dirname(path))
        matches = [e for e in self.list_dir(parent) if e["path"] == encoded and e["type"] == "file"]
        if len(matches) != 1:
            raise FreeboxError("Fichier source disparu")
        item = matches[0]
        if item["size"] != expected_size or item["modified"] != expected_modified:
            raise FreeboxError("Fichier source modifié depuis la sélection")

    def download(self, encoded, offset=0):
        self.allowed(encoded)
        endpoint = self.prefix + "/fs/download/" + urllib.parse.quote(encoded, safe="")
        for retry in range(2):
            with self._lock:
                if not self._token:
                    self._login()
                token = self._token
            try:
                return self._open(endpoint, token=token,
                                  headers={"Range": "bytes=" + str(offset) + "-"} if offset else None)
            except FreeboxError as exc:
                if retry == 0 and "auth_required" in str(exc):
                    with self._lock:
                        self._token = None
                    continue
                raise
        raise FreeboxError("Connexion Freebox impossible")
