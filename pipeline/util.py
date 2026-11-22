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


def steps(*names: str):
    """The steps of this job, shown as a list in Settings (call once at the start)."""
    print("@steps " + "|".join(names), flush=True)


def step(name: str):
    """The named step (one of steps()) starts now; Settings shows its time."""
    print(f"@step {time.time():.1f} {name}", flush=True)
    log(name)


def watch_index(conn, sql: str, lo: float, hi: float, label: str = "index"):
    """Run CREATE INDEX on conn and report its progress (pg_stat_progress_create_index) as lo..hi percent."""
    import threading
    pid = conn.info.backend_pid
    stop = threading.Event()

    def poll():
        with psycopg.connect(conn.info.dsn, autocommit=True) as c:
            while not stop.wait(2):
                r = c.execute("""SELECT phase, blocks_done, blocks_total, tuples_done, tuples_total
                                 FROM pg_stat_progress_create_index WHERE pid = %s""", (pid,)).fetchone()
                if not r:
                    continue
                phase, bd, bt, td, tt = r
                f = bd / bt if bt else td / tt if tt else 0
                progress(lo + (hi - lo) * min(f, 1), f"{label} · {phase}" + (f" · {td / 1e6:.1f} M done" if td else ""))

    t = threading.Thread(target=poll, daemon=True)
    t.start()
    try:
        conn.execute(sql)
    finally:
        stop.set()
        t.join()


def index_memory_mb() -> int:
    """A quarter of the free memory, 256 MB … 1 GB: leaves room for the desktop and other apps
    (more would sort a world grid in memory, but the logged index build is limited by disk writes anyway)."""
    try:
        free = next(int(l.split()[1]) for l in open("/proc/meminfo") if l.startswith("MemAvailable:")) >> 10
    except (OSError, StopIteration, ValueError):
        free = 3072
    return max(256, min(1024, free // 4))


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
