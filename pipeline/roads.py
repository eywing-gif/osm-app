"""Road lanes per 1 km square and per administrative area, for the Roads map views (tables of the map database):
road_cells (grid), road_view (the areas of each view, without gaps), road_view_lanes (lane-km per area and type).

    venv/bin/python -m pipeline.roads

Every road is cut into pieces of at most ~200 m; a piece counts for the square / area its middle lies in.
A view (backend/roads.py VIEWS) takes the areas of its level; where there are none, the next level in its list
fills the gap with what is left of its areas (a town without boroughs: its quarters, else the town itself).
Made by the map import (pipeline/osm.py); Settings → Map → "Prepare analyses" runs it for an older import.
"""
import psycopg

from backend.common import DB_OSM, dsn
from backend.roads import ADMIN_LEVELS, CELL, LANES_SQL, SRIDS, TYPE_SQL, VIEWS
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
CREATE UNLOGGED TABLE road_area AS
SELECT osm_id, admin_level::int AS level, min(name) AS name, ST_MakeValid(ST_Union(way)) AS way FROM planet_osm_polygon
WHERE boundary = 'administrative' AND admin_level IN ({", ".join(f"'{l}'" for l in ADMIN_LEVELS)})
GROUP BY osm_id, admin_level
"""
# the areas of `level` that the view does not cover yet: whole, or what is left of them (slivers along borders dropped)
FILL = """
INSERT INTO road_view_new (view, osm_id, level, name, way)
SELECT %(view)s, osm_id, level, name, ST_Multi(way) FROM (
  SELECT a.osm_id, a.level, a.name, ST_Area(a.way) AS full_area,
         CASE WHEN c.covered IS NULL THEN a.way
              ELSE ST_CollectionExtract(ST_MakeValid(ST_Difference(a.way, c.covered)), 3) END AS way
  FROM road_area a
  LEFT JOIN LATERAL (SELECT ST_Union(v.way) AS covered FROM road_view_new v
                     WHERE v.view = %(view)s AND v.way && a.way AND ST_Intersects(v.way, a.way)) c ON true
  WHERE a.level = %(level)s) s
WHERE NOT ST_IsEmpty(way) AND ST_Area(way) > GREATEST(0.02 * full_area, 100000)
"""
LANES = """
CREATE TABLE road_view_lanes_new AS
SELECT v.id, s.t, sum(s.m) / 1000 AS km, sum(s.m * s.lanes) / 1000 AS lane_km
FROM road_view_new v JOIN road_seg s ON s.pt && v.way AND ST_Contains(v.way, s.pt)
GROUP BY 1, 2
"""
TABLES = ("road_cells", "road_view", "road_view_lanes")
OLD = ("road_admin", "road_admin_area")       # made by version 0.8.0


def main(db: str = DB_OSM):
    with psycopg.connect(dsn(db), autocommit=True) as conn:
        for t in TABLES:
            conn.execute(f"DROP TABLE IF EXISTS {t}_new")
        conn.execute("DROP TABLE IF EXISTS road_seg, road_area")
        progress(0, "roads: cutting")
        conn.execute(SEGMENTS)
        conn.execute("CREATE INDEX ON road_seg USING gist (pt)")
        conn.execute("CREATE TABLE road_cells_new (srid int, gx int, gy int, t smallint, km float8, lane_km float8)")
        for i, srid in enumerate(SRIDS):
            progress(20 + 20 * i / len(SRIDS), f"roads: grid {i + 1} / {len(SRIDS)}")
            conn.execute(CELLS, {"srid": srid})
        conn.execute("CREATE INDEX ON road_cells_new (srid, gx, gy)")
        progress(40, "roads: areas")
        conn.execute(AREAS)
        conn.execute("CREATE INDEX ON road_area USING gist (way)")
        conn.execute("CREATE TABLE road_view_new (id serial PRIMARY KEY, view smallint, osm_id bigint, level smallint, name text, "
                     "way geometry(MultiPolygon, 3857), km2 float8)")
        conn.execute("CREATE INDEX ON road_view_new USING gist (way)")
        for view, levels in VIEWS.items():
            for level in levels:
                conn.execute(FILL, {"view": view, "level": level})
        conn.execute("UPDATE road_view_new SET km2 = ST_Area(ST_Transform(way, 4326)::geography) / 1e6")
        conn.execute("CREATE INDEX ON road_view_new (view)")
        progress(60, "roads: lanes per area")
        conn.execute(LANES)
        conn.execute("CREATE INDEX ON road_view_lanes_new (id)")
        conn.execute("DROP TABLE road_seg, road_area")
        for t in TABLES:                         # swap in all at once
            conn.execute(f"DROP TABLE IF EXISTS {t}")
            conn.execute(f"ALTER TABLE {t}_new RENAME TO {t}")
            conn.execute(f"ANALYZE {t}")
        for t in OLD:
            conn.execute(f"DROP TABLE IF EXISTS {t}")
        n, km = conn.execute("SELECT count(*), sum(lane_km) FROM road_cells WHERE srid = %s", (SRIDS[0],)).fetchone()
        views = conn.execute("SELECT string_agg(view || ': ' || n, ', ' ORDER BY view) FROM (SELECT view, count(*) n FROM road_view GROUP BY 1) s").fetchone()[0]
    log(f"{n:,} squares, {km or 0:,.0f} lane-km; areas per view {views}")


if __name__ == "__main__":
    main()
