"""Dedicated HTTP Basic credentials for HTTPS reverse-proxy access.

Secrets live only in the VPS private Freebox directory; no migrations are
accessible without successful authentication.
"""
import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import threading
from pathlib import Path

from freebox import ROOT

ITERATIONS = 420_000
CREDENTIAL_FILE = ROOT / "http-auth.json"


def generate(path=CREDENTIAL_FILE):
    """Only invoked by the operator, never on web server startup."""
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError("Credentials exist; refusing to replace them")
    username = "migration"
    password = secrets.token_urlsafe(32)
    salt = secrets.token_bytes(24)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)
    body = {"username": username, "salt": salt.hex(),
            "digest": digest.hex(), "iterations": ITERATIONS}
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(body, file)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return username, password


class BasicAuth:
    def __init__(self, path=CREDENTIAL_FILE):
        path = Path(path)
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise RuntimeError("chmod 600 required for http-auth.json")
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.username = payload["username"]
        self.salt = bytes.fromhex(payload["salt"])
        self.digest = bytes.fromhex(payload["digest"])
        self.iterations = int(payload["iterations"])
        if (not re.fullmatch(r"[a-zA-Z0-9_-]{1,48}", self.username)
                or len(self.salt) < 16 or len(self.digest) != 32
                or self.iterations < 250_000 or self.iterations > 1_000_000):
            raise RuntimeError("Invalid migration HTTP auth configuration")
        self._pepper = secrets.token_bytes(32)
        self._cached = None
        self._lock = threading.Lock()

    def verify(self, header):
        if not isinstance(header, str) or not header.startswith("Basic ") or len(header) > 4096:
            return False
        fingerprint = hmac.new(self._pepper, header.encode(), hashlib.sha256).digest()
        with self._lock:
            if self._cached is not None and hmac.compare_digest(self._cached, fingerprint):
                return True
        try:
            raw = base64.b64decode(header[6:], validate=True).decode("utf-8")
            supplied_user, supplied_password = raw.split(":", 1)
        except (binascii.Error, UnicodeDecodeError, ValueError):
            return False
        if len(supplied_password) > 512:
            return False
        candidate = hashlib.pbkdf2_hmac(
            "sha256", supplied_password.encode(), self.salt, self.iterations)
        match = (hmac.compare_digest(supplied_user, self.username)
                 and hmac.compare_digest(candidate, self.digest))
        if match:
            with self._lock:
                self._cached = fingerprint
        return match


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Create private migration browser password")
    parser.add_argument("--create", action="store_true")
    args = parser.parse_args()
    if not args.create:
        parser.error("Specify --create")
    user, password = generate()
    print("Identifiant :", user)
    print("Mot de passe :", password)
    print("Enregistrez ce mot de passe dans un gestionnaire sécurisé.")
    print("Un hash PBKDF2 est conservé dans", CREDENTIAL_FILE)
