"""The app itself: version, system checks, update from GitHub, restart."""
import json
import os
import shutil
import sys
import threading
import time

import psycopg
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .common import ROOT, fetch_json

router = APIRouter(prefix="/app")


def config() -> dict:
    return json.loads((ROOT / "app.json").read_text())


def parse(v: str) -> tuple:
    return tuple(int(p) for p in v.lstrip("v").split(".") if p.isdigit())


def checks() -> list[dict]:
    """What the machine needs: [{name, ok}]. Anything missing: the Repair button (Settings → App) fixes it."""
    out = [{"name": n, "ok": bool(shutil.which(c))} for n, c in (("Database tools", "psql"), ("Map import (osm2pgsql)", "osm2pgsql"), ("git", "git"))]
    try:
        with psycopg.connect("dbname=postgres", connect_timeout=3) as c:
            role = c.execute("SELECT rolcreatedb OR rolsuper FROM pg_roles WHERE rolname = current_user").fetchone()[0]
            gis = c.execute("SELECT count(*) FROM pg_available_extensions WHERE name = 'postgis'").fetchone()[0]
        out += [{"name": "Database", "ok": bool(role)}, {"name": "PostGIS", "ok": bool(gis)}]
    except psycopg.Error:
        out.append({"name": "Database", "ok": False})
    return out


@router.get("")
def info():
    cfg = config()
    return {"version": cfg["version"], "repo": cfg["repo"], "mode": "git" if (ROOT / ".git").exists() else "zip", "checks": checks()}


def _tags(repo: str) -> list[str]:
    """Version tags on GitHub: the API (twice – it answers 502/504 now and then), else git itself, which uses another server."""
    err = None
    for attempt in range(2):
        try:
            return [t["name"] for t in fetch_json(f"https://api.github.com/repos/{repo}/tags", timeout=15)]
        except Exception as e:
            err = e
            time.sleep(1 + attempt)
    if shutil.which("git"):
        import subprocess
        try:
            out = subprocess.run(["git", "ls-remote", "--tags", f"https://github.com/{repo}.git"], capture_output=True, text=True, timeout=30,
                                 env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
            if out.returncode == 0:
                return [l.rsplit("/", 1)[-1] for l in out.stdout.splitlines() if not l.endswith("^{}")]
        except (OSError, subprocess.SubprocessError):
            pass
    code = getattr(err, "code", None)
    raise HTTPException(502, f"GitHub is not answering ({'busy, HTTP ' + str(code) if code else err}) – try again in a minute.")


@router.get("/latest")
def latest():
    """The newest version tag on GitHub (tags like v0.6.0)."""
    cfg = config()
    if cfg["repo"].startswith("OWNER/"):
        raise HTTPException(400, "Repository not set (app.json).")
    names = sorted((t for t in _tags(cfg["repo"]) if parse(t)), key=parse, reverse=True)
    if not names:
        raise HTTPException(404, "No version published yet.")
    return {"latest": names[0], "current": cfg["version"], "newer": parse(names[0]) > parse(cfg["version"])}


class Update(BaseModel):
    tag: str


@router.post("/update")
def update(req: Update):
    from . import datasets
    if not parse(req.tag):
        raise HTTPException(400, "Bad version.")
    return datasets.start("App " + req.tag, "pipeline.update", req.tag)


@router.post("/repair")
def repair():
    """Install / start what is missing (asks for the administrator password in a window: pipeline/repair.py)."""
    from . import datasets
    return datasets.start("Repair", "pipeline.repair")


@router.post("/restart")
def restart():
    """Start the server again with the (new) code: same process, same port."""
    def go():
        time.sleep(0.5)
        os.execv(sys.executable, sys.orig_argv)
    threading.Thread(target=go, daemon=True).start()
    return {"ok": True}
