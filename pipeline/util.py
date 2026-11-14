"""Helpers of the import pipelines. Run them from the project root: venv/bin/python -m pipeline.<name> …

A line "@progress <percent> <text>" on stdout is what the Settings page shows while a job runs.
"""
import shutil
import subprocess
import time
import urllib.request

import psycopg

from backend.common import USER_AGENT, dsn


def log(*a):
    print(f"[{time.strftime('%H:%M:%S')}]", *a, flush=True)


_stage = (0.0, 100.0)


def stage(lo: float, hi: float):
    """The following progress() calls (0..100) are mapped to lo..hi of the whole job."""
    global _stage
    _stage = (lo, hi)


def progress(pct: float, text: str = ""):
    lo, hi = _stage
    print(f"@progress {lo + (hi - lo) * pct / 100:.0f} {text}", flush=True)


def download(url: str, dest, label: str, lo: float = 0, hi: float = 100):
    """Download to dest (via dest.part); progress is mapped to lo..hi percent."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as r, open(part, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done, last = 0, 0.0
        while chunk := r.read(1 << 20):
            f.write(chunk)
            done += len(chunk)
            if time.monotonic() - last > 1:
                last = time.monotonic()
                progress(lo + (hi - lo) * (done / total if total else 0), f"{label} {done >> 20}" + (f" / {total >> 20}" if total else "") + " MB")
    part.rename(dest)


def recreate_db(name: str, extensions=("postgis",)):
    """Drop and create an empty database."""
    subprocess.run(["dropdb", "--if-exists", name], check=True)
    subprocess.run(["createdb", name], check=True)
    with psycopg.connect(dsn(name), autocommit=True) as c:
        for e in extensions:
            c.execute(f"CREATE EXTENSION {e}")


def have(cmd: str):
    if not shutil.which(cmd):
        raise SystemExit(f"'{cmd}' is not installed")
