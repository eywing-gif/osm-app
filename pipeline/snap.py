"""Give the transit segments their real course (tracks, tram rails, roads) where OSM data is available.

The GTFS feed has no geometry, so pipeline.gtfs stores straight lines between stops. Here, for every segment inside
the area of the map database, we look for an OSM route relation of the same kind (bus, tram, subway, S-Bahn, train,
ferry) that passes close to both stops and cut out the piece between them. The result is stored as transit_edge.path;
segments without a match keep the straight line. transit_edge.line is what the map uses (path if there is one, else the
straight line). Also finds the lines that run on top of each other (transit_edge.ov).
Data: © OpenStreetMap contributors (ODbL).
"""
import psycopg

from backend.common import DB_OSM, dsn, load_state
from .util import log

# which OSM route=* values can carry a GTFS mode
MODES = [("bus", "bus"), ("bus", "trolleybus"), ("tram", "tram"), ("subway", "subway"),
         ("sbahn", "light_rail"), ("sbahn", "train"), ("regional", "train"), ("regional", "light_rail"),
         ("intercity", "train"), ("highspeed", "train"), ("ferry", "ferry"), ("other", "funicular")]
NEAR_RAIL = 450     # second pass for rail modes
RAIL = ('regional', 'sbahn', 'intercity', 'highspeed', 'subway', 'tram')
NEAR = 130          # a stop must be within this distance (EPSG:3857 units, ~80 m at 52°N) of the OSM route
DETOUR = 2.5        # the cut piece may be at most 2.5 x the straight distance + SLACK long (rejects loops / wrong branches)
SLACK = 400


OVERLAP = 0.00015   # degrees (~10 m): lines this close to each other for most of their length overlap


def overlaps(db):
    """Which segments of *different* modes run on top of each other (S-Bahn + regional train, tram + bus …).
    The map draws such lines side by side. Stored as transit_edge.ov = ids of the segments it overlaps."""
    with psycopg.connect(dsn(db)) as tr:
        log("looking for lines that run on top of each other …")
        tr.execute("DROP TABLE IF EXISTS transit_overlap")
        # (1) segments with a real course: close to each other for most of their length.
        #     Cheap candidates first (bounding boxes via the index), the exact length test only for those.
        # (2) straight segments between the same two stations (e.g. S-Bahn and regional train on the same stretch)
        tr.execute(f"""CREATE TABLE transit_overlap AS
            SELECT DISTINCT a, b FROM (
              SELECT a.id AS a, b.id AS b FROM transit_edge a JOIN transit_edge b
                ON a.path IS NOT NULL AND b.path IS NOT NULL AND a.cat <> b.cat
               AND a.path && ST_Expand(b.path, {OVERLAP}) AND ST_DWithin(a.path, b.path, {OVERLAP})
               AND ST_Length(ST_Intersection(a.path, ST_Buffer(b.path, {OVERLAP}))) >= 0.5 * ST_Length(a.path)
              UNION ALL
              SELECT b.id, a.id FROM transit_edge a JOIN transit_edge b
                ON a.path IS NOT NULL AND b.path IS NOT NULL AND a.cat <> b.cat
               AND a.path && ST_Expand(b.path, {OVERLAP}) AND ST_DWithin(a.path, b.path, {OVERLAP})
               AND ST_Length(ST_Intersection(a.path, ST_Buffer(b.path, {OVERLAP}))) >= 0.5 * ST_Length(a.path)
              UNION ALL
              SELECT a.id, b.id FROM transit_edge a JOIN transit_edge b
                ON a.cat <> b.cat AND a.alon = b.alon AND a.alat = b.alat AND a.blon = b.blon AND a.blat = b.blat
               AND a.path IS NULL AND b.path IS NULL) x""")
        tr.execute("ALTER TABLE transit_edge DROP COLUMN IF EXISTS ov")
        tr.execute("ALTER TABLE transit_edge ADD COLUMN ov int[]")
        tr.execute("UPDATE transit_edge e SET ov = o.ids FROM (SELECT a, array_agg(b) AS ids FROM transit_overlap GROUP BY a) o WHERE e.id = o.a")
        tr.commit()
        n = tr.execute("SELECT count(*) FROM transit_edge WHERE ov IS NOT NULL").fetchone()[0]
        log(f"{n:,} segments run next to / on top of a line of another mode")


def main(db):
    osm = psycopg.connect(dsn(DB_OSM))
    tr = psycopg.connect(dsn(db))

    # area covered by the map database (a little widened)
    box = load_state().get("osm", {}).get("bbox")
    if not box or box[0] is None:
        log("no map data – segments keep their straight lines")
        tr.execute("ALTER TABLE transit_edge ADD COLUMN path geometry(LineString, 4326)")
        tr.execute("ALTER TABLE transit_edge ADD COLUMN line geometry(LineString, 4326)")
        tr.execute("UPDATE transit_edge SET line = geom")
        tr.execute("CREATE INDEX transit_edge_line_gix ON transit_edge USING gist (line)")
        tr.commit(); tr.close(); osm.close()
        return overlaps(db)
    x0, y0, x1, y1 = box[0] - .02, box[1] - .02, box[2] + .02, box[3] + .02
    inside = "alon BETWEEN %s AND %s AND alat BETWEEN %s AND %s AND blon BETWEEN %s AND %s AND blat BETWEEN %s AND %s"
    args = [x0, x1, y0, y1, x0, x1, y0, y1]
    edges = tr.execute(f"SELECT id, cat, alon, alat, blon, blat FROM transit_edge WHERE {inside}", args).fetchall()
    log(f"{len(edges):,} segments in the area of the map data")

    with osm.cursor() as cur:
        cur.execute("CREATE TEMP TABLE te (id int, cat text, alon float8, alat float8, blon float8, blat float8)")
        with cur.copy("COPY te FROM STDIN") as cp:
            for e in edges:
                cp.write_row(e)
        cur.execute("CREATE TEMP TABLE m (cat text, route text)")
        cur.executemany("INSERT INTO m VALUES (%s, %s)", MODES)
        cur.execute("""CREATE TEMP TABLE tp AS SELECT id, cat,
            ST_Transform(ST_SetSRID(ST_MakePoint(alon, alat), 4326), 3857) AS a,
            ST_Transform(ST_SetSRID(ST_MakePoint(blon, blat), 4326), 3857) AS b FROM te""")
        log("matching with OSM routes …")
        found = []
        # pass 1: strict. pass 2: trains / trams are allowed to be further from the stop (big stations are
        # mapped with a centre point that can be a few hundred metres from the tracks)
        for near, cats in ((NEAR, None), (NEAR_RAIL, RAIL)):
            filt = "" if cats is None else "AND e.cat = ANY(%s) AND e.id <> ALL(%s)"
            params = [] if cats is None else [list(cats), [r[0] for r in found]]
            cur.execute(f"""
                WITH cand AS (
                  SELECT e.id, e.a, e.b, l.way AS g
                  FROM tp e JOIN m ON m.cat = e.cat
                  JOIN planet_osm_line l ON l.route = m.route
                   AND l.way && ST_Expand(e.a, {near}) AND ST_DWithin(l.way, e.a, {near}) AND ST_DWithin(l.way, e.b, {near})
                  WHERE true {filt}),
                loc AS (SELECT *, ST_LineLocatePoint(g, a) AS fa, ST_LineLocatePoint(g, b) AS fb FROM cand),
                piece AS (
                  SELECT id, ST_Distance(a, b) AS d,
                         CASE WHEN fa < fb THEN ST_LineSubstring(g, fa, fb) ELSE ST_Reverse(ST_LineSubstring(g, fb, fa)) END AS s
                  FROM loc WHERE fa <> fb)
                SELECT DISTINCT ON (id) id, ST_AsText(ST_Transform(s, 4326))
                FROM piece
                WHERE GeometryType(s) = 'LINESTRING' AND ST_Length(s) > 0 AND ST_Length(s) <= {DETOUR} * d + {SLACK}
                ORDER BY id, ST_Length(s)""", params)
            found += cur.fetchall()
            log(f"  pass with {near} units: {len(found):,} matched so far")
    log(f"{len(found):,} of {len(edges):,} segments got a real course")

    with tr.cursor() as cur:
        cur.execute("ALTER TABLE transit_edge DROP COLUMN IF EXISTS path")
        cur.execute("ALTER TABLE transit_edge ADD COLUMN path geometry(LineString, 4326)")
        cur.execute("CREATE TEMP TABLE sp (id int, wkt text)")
        with cur.copy("COPY sp FROM STDIN") as cp:
            for row in found:
                cp.write_row(row)
        cur.execute("UPDATE transit_edge e SET path = ST_GeomFromText(sp.wkt, 4326) FROM sp WHERE e.id = sp.id")
        cur.execute("ALTER TABLE transit_edge DROP COLUMN IF EXISTS line")
        cur.execute("ALTER TABLE transit_edge ADD COLUMN line geometry(LineString, 4326)")
        cur.execute("UPDATE transit_edge SET line = COALESCE(path, geom)")
        cur.execute("CREATE INDEX transit_edge_line_gix ON transit_edge USING gist (line)")
        cur.execute("ANALYZE transit_edge")
        tr.commit()
        for cat, total, snapped in cur.execute(f"""SELECT cat, count(*), count(path) FROM transit_edge
                WHERE {inside} GROUP BY cat ORDER BY 2 DESC""", args).fetchall():
            log(f"  {cat:10s} {snapped:6,} / {total:6,} ({100 * snapped / total:.0f} %)")
    tr.commit(); tr.close(); osm.close()     # no open transaction may be left: overlaps() changes the same table
    overlaps(db)
