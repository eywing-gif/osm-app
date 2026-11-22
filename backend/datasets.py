"""Settings: which data is installed and active, and the download / import jobs.

Three kinds of data: osm (map regions → one merged database), population (grids, one table each),
transit (timetable feeds, one database each). One job runs at a time (a subprocess of pipeline/*).
"""
import os
import subprocess
import sys
import time

import psycopg
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import amenities, catalog, population, reach, roads, supply, transit
from .common import DATA, DB_POP, PREFIX, ROOT, db_transit_name, dsn, load_state, update_state

router = APIRouter(prefix="/datasets")
JOB: dict = {}          # the current / last job: proc, title, log, started, done (finalized)


# ---------- what is installed ----------
def _query(db: str, sql: str, *args):
    try:
        with psycopg.connect(dsn(db)) as c:
            return [r[0] for r in c.execute(sql, args).fetchall()]
    except psycopg.OperationalError:
        return []


def _tables(prefix: str, known: dict) -> list[str]:
    # only complete grids: the index is built last (an interrupted import of an older version has none)
    names = _query(DB_POP, """SELECT tablename FROM pg_tables t WHERE tablename LIKE %s
                              AND EXISTS (SELECT 1 FROM pg_indexes i WHERE i.tablename = t.tablename AND i.indexdef ~* 'gist')""", prefix + "\\_%")
    return [n[len(prefix) + 1:] for n in names if n[len(prefix) + 1:] in known]


def installed_population() -> list[str]:
    return _tables("pop", catalog.POPULATION)


def installed_built() -> list[str]:
    return _tables("built", catalog.BUILT)


def installed_transit() -> list[str]:
    """Complete timetables only: not the one being imported, not one whose import stopped (the line levels are made last)."""
    names = _query("postgres", "SELECT datname FROM pg_database WHERE datname LIKE %s", PREFIX + "transit\\_%")
    busy = JOB.get("args", [None])[-1] if JOB and JOB["proc"].poll() is None else None
    return [f for n in names if (f := n[len(PREFIX) + 8:]) in catalog.FEEDS and f != busy
            and _query(db_transit_name(f), "SELECT 1 FROM pg_tables WHERE tablename = 'transit_edge_lvl'")]


def outdated_transit(feed: str) -> bool:
    """Built by an older version of the app: without the hops the Reach view needs."""
    return not _query(db_transit_name(feed), "SELECT 1 FROM pg_tables WHERE tablename = 'transit_link'")


def downloaded_osm() -> dict[str, int]:
    files = {r["id"]: catalog.osm_file(r["id"]) for r in catalog.osm_regions()}
    return {i: f.stat().st_size for i, f in files.items() if f.exists()}


def _active(kind: str, installed: list[str]):
    a = load_state().get(kind, {}).get("active")
    return a if a in installed else None


OSM_DISK = 10        # map region on disk ≈ 10 × the .osm.pbf (the database ≈ 9 ×, measured, plus the download, which is kept)


@router.get("")
def overview():
    st = load_state()
    have = downloaded_osm()
    sizes = catalog.osm_sizes()
    built = st.get("osm", {}).get("active", [])
    pops, feeds, builts = installed_population(), installed_transit(), installed_built()
    pa, fa, ba = _active("population", pops), _active("transit", feeds), _active("built", builts)
    return {
        "osm": {"regions": [{**r, "bytes": have.get(r["id"]), "est": sizes.get(r["id"]), "active": r["id"] in built} for r in catalog.osm_regions()],
                "built": built, "roads": bool(built) and _roads_ready(), "outdated": bool(built) and amenities.prepared() and amenities.outdated()},
        "osm_disk": OSM_DISK,
        "population": [{"id": k, **v, "disk": catalog.disk_mb(v, 3), "installed": k in pops, "active": k == pa} for k, v in catalog.POPULATION.items()],
        "built": [{"id": k, **v, "disk": catalog.disk_mb(v, 2), "installed": k in builts, "active": k == ba} for k, v in catalog.BUILT.items()],
        "transit": [{"id": k, **{f: x for f, x in v.items() if f not in ("parts", "full")}, "disk": catalog.disk_mb(v),
                     "installed": k in feeds, "active": k == fa, "outdated": k in feeds and outdated_transit(k)}
                    for k, v in catalog.FEEDS.items()],
        "job": job_status(),
    }


@router.get("/osm/sizes")
def osm_sizes(ids: str = ""):
    """{region: bytes} of the download; asks the server for the ones not known yet (at most 80 at a time)."""
    known = {r["id"] for r in catalog.osm_regions()}
    want = [i for i in ids.split(",") if i in known][:80]
    sizes = catalog.osm_sizes(want)
    return {i: sizes[i] for i in want if i in sizes}


@router.get("/active")
def active():
    """Short description of the active data (labels in the app, start view of the map)."""
    st = load_state()
    names = {r["id"]: r["name"] for r in catalog.osm_regions()}
    pa = _active("population", installed_population())
    ta = _active("transit", installed_transit())
    ba = _active("built", installed_built())
    return {"osm": [names.get(i, i) for i in st.get("osm", {}).get("active", [])], "bbox": st.get("osm", {}).get("bbox"),
            "population_id": pa or "", "cell_m": pa and catalog.POPULATION[pa]["cell"], "population": pa and f"{catalog.POPULATION[pa]['name']} · {catalog.POPULATION[pa]['source']}", "transit_name": ta and catalog.FEEDS[ta]["name"],
            "built_id": ba or "", "built_cell_m": ba and catalog.BUILT[ba]["cell"], "roads": _roads_ready(),
            # what may be missing or shown wrong (frontend: yellow hints): the area each data set covers, rail-only feeds, old builds
            "pop_bbox": pa and catalog.POPULATION[pa].get("bbox"), "built_bbox": ba and catalog.BUILT[ba].get("bbox"),
            "transit_bbox": ta and transit_bbox(ta), "transit_only": ta and catalog.FEEDS[ta].get("only"),
            "transit_full": ta and catalog.FEEDS[ta].get("full"), "transit_outdated": bool(ta) and outdated_transit(ta),
            "amenities_outdated": bool(st.get("osm", {}).get("active")) and amenities.prepared() and amenities.outdated()}


_bbox: dict = {}


def transit_bbox(feed: str):
    """Area of a feed: from the catalog, else where most of its stations are (1 % cut off at each side: a few far stations)."""
    if catalog.FEEDS[feed].get("bbox"):
        return catalog.FEEDS[feed]["bbox"]
    if feed not in _bbox:
        r = _query(db_transit_name(feed), """SELECT ARRAY[percentile_cont(0.01) WITHIN GROUP (ORDER BY lon), percentile_cont(0.01) WITHIN GROUP (ORDER BY lat),
                                                 percentile_cont(0.99) WITHIN GROUP (ORDER BY lon), percentile_cont(0.99) WITHIN GROUP (ORDER BY lat)]
                                          FROM transit_station""")
        _bbox[feed] = [round(v, 3) for v in r[0]] if r and r[0] and r[0][0] is not None else None
    return _bbox[feed]


def _roads_ready() -> bool:
    """The analyses made from the map data exist (road lanes and amenities: pipeline/prepare.py)."""
    try:
        return roads.has_cells() and amenities.prepared()
    except psycopg.Error:
        return False


def clear_caches():
    """New data or a finished job: forget what was computed from the old one."""
    _bbox.clear()
    for m in (transit, reach, roads, supply, amenities, population):
        m.clear_caches()


# ---------- jobs ----------
def start(title: str, *args: str):
    if JOB and JOB["proc"].poll() is None:
        raise HTTPException(409, "Another download is running.")
    DATA.mkdir(parents=True, exist_ok=True)
    log = DATA / "job.log"
    proc = subprocess.Popen([sys.executable, "-u", "-m", *args], cwd=ROOT, stdout=open(log, "w"), stderr=subprocess.STDOUT,
                            env={**os.environ, "PYTHONPATH": str(ROOT)})
    JOB.clear()
    JOB.update(proc=proc, title=title, log=log, started=time.time(), done=False, args=args)
    return job_status()


def job_status():
    if not JOB:
        return None
    rc = JOB["proc"].poll()
    lines = JOB["log"].read_text(errors="replace").splitlines()
    prog = next((l for l in reversed(lines) if l.startswith("@progress ")), "@progress 0").split(" ", 2)
    if rc is not None and not JOB["done"]:
        JOB["done"] = True
        clear_caches()
    state = "running" if rc is None else "done" if rc == 0 else "cancelled" if rc < 0 else "failed"
    text = prog[2] if len(prog) > 2 else ""
    if state == "failed":
        text = next((l for l in reversed(lines) if l.strip() and not l.startswith("@") and not l.startswith(" ")), "failed")
    end = time.time() if rc is None else JOB["log"].stat().st_mtime
    return {"title": JOB["title"], "state": state, "pct": 100 if state == "done" else int(prog[1]), "text": text,
            "secs": round(end - JOB["started"]), "steps": _steps(lines, state, end)}


def _steps(lines: list[str], state: str, end: float) -> list[dict]:
    """The job's steps (lines "@steps a|b|c" and "@step <time> a" of pipeline/util.py): [{name, state, secs}]."""
    names = next((l[7:].split("|") for l in lines if l.startswith("@steps ")), [])
    started = {}
    for l in lines:
        if l.startswith("@step "):
            _, t, name = l.split(" ", 2)
            started[name] = float(t)
    out, times = [], sorted(started.values())
    for name in names:
        t0 = started.get(name)
        if t0 is None:
            out.append({"name": name, "state": "todo"})
            continue
        t1 = next((t for t in times if t > t0), None)
        last = t1 is None
        st = "done" if not last or state == "done" else "run" if state == "running" else "stop"
        out.append({"name": name, "state": st, "secs": round((t1 or end) - t0)})
    return out


@router.get("/job")
def job():
    """The current / last job only (cheap: the app asks every few seconds while one runs)."""
    return job_status()


@router.post("/job/cancel")
def cancel():
    if JOB and JOB["proc"].poll() is None:
        JOB["proc"].terminate()
    return job_status()


# ---------- map regions ----------
class Regions(BaseModel):
    regions: list[str]


@router.post("/osm/download")
def osm_download(req: Regions):
    return start(f"Download {len(req.regions)} region(s)", "pipeline.osm", "download", *_valid_regions(req.regions))


@router.post("/osm/build")
def osm_build(req: Regions):
    """Make these regions the map data (imports them into one database)."""
    ids = _valid_regions(req.regions)
    # a region inside another chosen one would be loaded twice
    ids = [i for i in ids if not any(i.startswith(o + "/") for o in ids)]
    if not ids:
        raise HTTPException(400, "Choose at least one region.")
    names = {r["id"]: r["name"] for r in catalog.osm_regions()}
    return start("Map: " + ", ".join(names.get(i, i) for i in ids), "pipeline.osm", "build", *ids)


@router.post("/osm/update")
def osm_update(req: Regions):
    """Download the newest extracts of the regions in use and build the map data again."""
    ids = _valid_regions(req.regions)
    if not ids:
        raise HTTPException(400, "No map data in use.")
    return start("Update map data", "pipeline.osm", "update", *ids)


@router.post("/osm/roads")
def osm_roads():
    """Road lanes per 1 km square for the Roads layer (also part of every map import)."""
    return start("Prepare analyses", "pipeline.prepare")


@router.delete("/osm/{region:path}")
def osm_delete(region: str):
    f = catalog.osm_file(_valid_regions([region])[0])
    f.unlink(missing_ok=True)
    return {"ok": True}


def _valid_regions(ids: list[str]) -> list[str]:
    known = {r["id"] for r in catalog.osm_regions()}
    bad = [i for i in ids if i not in known]
    if bad:
        raise HTTPException(400, f"Unknown region: {bad[0]}")
    return ids


# ---------- population / built-up grids and timetable feeds ----------
KINDS = {"population": (catalog.POPULATION, "pipeline.population", installed_population),
         "built": (catalog.BUILT, "pipeline.built", installed_built),
         "transit": (catalog.FEEDS, "pipeline.transit", installed_transit)}


def _check(kind: str, ds: str):
    if kind not in KINDS or ds not in KINDS[kind][0]:
        raise HTTPException(404, "Unknown dataset")


@router.post("/{kind}/{ds}/install")
def install(kind: str, ds: str):
    """Download and import; for an installed dataset: the newest version (Update)."""
    _check(kind, ds)
    cat, module, installed = KINDS[kind]
    return start(("Update " if ds in installed() else "") + cat[ds]["name"], module, ds)


@router.post("/{kind}/{ds}/activate")
def activate(kind: str, ds: str):
    _check(kind, ds)
    if ds not in KINDS[kind][2]():
        raise HTTPException(400, "Not installed")
    update_state(lambda st: st.setdefault(kind, {}).__setitem__("active", ds))
    clear_caches()
    return {"ok": True}


@router.delete("/{kind}/{ds}")
def remove(kind: str, ds: str):
    _check(kind, ds)
    if kind == "transit":
        with psycopg.connect(dsn("postgres"), autocommit=True) as c:
            c.execute(f"DROP DATABASE IF EXISTS {db_transit_name(ds)} WITH (FORCE)")
    else:
        with psycopg.connect(dsn(DB_POP), autocommit=True) as c:
            c.execute(f"DROP TABLE IF EXISTS {'pop' if kind == 'population' else 'built'}_{ds}")
    update_state(lambda st: st.get(kind, {}).get("active") == ds and st[kind].pop("active"))
    clear_caches()
    return {"ok": True}
