"""Length of the roads in an area, counted in lanes (lane-km), from the map database (planet_osm_line).

Type: from highway=* (service roads split into driveways, parking aisles and the rest). Lanes: the lanes=* tag;
without it an estimate: one-way 1 (motorway / trunk 2), two-way 2 (motorway / trunk 4), service roads, tracks,
foot- and cycleways 1. Dual carriageways are two one-way lines in OSM, so they count with both halves.

Two ways to look at it:
  POST /roads/stats   an area (named OSM area, drawn shape or the map view): cut exactly at the border
  GET  /roads/grid/…  map layer: 1 km squares of the population grid, per km² or per inhabitant. The squares come
                      from table road_cells (pipeline/roads.py, made once per map import).
"""
import json
from functools import lru_cache

import psycopg
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import population
from .common import db

router = APIRouter(prefix="/roads")

# (id, highway values) in list order; driveway / parking_aisle are highway=service with that service=* value
TYPES = [("motorway", ("motorway", "motorway_link")), ("trunk", ("trunk", "trunk_link")),
         ("primary", ("primary", "primary_link")), ("secondary", ("secondary", "secondary_link")),
         ("tertiary", ("tertiary", "tertiary_link")), ("unclassified", ("unclassified", "road")),
         ("residential", ("residential",)), ("living_street", ("living_street",)), ("service", ("service",)),
         ("driveway", ()), ("parking_aisle", ()), ("track", ("track",)), ("pedestrian", ("pedestrian",)),
         ("cycleway", ("cycleway",)), ("footway", ("footway", "path", "steps", "bridleway"))]
TYPE_IDS = [t for t, _ in TYPES]
OFF_BY_DEFAULT = ("service", "driveway", "parking_aisle", "track", "pedestrian", "cycleway", "footway")   # default: roads for cars
CELL = 1000                     # m: the squares of the map layer
SRIDS = (3035, 54009)           # coordinate systems of the population grids (catalog.POPULATION)

# SQL: type number (index in TYPES) and lanes of a line l of planet_osm_line
TYPE_SQL = "CASE " + " ".join(
    f"WHEN l.highway = 'service' AND l.service = '{t}' THEN {i}" for i, t in enumerate(TYPE_IDS) if t in ("driveway", "parking_aisle")
) + " " + " ".join(f"WHEN l.highway IN ({', '.join(repr(h) for h in hs)}) THEN {i}" for i, (_, hs) in enumerate(TYPES) if hs) + " END"
_ONEWAY = "(l.oneway IN ('yes', '1', 'true', '-1') OR l.highway IN ('motorway', 'motorway_link') OR l.tags -> 'junction' = 'roundabout')"
_BIG = "l.highway IN ('motorway', 'trunk')"
_SINGLE = "l.highway IN ('service', 'track', 'footway', 'path', 'steps', 'bridleway', 'cycleway', 'pedestrian') OR l.highway LIKE '%%\\_link'"
LANES_SQL = (f"LEAST(12, COALESCE(NULLIF(substring(l.tags -> 'lanes' from '^\\s*(\\d+)')::int, 0),"
             f" CASE WHEN {_BIG} THEN CASE WHEN {_ONEWAY} THEN 2 ELSE 4 END"
             f" WHEN {_SINGLE} OR {_ONEWAY} THEN 1 ELSE 2 END))")
TAGGED_SQL = "(l.tags ? 'lanes')"


class Area(BaseModel):
    geometry: dict          # GeoJSON (Multi)Polygon in WGS84


STATS_QUERY = f"""
WITH a AS (SELECT ST_Transform(ST_SetSRID(ST_GeomFromGeoJSON(%(geom)s), 4326), 3857) AS g),
r AS (
  SELECT {TYPE_SQL} AS t, {LANES_SQL} AS lanes, {TAGGED_SQL} AS tagged,
         ST_Length(ST_Transform(CASE WHEN ST_Within(l.way, a.g) THEN l.way ELSE ST_Intersection(l.way, a.g) END, 4326)::geography) AS m
  FROM planet_osm_line l, a
  WHERE l.way && a.g AND l.highway IS NOT NULL AND ST_Intersects(l.way, a.g)
)
SELECT t, count(*), sum(m) / 1000, sum(m * lanes) / 1000, sum(m) FILTER (WHERE tagged) / 1000
FROM r WHERE t IS NOT NULL GROUP BY t
"""


@router.post("/stats")
def stats(req: Area):
    """km and lane-km per type inside the area; the area's size and inhabitants for the ratios."""
    if req.geometry.get("type") not in ("Polygon", "MultiPolygon"):
        raise HTTPException(400, "Area must be a polygon.")
    geom = json.dumps(req.geometry)
    try:
        with db() as conn:
            conn.execute("SET statement_timeout = '120s'")
            rows = conn.execute(STATS_QUERY, {"geom": geom}).fetchall()
            km2 = conn.execute("SELECT ST_Area(ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326)::geography) / 1e6", (geom,)).fetchone()[0]
    except psycopg.errors.QueryCanceled:
        raise HTTPException(400, "Area too large – choose a smaller one.")
    try:
        pop = population.area_stats(req.geometry)
        people = pop["population"] if pop["cells"] else None
    except HTTPException:
        people = None                     # no population data
    by = {t: (n, km, lane, tagged) for t, n, km, lane, tagged in rows}
    types = [{"id": tid, "ways": by[i][0], "km": round(by[i][1], 2), "lane_km": round(by[i][2], 2), "tagged_km": round(by[i][3] or 0, 2)}
             for i, tid in enumerate(TYPE_IDS) if i in by]
    return {"area_km2": round(km2, 3), "population": people, "types": types}


# ---------- map layer: 1 km squares ----------
def has_cells() -> bool:
    with db() as conn:
        return bool(conn.execute("SELECT 1 FROM pg_tables WHERE tablename = 'road_cells'").fetchone())


CELLS_QUERY = """
SELECT floor(gx::float8 * %(cell)s / %(size)s)::int AS x, floor(gy::float8 * %(cell)s / %(size)s)::int AS y, sum(lane_km)
FROM road_cells, (SELECT ST_Expand(ST_Envelope(ST_Transform(ST_MakeEnvelope(%(minx)s, %(miny)s, %(maxx)s, %(maxy)s, 3857), %(srid)s)), %(size)s) AS b) box
WHERE srid = %(srid)s AND t = ANY(%(types)s)
  AND gx BETWEEN floor(ST_XMin(b) / %(cell)s) AND ceil(ST_XMax(b) / %(cell)s) AND gy BETWEEN floor(ST_YMin(b) / %(cell)s) AND ceil(ST_YMax(b) / %(cell)s)
GROUP BY 1, 2
"""
POP_QUERY = """
SELECT floor(c.cx::float8 / %(size)s)::int, floor(c.cy::float8 / %(size)s)::int, sum(c.pop)
FROM {table} c, (SELECT ST_Expand(ST_Envelope(ST_Transform(ST_MakeEnvelope(%(minx)s, %(miny)s, %(maxx)s, %(maxy)s, 3857), %(srid)s)), %(size)s) AS b) box
WHERE c.geom && box.b GROUP BY 1, 2
"""
CENTRES_QUERY = """
SELECT ST_X(p), ST_Y(p) FROM (SELECT ST_Transform(ST_SetSRID(ST_MakePoint(x * %(size)s + %(size)s / 2.0, y * %(size)s + %(size)s / 2.0), %(srid)s), 3857) AS p
FROM unnest(%(xs)s::int[], %(ys)s::int[]) AS u(x, y)) s
"""


def grid_srid() -> tuple[int, str | None]:
    """SRID of the active population grid (and its table), so the squares line up with it."""
    try:
        g = population.grid()
        return g.srid, g.table
    except HTTPException:
        return SRIDS[0], None


@lru_cache(maxsize=1024)
def grid_json(z: int, x: int, y: int, types: tuple, per: str, srid: int, table: str | None) -> bytes:
    """[x, y, value] per square: lane-km per km² (per=area) or lane-metres per inhabitant (per=person)."""
    box, res, size = population.tile_frame(z, x, y, CELL, 0)
    p = dict(box, size=size, cell=CELL, srid=srid, types=list(types))
    with db() as conn:
        roads = {(a, b): v for a, b, v in conn.execute(CELLS_QUERY, p).fetchall()}
    if per == "person":
        if not table:
            raise HTTPException(409, "No population data – Settings → Population")
        with population.connect() as conn:
            people = {(a, b): v for a, b, v in conn.execute(POP_QUERY.format(table=table), p).fetchall()}
        vals = {k: v * 1000 / people[k] for k, v in roads.items() if people.get(k, 0) >= 1}
    else:
        vals = {k: v / (size / 1000) ** 2 for k, v in roads.items()}
    keys = list(vals)
    with population.connect() as conn:     # any database with PostGIS: projects the square centres
        pts = conn.execute(CENTRES_QUERY, dict(size=size, srid=srid, xs=[k[0] for k in keys], ys=[k[1] for k in keys])).fetchall() if keys else []
        rows = [(px, py, vals[k]) for (px, py), k in zip(pts, keys)]
        return population.tile_json(conn, rows, srid, box, res, size, lambda v: round(v, 1 if v < 100 else 0))


@router.get("/grid/{z}/{x}/{y}")
def grid_tile(z: int, x: int, y: int, types: str = "", per: str = "area"):
    if not 0 <= z <= 22 or not (0 <= x < 2 ** z and 0 <= y < 2 ** z):
        raise HTTPException(404, "No such tile")
    if per not in ("area", "person"):
        raise HTTPException(400, "per must be area or person")
    if not has_cells():
        raise HTTPException(409, "Roads not prepared – Settings → Map → Prepare roads")
    ids = tuple(sorted(TYPE_IDS.index(t) for t in set(types.split(",")) if t in TYPE_IDS))
    srid, table = grid_srid()
    return population.tile_response(grid_json(z, x, y, ids, per, srid, table if per == "person" else None))


@router.get("/info")
def info():
    return {"types": TYPE_IDS, "off": list(OFF_BY_DEFAULT), "prepared": has_cells()}


def clear_caches():
    grid_json.cache_clear()

