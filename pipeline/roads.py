"""Road lanes per 1 km square (table road_cells of the map database), for the Roads map layer.

    venv/bin/python -m pipeline.roads

Every road is cut into pieces of at most 200 m in the coordinate system of each population grid; a piece counts
for the square its middle lies in. Made by the map import (pipeline/osm.py); run again after an import by hand.
"""
import psycopg

from backend.common import DB_OSM, dsn
from backend.roads import CELL, LANES_SQL, SRIDS, TYPE_SQL
from .util import log, progress

CELLS = f"""
INSERT INTO road_cells_new
SELECT %(srid)s, floor((ST_X(ST_StartPoint(s)) + ST_X(ST_EndPoint(s))) / 2 / {CELL})::int,
       floor((ST_Y(ST_StartPoint(s)) + ST_Y(ST_EndPoint(s))) / 2 / {CELL})::int, t, sum(ST_Length(s)) / 1000, sum(ST_Length(s) * lanes) / 1000
FROM (SELECT t, lanes, (ST_DumpSegments(ST_Segmentize(ST_Transform(way, %(srid)s), 200))).geom AS s
      FROM (SELECT l.way, {TYPE_SQL} AS t, {LANES_SQL} AS lanes FROM planet_osm_line l WHERE l.highway IS NOT NULL) r
      WHERE t IS NOT NULL) x
GROUP BY 2, 3, 4
"""


def main(db: str = DB_OSM):
    with psycopg.connect(dsn(db), autocommit=True) as conn:
        conn.execute("DROP TABLE IF EXISTS road_cells_new")
        conn.execute("CREATE TABLE road_cells_new (srid int, gx int, gy int, t smallint, km float8, lane_km float8)")
        for i, srid in enumerate(SRIDS):
            progress(100 * i / len(SRIDS), f"roads, grid {i + 1} / {len(SRIDS)}")
            conn.execute(CELLS, {"srid": srid})
        conn.execute("CREATE INDEX ON road_cells_new (srid, gx, gy)")
        conn.execute("DROP TABLE IF EXISTS road_cells")
        conn.execute("ALTER TABLE road_cells_new RENAME TO road_cells")
        conn.execute("ANALYZE road_cells")
        n, km = conn.execute("SELECT count(*), sum(lane_km) FROM road_cells WHERE srid = %s", (SRIDS[0],)).fetchone()
    log(f"{n:,} squares, {km or 0:,.0f} lane-km")


if __name__ == "__main__":
    main()
