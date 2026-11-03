#!/usr/bin/env python3
"""Safely add/remove one authenticated path to the existing Partage HTTPS site.

Run using sudo on the VPS. No new public port and no credentials in nginx.
Refuse to expose anything unless the localhost app first returns a Basic
authentication challenge on /api/jobs.
"""
import argparse
import datetime
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_SITE = Path("/etc/nginx/sites-available/partage")
START = "# BEGIN PARTAGE FBX MIGRATION HTTPS"
END = "# END PARTAGE FBX MIGRATION HTTPS"


def clean_nginx_source(src):
    """Mask comments and quoted strings while preserving offsets and newlines."""
    out = list(src)
    quote = None
    comment = False
    escaped = False
    for idx, c in enumerate(src):
        if comment:
            if c == "\n":
                comment = False
            else:
                out[idx] = " "
        elif quote:
            if c == "\n":
                # Avoid hiding line breaks even in an invalid multiline quote.
                continue
            out[idx] = " "
            if escaped:
                escaped = False
            elif c == "\\":
                escaped = True
            elif c == quote:
                quote = None
        elif c == "#":
            comment = True
            out[idx] = " "
        elif c in ("\"", "'"):
            quote = c
            out[idx] = " "
    return "".join(out)


def server_blocks(src):
    masked = clean_nginx_source(src)
    blocks = []
    for match in re.finditer(r"(?m)^\s*server\s*\{", masked):
        opened = match.end() - 1
        level = 0
        end = None
        for i in range(opened, len(masked)):
            if masked[i] == "{":
                level += 1
            elif masked[i] == "}":
                level -= 1
                if level == 0:
                    end = i
                    break
        if end is None:
            raise ValueError("Invalid Nginx braces; refusing modification")
        blocks.append((opened, end))
    return blocks


def tls_server(src):
    matches = []
    for opened, closed in server_blocks(src):
        masked = clean_nginx_source(src[opened:closed])
        if re.search(r"(?m)^\s*listen\s+(?:[^\s;]*:)?443(?:\s|;)", masked):
            matches.append((opened, closed))
    if len(matches) != 1:
        raise ValueError("Expected exactly one HTTPS server block in this site; found " + str(len(matches)))
    return matches[0]


def apply_snippet(src, snippet):
    if START in src:
        if src.count(START) != 1 or src.count(END) != 1:
            raise ValueError("Installation markers are inconsistent")
        return src
    if "/fbx-migration" in src:
        raise ValueError("A migration location already exists; refusing duplicate route")
    _, closing = tls_server(src)
    addition = "\n    " + START + "\n"
    for line in snippet.strip().splitlines():
        addition += ("    " + line if line else "") + "\n"
    addition += "    " + END + "\n"
    return src[:closing] + addition + src[closing:]


def remove_snippet(src):
    if START not in src:
        return src
    if src.count(START) != 1 or src.count(END) != 1:
        raise ValueError("Installation markers are inconsistent")
    begin = src.index(START)
    end = src.index(END, begin) + len(END)
    # Preserve surrounding site and its original TLS/certificate settings.
    return src[:begin].rstrip(" \t") + src[end:]


def check_auth_ready():
    request = urllib.request.Request("http://127.0.0.1:8791/api/jobs", method="GET")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            raise RuntimeError("Migration API returned HTTP " + str(response.status) + " without auth")
    except urllib.error.HTTPError as exc:
        challenge = exc.headers.get("WWW-Authenticate", "")
        if exc.code != 401 or 'Basic realm="Partage Migration"' not in challenge:
            raise RuntimeError("Migration API is not protected by the expected HTTP Basic challenge") from None
    except urllib.error.URLError:
        raise RuntimeError("Migration service unavailable on 127.0.0.1:8791") from None


def atomic_write(path, content, old_stat):
    fd, tmpname = tempfile.mkstemp(prefix=".partage-nginx-", dir=path.parent)
    try:
        os.fchmod(fd, stat.S_IMODE(old_stat.st_mode))
        os.fchown(fd, old_stat.st_uid, old_stat.st_gid)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmpname, path)
    finally:
        if os.path.exists(tmpname):
            os.unlink(tmpname)


def run_nginx(*command):
    subprocess.run(command, check=True)


def main():
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--apply", action="store_true")
    group.add_argument("--remove", action="store_true")
    parser.add_argument("--site", default=str(DEFAULT_SITE))
    args = parser.parse_args()

    if os.geteuid() != 0:
        parser.error("Run as root via sudo")
    if args.apply:
        check_auth_ready()

    path = Path(args.site)
    if not path.is_file() or path.is_symlink():
        parser.error("Expected regular existing Nginx site file: " + str(path))
    original = path.read_text(encoding="utf-8")
    if args.apply:
        snippet = (Path(__file__).resolve().parent / "nginx-https-location.conf").read_text(encoding="utf-8")
        result = apply_snippet(original, snippet)
    else:
        result = remove_snippet(original)
    if result == original:
        print("No changes needed.")
        return

    backup = path.with_name(path.name + ".pre-fbx-" +
        datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d%H%M%S"))
    shutil.copy2(path, backup)
    original_stat = path.stat()
    atomic_write(path, result, original_stat)
    try:
        run_nginx("nginx", "-t")
        run_nginx("systemctl", "reload", "nginx")
    except (subprocess.CalledProcessError, OSError):
        atomic_write(path, original, original_stat)
        try:
            run_nginx("nginx", "-t")
            run_nginx("systemctl", "reload", "nginx")
        except (subprocess.CalledProcessError, OSError):
            pass
        raise RuntimeError("Nginx validation/reload failed. Previous site restored; see nginx -t") from None

    print("HTTPS route", "enabled" if args.apply else "removed")
    print("Backup:", backup)
    print("Migration stays bound to localhost:8791; no public firewall port required.")
    if args.apply:
        print("Open https://137.74.169.214/fbx-migration/")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, OSError) as exc:
        sys.exit("ERROR: " + str(exc))
