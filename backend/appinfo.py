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


@router.get("/latest")
def latest():
    """The newest version tag on GitHub (tags like v0.6.0)."""
    cfg = config()
    if cfg["repo"].startswith("OWNER/"):
        raise HTTPException(400, "Repository not set (app.json).")
    try:
        tags = fetch_json(f"https://api.github.com/repos/{cfg['repo']}/tags", timeout=15)
    except Exception as e:
        raise HTTPException(502, f"GitHub not reachable: {e}")
    names = sorted((t["name"] for t in tags if parse(t["name"])), key=parse, reverse=True)
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
