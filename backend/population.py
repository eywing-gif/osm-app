"""Population statistics for an area from the active population grid (database "population", table pop_<id>).
Any grid works: square cells in a metric coordinate system (catalog.POPULATION); age / housing values may be NULL.

A cell that is cut by the area's border contributes the share of its square that lies inside
(area weighting: the population is assumed to be spread evenly over the cell).
Cells whose centre is more than half a cell diagonal inside the area are fully
inside, so only the cells along the border need a real intersection – that keeps big areas fast.
"""
import json
import math
from functools import lru_cache

import psycopg
from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel

from . import catalog
from .common import DB_POP, dsn, load_state

router = APIRouter()

WORLD_BOX = (-180, -85, 180, 85)


class Grid:
    """A grid dataset: a population grid (pop_<id>) or a built-up grid (built_<id>)."""
    def __init__(self, ds: str):
        info = catalog.POPULATION.get(ds) or catalog.BUILT[ds]
        self.ds, self.table, self.srid, self.cell = ds, f"{'pop' if ds in catalog.POPULATION else 'built'}_{ds}", info["srid"], info["cell"]
        self.half_diagonal = self.cell * 0.71         # a cell centre this far inside the area means the whole cell is inside
        self.box = info.get("bbox") or WORLD_BOX      # clip the area to what the grid covers first (keeps the projection happy)


def grid(kind: str = "population") -> Grid:
    """The active grid of a kind ("population" or "built")."""
    ds = load_state().get(kind, {}).get("active")
    if ds not in (catalog.POPULATION if kind == "population" else catalog.BUILT):
        raise HTTPException(503, f"No {kind} data – open Settings and download a dataset.")
    return Grid(ds)


def connect():
    return psycopg.connect(dsn(DB_POP))


def clear_caches():
    grid_tile_json.cache_clear()
    per_person_json.cache_clear()

QUERY = """
WITH shape AS (
  SELECT ST_SetSRID(ST_GeomFromGeoJSON(%(geom)s), 4326) AS g4
), area AS (
  SELECT ST_Area(g4::geography) / 1e6 AS km2,
         ST_SimplifyPreserveTopology(ST_Transform(ST_Intersection(g4, ST_MakeEnvelope(%(w)s, %(s)s, %(e)s, %(n)s, 4326)), {srid}), 5) AS g
  FROM shape
), zone AS (
  SELECT km2, g, ST_Buffer(g, -%(h)s) AS core, ST_Buffer(g, %(h)s) AS reach FROM area
), cells AS (
  -- weight = share of the 100 m square inside the area
  SELECT c.*, 1.0 AS w FROM {table} c, zone z
   WHERE c.geom && z.g AND ST_Within(c.geom, z.core)
  UNION ALL
  SELECT c.*, ST_Area(ST_Intersection(ST_MakeEnvelope(c.cx - %(half)s, c.cy - %(half)s, c.cx + %(half)s, c.cy + %(half)s, {srid}), z.g)) / %(cell2)s
    FROM {table} c, zone z
   WHERE c.geom && ST_Expand(z.g, %(h)s) AND ST_Intersects(c.geom, z.reach) AND NOT ST_Within(c.geom, z.core)
)
SELECT (SELECT km2 FROM zone),
       count(*), sum(w * pop),
       sum(w) FILTER (WHERE pop > 0),
       -- the other values are averages / shares per cell: weight them by the number of people
       sum(w * pop * avg_age)     / nullif(sum(w * pop) FILTER (WHERE avg_age IS NOT NULL), 0),
       sum(w * pop * pct_under18) / nullif(sum(w * pop) FILTER (WHERE pct_under18 IS NOT NULL), 0),
       sum(w * pop * pct_over65)  / nullif(sum(w * pop) FILTER (WHERE pct_over65 IS NOT NULL), 0),
       sum(w * pop * pct_foreign) / nullif(sum(w * pop) FILTER (WHERE pct_foreign IS NOT NULL), 0),
       -- household size: people / households (households = people / size)
       sum(w * pop) FILTER (WHERE hh_size IS NOT NULL)
         / nullif(sum(w * pop / hh_size) FILTER (WHERE hh_size IS NOT NULL), 0),
       sum(w * pop * rent_sqm)    / nullif(sum(w * pop) FILTER (WHERE rent_sqm IS NOT NULL), 0),
       sum(w * pop * pct_owner)   / nullif(sum(w * pop) FILTER (WHERE pct_owner IS NOT NULL), 0),
       sum(w * pop * pct_vacant)  / nullif(sum(w * pop) FILTER (WHERE pct_vacant IS NOT NULL), 0),
       sum(w * pop * living_space) / nullif(sum(w * pop) FILTER (WHERE living_space IS NOT NULL), 0),
       sum(w) FILTER (WHERE w < 0.9999)
FROM cells
"""


class PopulationRequest(BaseModel):
    geometry: dict          # GeoJSON (Multi)Polygon in WGS84


def r(v, digits=1):
    return None if v is None else round(float(v), digits)


def area_stats(geometry: dict) -> dict:
    """Inhabitants etc. inside a (Multi)Polygon, from the active grid."""
    g = grid()
    w, s, e, n = g.box
    params = dict(geom=json.dumps(geometry), w=w, s=s, e=e, n=n, h=g.half_diagonal, half=g.cell / 2, cell2=g.cell ** 2)
    try:
        with connect() as conn:
            row = conn.execute(QUERY.format(table=g.table, srid=g.srid), params).fetchone()
    except psycopg.errors.InternalError_ as err:
        raise HTTPException(400, f"Query failed: {err.diag.message_primary or err}")
    (km2, cells, pop, populated, age, under18, over65, foreign, hh, rent, owner, vacant, space, border) = row
    pop = float(pop or 0)
    populated_km2 = float(populated or 0) * g.cell ** 2 / 1e6
    return {
        "area_km2": r(km2, 3), "population": round(pop),
        "density": r(pop / km2) if km2 and pop else None,
        "populated_km2": r(populated_km2, 2),
        # density where people actually live (without fields, forests, water …)
        "density_populated": r(pop / populated_km2) if populated_km2 else None,
        "avg_age": r(age), "pct_under18": r(under18), "pct_over65": r(over65), "pct_foreign": r(foreign),
        "hh_size": r(hh, 2), "rent_sqm": r(rent, 2), "pct_owner": r(owner), "pct_vacant": r(vacant),
        "living_space": r(space), "cells": cells or 0, "border_cells": round(float(border or 0)),
        "dataset": f"{catalog.POPULATION[g.ds]['name']} · {catalog.POPULATION[g.ds]['source']}", "cell_m": g.cell,
    }


@router.post("/population")
def population(req: PopulationRequest):
    if req.geometry.get("type") not in ("Polygon", "MultiPolygon"):
        raise HTTPException(400, "Area must be a polygon.")
    return area_stats(req.geometry)


# ---------- map layer: population density as tiles ----------
# The browser draws the tiles itself (like the slope layer), so we only send the numbers:
# one entry [x, y, inhabitants per km²] per cell, with x/y being the cell centre in tile pixels.
# Zoomed out, the cells would be smaller than a pixel, so they are summed up to coarser squares
# (cell * 2^k, on one global grid, so neighbouring tiles line up). `size` asks for bigger squares (500 m, 1 km …):
# the cells are summed, the edges of the squares stay on the edges of the grid.

WORLD = 40075016.685578488      # circumference in EPSG:3857 metres
MIN_CELL_PX = 3                 # coarsen until a drawn cell is at least this wide

# {table} {srid}: the active grid. size: edge of the (coarsened) squares in grid metres
TILE_QUERY = """
WITH box AS (SELECT ST_Expand(ST_Envelope(ST_Transform(ST_MakeEnvelope(%(minx)s, %(miny)s, %(maxx)s, %(maxy)s, 3857), {srid})), %(size)s) AS b),
grp AS (
  SELECT floor(c.cx::float8 / %(size)s)::int AS gx, floor(c.cy::float8 / %(size)s)::int AS gy, sum(c.{col}) AS pop
  FROM {table} c, box WHERE c.geom && box.b GROUP BY 1, 2
)
SELECT ST_X(p), ST_Y(p), pop FROM (
  SELECT ST_Transform(ST_SetSRID(ST_MakePoint(gx * %(size)s + %(size)s / 2.0, gy * %(size)s + %(size)s / 2.0), {srid}), 3857) AS p, pop
  FROM grp) s
"""

# where one cell edge (east / north in the grid) ends up on the screen, around the tile centre
AXES_QUERY = """
WITH q AS (SELECT ST_Transform(ST_SetSRID(ST_MakePoint(%(mx)s, %(my)s), 3857), {srid}) AS p),
pts AS (SELECT ST_X(p) AS x, ST_Y(p) AS y FROM q),
t AS (SELECT ST_Transform(ST_SetSRID(ST_MakePoint(x + dx, y + dy), {srid}), 3857) AS g
      FROM pts, (VALUES (0, 0, 0), (1, %(size)s, 0), (2, 0, %(size)s)) AS d(i, dx, dy) ORDER BY i)
SELECT array_agg(ST_X(g)), array_agg(ST_Y(g)) FROM t
"""


def tile_frame(z: int, x: int, y: int, cell: int, want: int):
    """Tile bounds in EPSG:3857, metres per pixel and the square size (a multiple of the cell, coarsened until a square is at least MIN_CELL_PX wide)."""
    res = WORLD / (256 * 2 ** z)
    minx, maxy = -WORLD / 2 + x * 256 * res, WORLD / 2 - y * 256 * res
    maxx, miny = minx + 256 * res, maxy - 256 * res
    lat = math.atan(math.sinh(math.pi * (maxy + miny) / WORLD))   # tile centre latitude (radians)
    base = want if want >= cell and want % cell == 0 else cell
    size = base * 2 ** max(0, math.ceil(math.log2(MIN_CELL_PX * res * math.cos(lat) / base)))
    return dict(minx=minx, miny=miny, maxx=maxx, maxy=maxy), res, size


def tile_json(conn, rows, srid, box, res, size, scale) -> bytes:
    """Cells [x, y, value] in tile pixels plus the screen vectors of one cell edge.
    The grid is slightly rotated against the web map: "ex" / "ey" (pixels, y down) let the client draw true squares."""
    xs, ys = conn.execute(AXES_QUERY.format(srid=srid), dict(mx=(box["minx"] + box["maxx"]) / 2, my=(box["miny"] + box["maxy"]) / 2, size=size)).fetchone()
    cells = [[round((px - box["minx"]) / res, 1), round((box["maxy"] - py) / res, 1), scale(*v)] for px, py, *v in rows]
    axes = {"ex": [round((xs[1] - xs[0]) / res, 3), round(-(ys[1] - ys[0]) / res, 3)],
            "ey": [round((xs[2] - xs[0]) / res, 3), round(-(ys[2] - ys[0]) / res, 3)]}
    return json.dumps({**axes, "cells": cells}, separators=(",", ":")).encode()


@lru_cache(maxsize=1024)
def grid_tile_json(ds: str, z: int, x: int, y: int, want: int = 0, col: str = "pop") -> bytes:
    """Cells of a tile: [x, y, value]. col pop: inhabitants per km²; built / nres: share of the square (%) built over."""
    g = Grid(ds)
    box, res, size = tile_frame(z, x, y, g.cell, want)
    k = 1e6 / size ** 2 if col == "pop" else 100 / size ** 2          # per km²  /  per cent of the square
    with connect() as conn:
        rows = conn.execute(TILE_QUERY.format(table=g.table, srid=g.srid, col=col), dict(box, size=size)).fetchall()
        return tile_json(conn, rows, g.srid, box, res, size,
                         (lambda v: round(v * k)) if col == "pop" else (lambda v: round(min(v * k, 100), 1)))


# built-up area per inhabitant: both grids summed onto the same squares of the built-up grid (>= 1 km, so a census square is never alone)
PER_PERSON_QUERY = """
WITH bx AS (SELECT ST_Transform(ST_MakeEnvelope(%(minx)s, %(miny)s, %(maxx)s, %(maxy)s, 3857), {bsrid}) AS e),
b AS (
  SELECT floor(c.cx::float8 / %(size)s)::int AS gx, floor(c.cy::float8 / %(size)s)::int AS gy, sum(c.built) AS m2
  FROM {btable} c, bx WHERE c.geom && ST_Expand(ST_Envelope(bx.e), %(size)s) GROUP BY 1, 2
), p AS (
  SELECT floor(ST_X(q) / %(size)s)::int AS gx, floor(ST_Y(q) / %(size)s)::int AS gy, sum(pop) AS pop FROM (
    SELECT {point} AS q, c.pop FROM {ptable} c, bx
    WHERE c.geom && ST_Expand(ST_Envelope(ST_Transform(bx.e, {psrid})), %(size)s)) s
  GROUP BY 1, 2
)
SELECT ST_X(pt), ST_Y(pt), m2, coalesce(pop, 0) FROM (
  SELECT ST_Transform(ST_SetSRID(ST_MakePoint(gx * %(size)s + %(size)s / 2.0, gy * %(size)s + %(size)s / 2.0), {bsrid}), 3857) AS pt, m2, pop
  FROM b LEFT JOIN p USING (gx, gy)
  -- no inhabitants: only next to squares that have some (else it is outside the population data, not empty)
  WHERE pop IS NOT NULL OR EXISTS (SELECT 1 FROM p q WHERE abs(q.gx - b.gx) <= 1 AND abs(q.gy - b.gy) <= 1)) s
"""
PER_PERSON_NOBODY = 2000  # sent for squares where nobody lives (real values stay below)


def _pp_args(b: Grid, p: Grid) -> dict:
    point = "ST_MakePoint(c.cx, c.cy)" if p.srid == b.srid else f"ST_Transform(ST_SetSRID(ST_MakePoint(c.cx, c.cy), {p.srid}), {b.srid})"
    return dict(bsrid=b.srid, btable=b.table, psrid=p.srid, ptable=p.table, point=point)


@lru_cache(maxsize=512)
def per_person_json(bds: str, pds: str, z: int, x: int, y: int) -> bytes:
    """Cells of a tile: [x, y, built-up m² per inhabitant] (PER_PERSON_NOBODY where nobody lives)."""
    b, p = Grid(bds), Grid(pds)
    box, res, size = tile_frame(z, x, y, 1000, 0)
    query = PER_PERSON_QUERY.format(**_pp_args(b, p))
    with connect() as conn:
        rows = conn.execute(query, dict(box, size=size)).fetchall()
        return tile_json(conn, rows, b.srid, box, res, size,
                         lambda m2, pop: round(min(m2 / pop, PER_PERSON_NOBODY - 1)) if pop >= 1 else PER_PERSON_NOBODY)


@router.get("/density/{z}/{x}/{y}")
def density_tile(z: int, x: int, y: int, size: int = 0):
    if not 0 <= z <= 22 or not (0 <= x < 2 ** z and 0 <= y < 2 ** z):
        raise HTTPException(404, "No such tile")
    return tile_response(grid_tile_json(grid().ds, z, x, y, min(size, 100000), "pop"))


@router.get("/built/{z}/{x}/{y}")
def built_tile(z: int, x: int, y: int, size: int = 0, col: str = "built"):
    if col not in ("built", "nres"):
        raise HTTPException(400, "col must be built or nres")
    if not 0 <= z <= 22 or not (0 <= x < 2 ** z and 0 <= y < 2 ** z):
        raise HTTPException(404, "No such tile")
    return tile_response(grid_tile_json(grid("built").ds, z, x, y, min(size, 100000), col))


@router.get("/perperson/{z}/{x}/{y}")
def per_person_tile(z: int, x: int, y: int):
    if not 0 <= z <= 22 or not (0 <= x < 2 ** z and 0 <= y < 2 ** z):
        raise HTTPException(404, "No such tile")
    return tile_response(per_person_json(grid("built").ds, grid().ds, z, x, y))


def tile_response(body):
    return Response(body, media_type="application/json", headers={"Cache-Control": "public, max-age=86400"})


def built_at(lon: float, lat: float) -> dict | None:
    """Built-up area of the 1 km square under a point: m² per inhabitant and share of the square. None without data."""
    try:
        b, p = grid("built"), grid()
        box = dict(zip(("minx", "miny", "maxx", "maxy"), _merc(lon, lat)))
        with connect() as conn:
            rows = conn.execute(PER_PERSON_QUERY.format(**_pp_args(b, p)), dict(box, size=1000)).fetchall()
    except (HTTPException, psycopg.Error):
        return None
    if not rows:
        return None
    mx, my = _merc(lon, lat)[0] + 1, _merc(lon, lat)[1] + 1
    x, y, m2, pop = min(rows, key=lambda r: (r[0] - mx) ** 2 + (r[1] - my) ** 2)
    return {"per_person": round(m2 / pop) if pop >= 1 else None, "share": round(m2 / 1e4, 1), "size_m": 1000}


def _merc(lon, lat):
    x = lon * WORLD / 360
    y = math.log(math.tan(math.pi / 4 + math.radians(lat) / 2)) * WORLD / (2 * math.pi)
    return x - 1, y - 1, x + 1, y + 1


# ---------- the grid cell at a point (point info) ----------
CELL_QUERY = """
WITH q AS (SELECT ST_Transform(ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326), {srid}) AS p)
SELECT c.cx, c.cy, c.pop, c.avg_age, c.pct_under18, c.pct_over65, c.pct_foreign, c.hh_size, c.rent_sqm, c.pct_owner, c.pct_vacant, c.living_space,
       ST_AsGeoJSON(ST_Transform(ST_MakeEnvelope(c.cx - %(h)s, c.cy - %(h)s, c.cx + %(h)s, c.cy + %(h)s, {srid}), 4326), 6)
FROM {table} c, q
WHERE c.geom && ST_Expand(q.p, %(h)s) AND abs(c.cx - ST_X(q.p)) <= %(h)s AND abs(c.cy - ST_Y(q.p)) <= %(h)s
ORDER BY ST_Distance(c.geom, q.p) LIMIT 1
"""
STAT_NAMES = ("avg_age", "pct_under18", "pct_over65", "pct_foreign", "hh_size", "rent_sqm", "pct_owner", "pct_vacant", "living_space")


def cell_at(lon: float, lat: float) -> dict | None:
    """The grid cell under a point: inhabitants, density, the cell's own values. None if there is no grid or no coverage."""
    try:
        g = grid()
        w, s, e, n = g.box
        if not (w <= lon <= e and s <= lat <= n):
            return None
        with connect() as conn:
            row = conn.execute(CELL_QUERY.format(table=g.table, srid=g.srid), dict(lon=lon, lat=lat, h=g.cell / 2)).fetchone()
    except (HTTPException, psycopg.Error):
        return None
    info = catalog.POPULATION[g.ds]
    out = {"dataset": f"{info['name']} · {info['source']}", "cell_m": g.cell}
    if row is None:                      # inside the coverage, but no row: nobody lives there
        return {**out, "population": 0, "density": 0, "stats": {}, "geometry": None}
    pop = float(row[2])
    return {**out, "population": round(pop), "density": round(pop / (g.cell ** 2 / 1e6)),
            "stats": {k: r(v, 1) for k, v in zip(STAT_NAMES, row[3:12]) if v is not None}, "geometry": json.loads(row[12])}


# ---------- catchment of a stop: inhabitants within walking distance ----------
WALK_KMH = 4.5          # assumed walking speed: sources range 4.2 (NWL) - 4.3 (Raeppel) - 5.0 km/h (Stuttgart, door2door)
DETOUR = 1.2            # real paths are longer than the straight line


@router.get("/catchment")
def catchment(lon: float, lat: float, minutes: float = 5, speed_kmh: float = WALK_KMH, detour: float = DETOUR):
    if not (0 < minutes <= 60 and 0 < speed_kmh <= 10 and 1 <= detour <= 3):
        raise HTTPException(400, "Invalid parameters.")
    path_m = speed_kmh * 1000 / 60 * minutes            # distance that is walked
    radius = path_m / detour                            # straight-line radius
    dlat = radius / 111320
    dlon = radius / (111320 * math.cos(math.radians(lat)))
    ring = [[lon + dlon * math.cos(a), lat + dlat * math.sin(a)]
            for a in (2 * math.pi * i / 96 for i in range(96))]
    ring.append(ring[0])
    res = area_stats({"type": "Polygon", "coordinates": [ring]})
    return {"minutes": minutes, "speed_kmh": speed_kmh, "detour": detour, "walk_m": round(path_m),
            "radius_m": round(radius), "population": res["population"], "density": res["density"],
            "dataset": res["dataset"]}
