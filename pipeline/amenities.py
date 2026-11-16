"""Amenities of the map database (table amenity_poi, one point per place) and the edges of green spaces
(table amenity_green_pt, points every 100 m along the outline: the way into a park is to its edge, not its middle).

    venv/bin/python -m pipeline.amenities

Groups and tags: backend/amenity_catalog.py. Made by the map import and by Settings → Map → "Prepare analyses".
"""
import psycopg

from backend.amenity_catalog import GREEN, GREEN_MIN_M2, code_sql
from backend.common import DB_OSM, dsn
from .util import log, progress

POIS = f"""
CREATE TABLE amenity_poi_new AS
SELECT (code / 100)::smallint AS g, (code % 100)::smallint AS s, name, way FROM (
  SELECT {code_sql("t", green=False)} AS code, t.name, t.way FROM planet_osm_point t
  UNION ALL
  SELECT {code_sql("t", green=False)}, t.name, ST_PointOnSurface(t.way) FROM planet_osm_polygon t
  UNION ALL       -- green spaces: one point each for counting (the edges are in amenity_green_pt)
  SELECT {code_sql("t", green=True)}, t.name, ST_PointOnSurface(t.way) FROM planet_osm_polygon t WHERE t.way_area >= {GREEN_MIN_M2}
) s WHERE code IS NOT NULL
"""
GREEN_EDGES = f"""
CREATE TABLE amenity_green_pt_new AS
SELECT (code % 100)::smallint AS s, (ST_DumpPoints(ST_Segmentize(ST_ExteriorRing((ST_Dump(way)).geom), 100))).geom AS way
FROM (SELECT {code_sql("t", green=True)} AS code, t.way FROM planet_osm_polygon t WHERE t.way_area >= {GREEN_MIN_M2}) s
WHERE code IS NOT NULL
"""


def main(db: str = DB_OSM):
    with psycopg.connect(dsn(db), autocommit=True) as conn:
        conn.execute("DROP TABLE IF EXISTS amenity_poi_new, amenity_green_pt_new")
        progress(0, "amenities")
        conn.execute(POIS)
        conn.execute("CREATE INDEX ON amenity_poi_new USING gist (way)")
        progress(50, "amenities: green spaces")
        conn.execute(GREEN_EDGES)
        conn.execute("CREATE INDEX ON amenity_green_pt_new USING gist (way)")
        for t in ("amenity_poi", "amenity_green_pt"):
            conn.execute(f"DROP TABLE IF EXISTS {t}")
            conn.execute(f"ALTER TABLE {t}_new RENAME TO {t}")
            conn.execute(f"ANALYZE {t}")
        n = conn.execute("SELECT count(*) FROM amenity_poi").fetchone()[0]
        e = conn.execute("SELECT count(*) FROM amenity_green_pt").fetchone()[0]
    log(f"{n:,} amenities, {e:,} points along green spaces (group {GREEN})")


if __name__ == "__main__":
    main()
