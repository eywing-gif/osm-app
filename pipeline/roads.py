"""Road lanes per 1 km square and per administrative area (tables road_cells, road_admin, road_admin_area of the
map database), for the Roads map views.

    venv/bin/python -m pipeline.roads

Every road is cut into pieces of at most ~200 m; a piece counts for the square / area its middle lies in.
Made by the map import (pipeline/osm.py); Settings → Map → "Prepare roads" runs it for an older import.
"""
import psycopg

from backend.common import DB_OSM, dsn
from backend.roads import ADMIN_LEVELS, CELL, LANES_SQL, SRIDS, TYPE_SQL
from .util import log, progress

# pieces of road: type, lanes, length in metres (EPSG:3857 length / cosh(y / R) = true length), middle point
SEGMENTS = f"""
CREATE UNLOGGED TABLE road_seg AS
SELECT t, lanes, ST_Length(s) / cosh(ST_Y(c) / 6378137.0) AS m, c AS pt FROM (
  SELECT t, lanes, s, ST_SetSRID(ST_MakePoint((ST_X(ST_StartPoint(s)) + ST_X(ST_EndPoint(s))) / 2,
                                              (ST_Y(ST_StartPoint(s)) + ST_Y(ST_EndPoint(s))) / 2), 3857) AS c
  FROM (SELECT t, lanes, (ST_DumpSegments(ST_Segmentize(way, 200))).geom AS s
        FROM (SELECT l.way, {TYPE_SQL} AS t, {LANES_SQL} AS lanes FROM planet_osm_line l WHERE l.highway IS NOT NULL) r
        WHERE t IS NOT NULL) a) b
"""
CELLS = f"""
INSERT INTO road_cells_new
SELECT %(srid)s, floor(ST_X(q) / {CELL})::int, floor(ST_Y(q) / {CELL})::int, t, sum(m) / 1000, sum(m * lanes) / 1000
FROM (SELECT t, lanes, m, ST_Transform(pt, %(srid)s) AS q FROM road_seg) s
GROUP BY 2, 3, 4
"""
# administrative areas (osm2pgsql may store one area in several rows: they are merged)
AREAS = f"""
CREATE TABLE road_admin_area_new AS
SELECT osm_id, admin_level::int AS level, min(name) AS name, ST_Union(way) AS way FROM planet_osm_polygon
WHERE boundary = 'administrative' AND admin_level IN ({", ".join(f"'{l}'" for l in ADMIN_LEVELS)})
GROUP BY osm_id, admin_level
"""
ADMIN = """
CREATE TABLE road_admin_new AS
SELECT a.osm_id, s.t, sum(s.m) / 1000 AS km, sum(s.m * s.lanes) / 1000 AS lane_km
FROM road_admin_area_new a JOIN road_seg s ON s.pt && a.way AND ST_Contains(a.way, s.pt)
GROUP BY 1, 2
"""
TABLES = ("road_cells", "road_admin", "road_admin_area")


def main(db: str = DB_OSM):
    with psycopg.connect(dsn(db), autocommit=True) as conn:
        for t in TABLES:
            conn.execute(f"DROP TABLE IF EXISTS {t}_new")
        conn.execute("DROP TABLE IF EXISTS road_seg")
        progress(0, "roads: cutting")
        conn.execute(SEGMENTS)
        conn.execute("CREATE INDEX ON road_seg USING gist (pt)")
        conn.execute("CREATE TABLE road_cells_new (srid int, gx int, gy int, t smallint, km float8, lane_km float8)")
        for i, srid in enumerate(SRIDS):
            progress(25 + 25 * i / len(SRIDS), f"roads: grid {i + 1} / {len(SRIDS)}")
            conn.execute(CELLS, {"srid": srid})
        conn.execute("CREATE INDEX ON road_cells_new (srid, gx, gy)")
        progress(50, "roads: areas")
        conn.execute(AREAS)
        conn.execute("ALTER TABLE road_admin_area_new ADD COLUMN km2 float8")
        conn.execute("UPDATE road_admin_area_new SET km2 = ST_Area(ST_Transform(way, 4326)::geography) / 1e6")
        conn.execute("CREATE INDEX ON road_admin_area_new USING gist (way)")
        conn.execute(ADMIN)
        conn.execute("CREATE INDEX ON road_admin_new (osm_id)")
        conn.execute("DROP TABLE road_seg")
        for t in TABLES:                         # swap in all at once
            conn.execute(f"DROP TABLE IF EXISTS {t}")
            conn.execute(f"ALTER TABLE {t}_new RENAME TO {t}")
            conn.execute(f"ANALYZE {t}")
        n, km = conn.execute("SELECT count(*), sum(lane_km) FROM road_cells WHERE srid = %s", (SRIDS[0],)).fetchone()
        areas = conn.execute("SELECT count(*) FROM road_admin_area").fetchone()[0]
    log(f"{n:,} squares, {km or 0:,.0f} lane-km, {areas:,} administrative areas")


if __name__ == "__main__":
    main()
