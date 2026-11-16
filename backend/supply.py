"""Public transport supply: departures (07–19 h, one reference day, chosen types of line) of the active timetable,
added up per grid square, per administrative area or for one area – in total, per km² or per 1,000 inhabitants.

The departures of a station are counted where the station is (table transit_station: one row per station and type).
"""
import json
from functools import lru_cache

import psycopg
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import population, roads
from .common import active_transit, db, transit_db
from .transit import CATEGORIES, DAYS

router = APIRouter(prefix="/transit/supply")
PER_PEOPLE = 1000          # "per person": departures per 1,000 inhabitants


def _filters(day: str, modes: str) -> list[str]:
    if day not in DAYS:
        raise HTTPException(400, "day must be wd, sa or su")
    return [m for m in modes.split(",") if m in CATEGORIES] if modes else CATEGORIES


GRID_QUERY = """
SELECT floor(ST_X(q) / %(size)s)::int, floor(ST_Y(q) / %(size)s)::int, sum(n) FROM (
  SELECT ST_Transform(geom, %(srid)s) AS q, n_{day} AS n FROM transit_station
  WHERE geom && ST_Transform(ST_Expand(ST_MakeEnvelope(%(minx)s, %(miny)s, %(maxx)s, %(maxy)s, 3857), %(size)s * 2), 4326)
    AND cat = ANY(%(modes)s) AND n_{day} > 0) s
GROUP BY 1, 2
"""


@lru_cache(maxsize=1024)
def grid_json(feed: str, z: int, x: int, y: int, day: str, modes: tuple, per: str, want: int, srid: int, table: str | None, cell: int) -> bytes:
    box, res, size = population.tile_frame(z, x, y, cell, want)
    with transit_db() as conn:
        deps = {(a, b): float(v) for a, b, v in conn.execute(GRID_QUERY.format(day=day), dict(box, size=size, srid=srid, modes=list(modes))).fetchall()}
    return roads.grid_finish(deps, box, res, size, per, srid, table, PER_PEOPLE)


@router.get("/grid/{z}/{x}/{y}")
def grid_tile(z: int, x: int, y: int, day: str = "wd", modes: str = "", per: str = "area", size: int = 0):
    if not 0 <= z <= 22 or not (0 <= x < 2 ** z and 0 <= y < 2 ** z):
        raise HTTPException(404, "No such tile")
    if per not in roads.PER:
        raise HTTPException(400, "per must be total, area or person")
    srid, table = roads.grid_srid()
    # squares: multiples of the population cells (per person) or of 500 m (stations are points: smaller squares are empty)
    cell = population.grid().cell if per == "person" and table else 500
    if per == "person":
        size = max(size, 1000)      # smaller squares: a few people make huge numbers
    return population.tile_response(grid_json(active_transit(), z, x, y, day, tuple(_filters(day, modes)), per,
                                              min(size, 100000), srid, table if per == "person" else None, cell))


# ---------- per administrative area (the areas of the Split view, table road_view of the map database) ----------
STATIONS_QUERY = """
SELECT ST_X(p), ST_Y(p), n FROM (
  SELECT ST_Transform(geom, 3857) AS p, sum(n_{day}) AS n FROM transit_station
  WHERE geom && ST_Transform(ST_MakeEnvelope(%s, %s, %s, %s, 3857), 4326) AND cat = ANY(%s) AND n_{day} > 0
  GROUP BY geom) s
"""
AREAS_QUERY = """
WITH s AS (SELECT ST_SetSRID(ST_MakePoint(x, y), 3857) AS p, n FROM unnest(%s::float8[], %s::float8[], %s::float8[]) AS u(x, y, n))
SELECT v.id, sum(s.n) FROM road_view v JOIN s ON v.way && s.p AND ST_Contains(v.way, s.p)
WHERE v.id = ANY(%s) GROUP BY v.id
"""


@router.get("/admin")
def admin(level: int, minx: float, miny: float, maxx: float, maxy: float, day: str = "wd", modes: str = ""):
    """{area id: departures} for the areas of a Split view level in the map view (ids as in /roads/admin)."""
    mods = _filters(day, modes)
    if not roads.has_cells():
        raise HTTPException(409, "Roads not prepared – Settings → Map → Prepare analyses")
    with db() as conn:            # the areas in view and how far they reach
        rows = conn.execute("SELECT id, ST_XMin(way), ST_YMin(way), ST_XMax(way), ST_YMax(way) FROM road_view "
                            "WHERE view = %s AND way && ST_MakeEnvelope(%s, %s, %s, %s, 3857)", (level, minx, miny, maxx, maxy)).fetchall()
    if not rows:
        return {}
    ext = (min(r[1] for r in rows), min(r[2] for r in rows), max(r[3] for r in rows), max(r[4] for r in rows))
    with transit_db() as conn:
        st = conn.execute(STATIONS_QUERY.format(day=day), (*ext, mods)).fetchall()
    if not st:
        return {}
    with db() as conn:
        got = conn.execute(AREAS_QUERY, ([s[0] for s in st], [s[1] for s in st], [float(s[2]) for s in st], [r[0] for r in rows])).fetchall()
    return {i: round(float(n)) for i, n in got}


# ---------- one area: the map view, a drawn shape or a named area ----------
class Area(BaseModel):
    geometry: dict
    day: str = "wd"
    modes: str = ""


STATS_QUERY = """
WITH a AS (SELECT ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326) AS g)
SELECT cat, count(DISTINCT geom), sum(n_{day}) FROM transit_station, a
WHERE geom && a.g AND ST_Intersects(geom, a.g) AND cat = ANY(%s) AND n_{day} > 0
GROUP BY cat
"""


@router.post("/stats")
def stats(req: Area):
    mods = _filters(req.day, req.modes)
    geom = json.dumps(req.geometry)
    try:
        with transit_db() as conn:
            rows = conn.execute(STATS_QUERY.format(day=req.day), (geom, mods)).fetchall()
            km2 = conn.execute("SELECT ST_Area(ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326)::geography) / 1e6", (geom,)).fetchone()[0]
            stations = conn.execute("SELECT count(DISTINCT geom) FROM transit_station, (SELECT ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326) AS g) a "
                                    f"WHERE geom && a.g AND ST_Intersects(geom, a.g) AND cat = ANY(%s) AND n_{req.day} > 0", (geom, mods)).fetchone()[0]
    except psycopg.errors.InternalError_ as e:
        raise HTTPException(400, f"Query failed: {e.diag.message_primary or e}")
    try:
        pop = population.area_stats(req.geometry)
        people = pop["population"] if pop["cells"] else None
    except HTTPException:
        people = None
    return {"area_km2": round(km2, 3), "population": people, "stations": stations,
            "modes": sorted(({"cat": c, "departures": int(n)} for c, _, n in rows), key=lambda m: -m["departures"]),
            "departures": int(sum(n for _, _, n in rows))}


def clear_caches():
    grid_json.cache_clear()
