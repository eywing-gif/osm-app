"""Helpers shared by the backend modules."""
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("OSMAPP_DATA") or ROOT / "data")          # downloads, state, job logs
PREFIX = os.environ.get("OSMAPP_DB_PREFIX", "")                      # lets a test run use its own databases
USER_AGENT = "osm-tag-search/0.4 (personal desktop app)"
STATE_FILE = DATA / "state.json"

# ---- the databases: osm (map data), population (grid tables pop_<id>), transit_<feed id> (one per timetable feed)
DB_OSM = PREFIX + "osm"
DB_POP = PREFIX + "population"
_state_lock = threading.Lock()


def load_state() -> dict:
    """What is installed / active (written by the app and the import pipelines)."""
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def update_state(fn) -> dict:
    """Change the state file atomically: fn(state) edits the dict in place."""
    with _state_lock:
        st = load_state()
        fn(st)
        DATA.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, indent=1))
        tmp.replace(STATE_FILE)
        return st


def dsn(name: str) -> str:
    return f"dbname={name}"


def db_transit_name(feed: str) -> str:
    return f"{PREFIX}transit_{feed}"


def active_transit() -> str | None:
    return load_state().get("transit", {}).get("active")


def db():
    return psycopg.connect(dsn(DB_OSM))


def transit_db():
    """Connection to the database of the active timetable feed (503 if none is installed)."""
    from fastapi import HTTPException
    feed = active_transit()
    if not feed:
        raise HTTPException(503, "No timetable data – open Settings and download a feed.")
    try:
        return psycopg.connect(dsn(db_transit_name(feed)))
    except psycopg.OperationalError:
        raise HTTPException(503, "Timetable data missing – open Settings and download a feed.")


_last_call: dict[str, float] = {}
_lock = threading.Lock()


def fetch_json(url: str, params: dict | None = None, data: dict | None = None,
               timeout: float = 20, min_interval: float = 0):
    """GET (or POST, if `data` is given) a URL and parse the JSON answer.

    `min_interval` enforces a pause between calls to the same host, e.g. the
    Nominatim usage policy allows at most one request per second.
    """
    if params:
        url += "?" + urllib.parse.urlencode(params)
    if min_interval:
        host = urllib.parse.urlsplit(url).netloc
        with _lock:
            wait = _last_call.get(host, 0) + min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            _last_call[host] = time.monotonic()
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def geojson_bbox(geometry: dict) -> tuple[float, float, float, float]:
    """(minx, miny, maxx, maxy) of a GeoJSON geometry."""
    xs, ys = [], []

    def walk(c):
        if c and isinstance(c[0], (int, float)):
            xs.append(c[0]); ys.append(c[1])
        else:
            for part in c:
                walk(part)
    walk(geometry["coordinates"])
    return min(xs), min(ys), max(xs), max(ys)
