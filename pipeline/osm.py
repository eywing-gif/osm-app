"""Build the map database "osm" from downloaded Geofabrik regions.

    venv/bin/python -m pipeline.osm download europe/germany/sachsen
    venv/bin/python -m pipeline.osm build europe/germany/sachsen europe/germany/berlin

`build` imports the files with osm2pgsql into a fresh database and swaps it in; the old one stays as <osm>_old.
Data: © OpenStreetMap contributors (ODbL), extracts by Geofabrik.
"""
import os
import subprocess
import sys
from pathlib import Path

import psycopg

from backend import catalog
from backend.common import DB_OSM, dsn, update_state
from . import roads
from .util import download, have, log, progress, recreate_db, stage

INDEXES = """
CREATE INDEX ON planet_osm_point USING gin (tags);
CREATE INDEX ON planet_osm_polygon USING gin (tags);
CREATE INDEX ON planet_osm_point (amenity);   CREATE INDEX ON planet_osm_polygon (amenity);
CREATE INDEX ON planet_osm_point (shop);      CREATE INDEX ON planet_osm_polygon (shop);
CREATE INDEX ON planet_osm_point (tourism);   CREATE INDEX ON planet_osm_polygon (tourism);
CREATE INDEX ON planet_osm_polygon (osm_id);
ANALYZE;
"""


def fetch(region: str, fresh: bool = False):
    """Download a region (fresh: again, the newest extract; the old file stays until the new one is complete)."""
    f = catalog.osm_file(region)
    if f.exists() and not fresh:
        return
    download(f"{catalog.GEOFABRIK_PREFIX}{region}-latest.osm.pbf", f, region.rsplit("/", 1)[-1])
    update_state(lambda st: st.setdefault("osm", {}).setdefault("downloaded", {}).__setitem__(region, f.stat().st_size))


DISK_FACTOR = 20      # the import needs about this many times the size of the .pbf files on the database disk (at its peak)


def free_disk() -> int | None:
    """Free bytes on the disk PostgreSQL keeps its data on (None if unknown)."""
    try:
        with psycopg.connect(dsn("postgres")) as c:
            path = Path(c.execute("SHOW data_directory").fetchone()[0])
    except psycopg.Error:
        return None
    for p in (path, *path.parents):        # the data folder itself is not readable for us: its parent is on the same disk
        try:
            st = os.statvfs(p)
            return st.f_bavail * st.f_frsize
        except OSError:
            continue
    return None


def cache_mb() -> int:
    """Node cache of osm2pgsql: 2 GB, less on a machine with little free memory."""
    try:
        avail = next(int(l.split()[1]) for l in open("/proc/meminfo") if l.startswith("MemAvailable")) // 1024
    except (OSError, StopIteration, ValueError):
        return 1000
    return max(300, min(2000, avail * 4 // 10))


def import_files(db: str, files: list[Path]):
    """osm2pgsql into db; its output goes to the log. On failure: a clear message instead of a traceback."""
    # --slim --drop: helper tables only during the import; several files are merged by osm2pgsql itself
    cmd = ["osm2pgsql", "-d", db, "--create", "--slim", "--drop", "--hstore", "--cache", str(cache_mb()),
           "--number-processes", str(min(4, os.cpu_count() or 1)), *map(str, files)]
    log(" ".join(cmd))
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
    last_error = ""
    for line in proc.stdout:
        print(line, end="", flush=True)
        if "ERROR" in line:
            last_error = line.split("ERROR", 1)[1].lstrip(": ").strip()
    rc = proc.wait()
    if rc:
        reason = last_error or (f"stopped by signal {-rc} – out of memory?" if rc < 0 else f"exit code {rc}")
        if "space" in reason.lower():
            reason += " – not enough disk space"
        raise SystemExit(f"Map import failed: {reason}")


def build(regions: list[str], fresh: bool = False):
    have("osm2pgsql")
    for r in regions:
        fetch(r, fresh)
    files = [catalog.osm_file(r) for r in regions]
    need, free = sum(f.stat().st_size for f in files) * DISK_FACTOR, free_disk()
    if free is not None and free < need:
        raise SystemExit(f"Not enough disk space: the import needs about {need / 1e9:.0f} GB, {free / 1e9:.0f} GB are free. "
                         "Choose smaller regions or free up space.")
    new = DB_OSM + "_import"
    progress(5, "creating database")
    recreate_db(new, ("postgis", "hstore"))
    progress(10, "importing (takes a while)")
    try:
        import_files(new, files)
    except BaseException:
        with psycopg.connect(dsn("postgres"), autocommit=True) as c:      # give the disk space back
            c.execute(f"DROP DATABASE IF EXISTS {new} WITH (FORCE)")
        raise
    progress(80, "indexes")
    with psycopg.connect(dsn(new), autocommit=True) as c:
        c.execute(INDEXES)
        box = c.execute("""SELECT ST_XMin(b), ST_YMin(b), ST_XMax(b), ST_YMax(b) FROM
                           (SELECT ST_Extent(ST_Transform(way, 4326)) AS b FROM planet_osm_point) s""").fetchone()
    stage(82, 95)
    roads.main(new)                      # road lanes per 1 km square (map layer "Roads")
    stage(0, 100)
    progress(95, "switching")
    with psycopg.connect(dsn("postgres"), autocommit=True) as c:
        c.execute(f"DROP DATABASE IF EXISTS {DB_OSM}_old")
        if c.execute("SELECT 1 FROM pg_database WHERE datname = %s", (DB_OSM,)).fetchone():
            c.execute(f"ALTER DATABASE {DB_OSM} RENAME TO {DB_OSM}_old")
        c.execute(f"ALTER DATABASE {new} RENAME TO {DB_OSM}")
        c.execute(f"DROP DATABASE IF EXISTS {DB_OSM}_old")        # not worth the disk space
    update_state(lambda st: st.setdefault("osm", {}).update(active=regions, bbox=list(box)))
    log("done")


if __name__ == "__main__":
    cmd, args = sys.argv[1], sys.argv[2:]
    if cmd == "download":
        for r in args:
            fetch(r)
    elif cmd == "build":
        build(args)
    elif cmd == "update":          # newest extracts of the regions, then build them
        build(args, fresh=True)
