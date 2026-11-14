"""Build the map database "osm" from downloaded Geofabrik regions.

    venv/bin/python -m pipeline.osm download europe/germany/sachsen
    venv/bin/python -m pipeline.osm build europe/germany/sachsen europe/germany/berlin

`build` imports the files with osm2pgsql into a fresh database and swaps it in; the old one stays as <osm>_old.
Data: © OpenStreetMap contributors (ODbL), extracts by Geofabrik.
"""
import subprocess
import sys

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


def build(regions: list[str], fresh: bool = False):
    have("osm2pgsql")
    for r in regions:
        fetch(r, fresh)
    new = DB_OSM + "_import"
    progress(5, "creating database")
    recreate_db(new, ("postgis", "hstore"))
    progress(10, "importing (takes a while)")
    # --slim --drop: helper tables only during the import; several files are merged by osm2pgsql itself
    subprocess.run(["osm2pgsql", "-d", new, "--create", "--slim", "--drop", "--hstore", "--cache", "2000",
                    "--number-processes", "4", *map(str, (catalog.osm_file(r) for r in regions))], check=True)
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
