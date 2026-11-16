"""Amenities: what a place offers within walking distance, and several scores for it (table amenity_poi, pipeline/amenities.py).

Scores of a location (distances: straight line × DETOUR = way on foot, WALK_KMH):
  amenity score  0–100, Walk-Score method: per group the nearest places count with a share of the group weight
                 (amenity_catalog), fully within 400 m (5 min), less with distance, nothing beyond 2.4 km (30 min);
                 grade 1–6 from the Walk Score bands (90+ = 1, 70 = 2, 50 = 3, 25 = 4, 10 = 5, 0 = 6).
  15-min groups  how many of the 10 groups have a place within 15 minutes on foot (15-minute city, Moreno).
  proximity time mean over the groups of the walking time to the 5 nearest places (Bruno et al. 2024: 20 nearest,
                 routed; here 5, straight × detour, at most 60 min).
  diversity      evenness (Shannon / Pielou, 0–1) of the groups within 10 minutes.
  local supply   BBSR "Nahversorgung": supermarket, pharmacy, doctor, school, public transport stop within 1 km (straight).
"""
import json
import math
from functools import lru_cache

import numpy as np
import psycopg
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from scipy.spatial import cKDTree

from . import population, roads
from .amenity_catalog import GREEN, GROUP_IDS, GROUPS, catalog
from .common import db, transit_db

router = APIRouter(prefix="/amenities")

WALK_M_MIN = population.WALK_KMH * 1000 / 60      # metres per minute on foot
DETOUR = population.DETOUR
FULL_M, ZERO_M = 400, 2400                          # walking metres: full points / no points (Walk Score: 5 / 30 min)
MAX_M = 60 * WALK_M_MIN                             # proximity time: at most 60 minutes
BBSR_M = 1000                                       # local supply: within 1 km straight
LOCAL = [("Supermarket", 0, "supermarket"), ("Pharmacy", 3, "pharmacy"), ("Doctor", 3, "doctor"), ("School", 5, "school")]
WEIGHTS = np.array([g["weight"] for g in GROUPS], dtype=float)
GRADES = [(90, 1.0), (70, 2.0), (50, 3.0), (25, 4.0), (10, 5.0), (0, 6.0)]


def grade(score: float) -> float:
    """School grade from the score: the Walk Score bands, linear in between (100 → 1.0, 0 → 6.0)."""
    pts = [(100, 1.0)] + GRADES
    for (s1, g1), (s0, g0) in zip(pts, pts[1:]):
        if score >= s0:
            return round(g0 + (g1 - g0) * (score - s0) / (s1 - s0), 1) if s1 > s0 else g1
    return 6.0


def decay(walk_m):
    return np.clip(1 - (np.asarray(walk_m) - FULL_M) / (ZERO_M - FULL_M), 0, 1) ** 2


# ---------- the places around a location: one tree per group (metres in a local plane) ----------
class Places:
    def __init__(self, minx, miny, maxx, maxy, names: bool = False):
        self.k = math.cos(math.atan(math.sinh((miny + maxy) / 2 / 6378137)))      # EPSG:3857 → metres
        m = (MAX_M / DETOUR) / self.k                                               # look this far around
        with db() as conn:
            rows = conn.execute(f"SELECT g, s, ST_X(way), ST_Y(way){', name' if names else ''} FROM amenity_poi "
                                "WHERE way && ST_MakeEnvelope(%s, %s, %s, %s, 3857)", (minx - m, miny - m, maxx + m, maxy + m)).fetchall()
            edges = conn.execute("SELECT ST_X(way), ST_Y(way) FROM amenity_green_pt WHERE way && ST_MakeEnvelope(%s, %s, %s, %s, 3857)",
                                 (minx - m, miny - m, maxx + m, maxy + m)).fetchall()
        self.rows = rows
        arr = np.array([r[:4] for r in rows], dtype=float).reshape(-1, 4)
        self.trees, self.idx = [], []
        for gi in range(len(GROUPS)):
            sel = np.nonzero(arr[:, 0] == gi)[0]
            self.idx.append(sel)
            self.trees.append(cKDTree(arr[sel, 2:4] * self.k) if len(sel) else None)
        e = np.array(edges, dtype=float).reshape(-1, 2)
        self.green_edges = cKDTree(e * self.k) if len(e) else None
        self.subs = arr[:, 1]
        self._sub_trees = {}

    def sub_tree(self, gi: int, sub: str):
        """Tree of one type of place (e.g. the supermarkets of the daily needs), or None."""
        if (gi, sub) not in self._sub_trees:
            si = [s[0] for s in GROUPS[gi]["subs"]].index(sub)
            sel = self.idx[gi][self.subs[self.idx[gi]] == si] if len(self.idx[gi]) else []
            self._sub_trees[(gi, sub)] = cKDTree(np.array([[self.rows[r][2], self.rows[r][3]] for r in sel]) * self.k) if len(sel) else None
        return self._sub_trees[(gi, sub)]

    def local_count(self, pts) -> np.ndarray:
        """BBSR local supply: how many of supermarket, pharmacy, doctor, school are within 1 km (straight)."""
        n = np.zeros(len(pts), dtype=int)
        for _, gi, sub in LOCAL:
            tree = self.sub_tree(gi, sub)
            if tree is not None:
                d, _ = tree.query(pts * self.k, k=1, distance_upper_bound=BBSR_M)
                n += np.isfinite(d)
        return n

    def diversity(self, pts) -> np.ndarray:
        """Evenness of the groups within 10 minutes on foot (0–1)."""
        counts = np.array([self.counts(pts, gi, 10 * WALK_M_MIN) for gi in range(len(GROUPS))]).T
        return diversity(counts)

    def nearest(self, pts, gi: int, n: int):
        """Walking metres to the n nearest places of group gi (inf where there are fewer), and their row numbers."""
        tree = self.green_edges if gi == GREEN else self.trees[gi]
        if tree is None:
            return np.full((len(pts), n), np.inf), np.full((len(pts), n), -1)
        d, i = tree.query(pts * self.k, k=n, distance_upper_bound=MAX_M / DETOUR)
        d, i = d.reshape(len(pts), n), i.reshape(len(pts), n)
        rows = np.where(np.isfinite(d), self.idx[gi][np.minimum(i, len(self.idx[gi]) - 1)] if gi != GREEN else -1, -1)
        return d * DETOUR, rows

    def counts(self, pts, gi: int, walk_m: float):
        tree = self.trees[gi]
        if tree is None:
            return np.zeros(len(pts), dtype=int)
        return np.asarray(tree.query_ball_point(pts * self.k, walk_m / DETOUR, return_length=True))


def scores(pl: Places, pts: np.ndarray) -> dict:
    """The scores for many locations at once (pts: EPSG:3857 x / y)."""
    n = len(pts)
    weighted = np.zeros(n)
    within15 = np.zeros(n, dtype=int)
    prox = np.zeros(n)
    near = []
    for gi, g in enumerate(GROUPS):
        k = max(len(g["shares"]), 5)
        d, _ = pl.nearest(pts, gi, k)
        near.append(d[:, 0])
        weighted += g["weight"] * (decay(d[:, :len(g["shares"])]) * np.array(g["shares"])).sum(axis=1)
        within15 += d[:, 0] <= 15 * WALK_M_MIN
        prox += np.minimum(d[:, :5], MAX_M).mean(axis=1) / WALK_M_MIN
    score = 100 * weighted / WEIGHTS.sum()
    return {"score": score, "within15": within15, "proximity_min": prox / len(GROUPS), "nearest": np.array(near).T}


def diversity(counts: np.ndarray) -> np.ndarray:
    """Pielou evenness of the counts per group (rows: locations)."""
    tot = counts.sum(axis=1, keepdims=True)
    p = np.where(tot > 0, counts / np.maximum(tot, 1), 0)
    h = -(np.where(p > 0, p * np.log(np.where(p > 0, p, 1)), 0)).sum(axis=1)
    return h / math.log(len(GROUPS))


def to3857(lon, lat):
    return lon * 20037508.342789244 / 180, math.log(math.tan((90 + lat) * math.pi / 360)) * 6378137


# ---------- one location: the full evaluation ----------
@router.get("/at")
def at(lon: float, lat: float):
    if not (-180 <= lon <= 180 and -85 <= lat <= 85):
        raise HTTPException(400, "bad point")
    _need()
    x, y = to3857(lon, lat)
    pl = Places(x, y, x, y, names=True)
    pt = np.array([[x, y]])
    s = scores(pl, pt)
    groups, counts10 = [], []
    for gi, g in enumerate(GROUPS):
        d, rows = pl.nearest(pt, gi, 3)
        by_time = {f"min{t}": int(pl.counts(pt, gi, t * WALK_M_MIN)[0]) for t in (5, 10, 15)}
        counts10.append(by_time["min10"])
        nearest = []
        for dist, r in zip(d[0], rows[0]):
            if np.isfinite(dist):
                if r >= 0:
                    row = pl.rows[r]
                    nearest.append({"name": row[4], "type": g["subs"][int(row[1])][1], "walk_m": round(float(dist)), "walk_min": round(float(dist) / WALK_M_MIN, 1)})
                else:
                    nearest.append({"name": None, "type": "Green space (edge)", "walk_m": round(float(dist)), "walk_min": round(float(dist) / WALK_M_MIN, 1)})
                    break
        subs = {}           # places per type within 10 minutes
        if pl.trees[gi] is not None:
            for j in pl.trees[gi].query_ball_point(pt[0] * pl.k, 10 * WALK_M_MIN / DETOUR):
                name = g["subs"][int(pl.subs[pl.idx[gi][j]])][1]
                subs[name] = subs.get(name, 0) + 1
        groups.append({"id": g["id"], "name": g["name"], "weight": g["weight"], "nearest": nearest, **by_time,
                       "within15": bool(np.isfinite(d[0][0]) and d[0][0] <= 15 * WALK_M_MIN),
                       "subs": sorted(({"name": k, "n": v} for k, v in subs.items()), key=lambda v: -v["n"])})
    # each group's part of the score (the same formula as scores(), for the bars in the sidebar)
    for gi, g in enumerate(GROUPS):
        d, _ = pl.nearest(pt, gi, len(g["shares"]))
        groups[gi]["points"] = round(float(g["weight"] * (decay(d[0]) * np.array(g["shares"])).sum()), 2)
    local = local_supply(pl, pt, lon, lat)
    sc = float(s["score"][0])
    return {"score": round(sc), "grade": grade(sc), "within15": int(s["within15"][0]), "groups_total": len(GROUPS),
            "proximity_min": round(float(s["proximity_min"][0]), 1), "diversity": round(float(diversity(np.array([counts10]))[0]), 2),
            "local": local, "groups": groups, "weight_total": float(WEIGHTS.sum())}


def local_supply(pl: Places, pt, lon, lat) -> list[dict]:
    """BBSR local supply: the nearest supermarket, pharmacy, doctor, school, transit stop (straight distance)."""
    out = []
    for label, gi, sub in LOCAL:
        tree = pl.sub_tree(gi, sub)
        d = float(tree.query(pt[0] * pl.k, k=1)[0]) if tree is not None else None
        out.append({"name": label, "m": None if d is None or d > MAX_M else round(d)})
    try:
        with transit_db() as conn:
            d = conn.execute("SELECT ST_Distance(geom::geography, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography) FROM transit_station "
                             "ORDER BY geom <-> ST_SetSRID(ST_MakePoint(%s, %s), 4326) LIMIT 1", (lon, lat, lon, lat)).fetchone()
        out.append({"name": "Transit stop", "m": round(d[0]) if d else None})
    except (HTTPException, psycopg.Error):
        out.append({"name": "Transit stop", "m": None})
    for o in out:
        o["ok"] = o["m"] is not None and o["m"] <= BBSR_M
    return out


def _need():
    with db() as conn:
        if not conn.execute("SELECT 1 FROM pg_tables WHERE tablename = 'amenity_green_pt'").fetchone():
            raise HTTPException(409, "Amenities not prepared – Settings → Map → Prepare analyses")


def prepared() -> bool:
    try:
        _need()
        return True
    except (HTTPException, psycopg.Error):
        return False


# ---------- map grid: score per square (from 100 m) or the places in each square ----------
METRICS = ("score", "within15", "proximity", "diversity", "local", "total", "area", "person")
CELLS_QUERY = """
SELECT gx, gy FROM (SELECT ST_Envelope(ST_Transform(ST_MakeEnvelope(%(minx)s, %(miny)s, %(maxx)s, %(maxy)s, 3857), %(srid)s)) AS b) e,
  generate_series(floor(ST_XMin(b) / %(size)s)::int, floor(ST_XMax(b) / %(size)s)::int) gx,
  generate_series(floor(ST_YMin(b) / %(size)s)::int, floor(ST_YMax(b) / %(size)s)::int) gy
"""
COUNT_QUERY = """
SELECT floor(ST_X(q) / %(size)s)::int, floor(ST_Y(q) / %(size)s)::int, count(*) FROM (
  SELECT ST_Transform(way, %(srid)s) AS q FROM amenity_poi
  WHERE way && ST_Expand(ST_MakeEnvelope(%(minx)s, %(miny)s, %(maxx)s, %(maxy)s, 3857), %(size)s * 2) AND g = ANY(%(groups)s)) s
GROUP BY 1, 2
"""


@lru_cache(maxsize=1024)
def grid_json(z: int, x: int, y: int, metric: str, groups: tuple, want: int, srid: int, table: str | None, cell: int) -> bytes:
    box, res, size = population.tile_frame(z, x, y, cell, want)
    p = dict(box, size=size, srid=srid, groups=list(groups))
    if metric in ("total", "area", "person"):
        with db() as conn:
            totals = {(a, b): float(v) for a, b, v in conn.execute(COUNT_QUERY, p).fetchall()}
        return roads.grid_finish(totals, box, res, size, metric, srid, table, 1000)
    with db() as conn:
        keys = conn.execute(CELLS_QUERY, p).fetchall()
        pts = conn.execute(roads.CENTRES_QUERY, dict(size=size, srid=srid, xs=[k[0] for k in keys], ys=[k[1] for k in keys])).fetchall()
    xy = np.array(pts, dtype=float).reshape(-1, 2)
    pl = Places(box["minx"], box["miny"], box["maxx"], box["maxy"])
    if metric == "diversity":
        vals = pl.diversity(xy) * 100                       # per cent, so the squares show whole numbers
    elif metric == "local":
        vals = pl.local_count(xy).astype(float)
    else:
        s = scores(pl, xy)
        vals = {"score": s["score"], "within15": s["within15"].astype(float), "proximity": s["proximity_min"]}[metric]
    keep = (lambda v: v < 59.5) if metric == "proximity" else (lambda v: v > 0)     # nothing near: leave the square empty
    rows = [(px, py, v) for (px, py), v in zip(pts, vals.tolist()) if keep(v)]
    with db() as conn:
        return population.tile_json(conn, rows, srid, box, res, size, lambda v: round(v))


@router.get("/grid/{z}/{x}/{y}")
def grid_tile(z: int, x: int, y: int, metric: str = "score", groups: str = "", size: int = 0):
    if not 0 <= z <= 22 or not (0 <= x < 2 ** z and 0 <= y < 2 ** z):
        raise HTTPException(404, "No such tile")
    if metric not in METRICS:
        raise HTTPException(400, f"metric must be one of {METRICS}")
    _need()
    gs = tuple(sorted(GROUP_IDS.index(g) for g in groups.split(",") if g in GROUP_IDS)) or tuple(range(len(GROUPS)))
    srid, table = roads.grid_srid()
    cell = 100
    if metric in ("diversity", "proximity"):
        size = max(size, 200)          # more work per square: at least 200 m
    if metric == "person":
        cell, size = population.grid().cell if table else 1000, max(size, 1000)
    return population.tile_response(grid_json(z, x, y, metric, gs, min(size, 100000), srid, table if metric == "person" else None, cell))


# ---------- an area: places per group, and the scores of its inhabitants ----------
class Area(BaseModel):
    geometry: dict


COUNT_AREA = """
WITH a AS (SELECT ST_Transform(ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326), 3857) AS g)
SELECT p.g, p.s, count(*) FROM amenity_poi p, a WHERE p.way && a.g AND ST_Intersects(p.way, a.g) GROUP BY 1, 2
"""
PEOPLE_AREA = """
WITH a AS MATERIALIZED (SELECT ST_Transform(ST_SetSRID(ST_GeomFromGeoJSON(%(geom)s), 4326), {srid}) AS g)
SELECT floor(c.cx::float8 / %(s)s)::int, floor(c.cy::float8 / %(s)s)::int, sum(c.pop) FROM {table} c, a
WHERE ST_Intersects(a.g, c.geom) GROUP BY 1, 2
"""
MAX_SAMPLES = 6000


@router.post("/stats")
def stats(req: Area):
    _need()
    geom = json.dumps(req.geometry)
    with db() as conn:
        conn.execute("SET statement_timeout = '120s'")
        rows = conn.execute(COUNT_AREA, (geom,)).fetchall()
        km2 = conn.execute("SELECT ST_Area(ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326)::geography) / 1e6", (geom,)).fetchone()[0]
    groups = []
    for gi, g in enumerate(GROUPS):
        subs = {g["subs"][s][1]: n for gg, s, n in rows if gg == gi}
        groups.append({"id": g["id"], "name": g["name"], "n": sum(subs.values()),
                       "subs": sorted(({"name": k, "n": v} for k, v in subs.items()), key=lambda v: -v["n"])})
    out = {"area_km2": round(km2, 3), "groups": groups, "population": None, "people": None}
    try:
        grid = population.grid()
    except HTTPException:
        return out
    # the inhabitants: their squares (coarser for big areas), the scores at each square, weighted by the people
    s = max(200, grid.cell)
    while True:
        with population.connect() as conn:
            cells = conn.execute(PEOPLE_AREA.format(srid=grid.srid, table=grid.table), dict(geom=geom, s=s)).fetchall()
        if len(cells) <= MAX_SAMPLES:
            break
        s *= 2
    cells = [c for c in cells if c[2] and c[2] > 0]
    if not cells:
        return out
    with population.connect() as conn:
        pts = np.array(conn.execute(roads.CENTRES_QUERY, dict(size=s, srid=grid.srid, xs=[c[0] for c in cells], ys=[c[1] for c in cells])).fetchall(), dtype=float)
    w = np.array([float(c[2]) for c in cells])
    pl = Places(pts[:, 0].min(), pts[:, 1].min(), pts[:, 0].max(), pts[:, 1].max())
    sc = scores(pl, pts)
    local = {}
    # local supply per inhabitant: straight distance to the nearest supermarket / pharmacy / doctor / school
    for label, gi, sub in LOCAL:
        tree = pl.sub_tree(gi, sub)
        if tree is not None:
            d, _ = tree.query(pts * pl.k, k=1)
            local[label] = round(float((w * (d <= BBSR_M)).sum() / w.sum() * 100), 1)
        else:
            local[label] = 0.0
    mean = float((w * sc["score"]).sum() / w.sum())
    out["population"] = round(float(w.sum()))
    out["people"] = {"score": round(mean), "grade": grade(mean), "within15": round(float((w * sc["within15"]).sum() / w.sum()), 1),
                     "proximity_min": round(float((w * sc["proximity_min"]).sum() / w.sum()), 1),
                     "good": round(float((w * (sc["score"] >= 70)).sum() / w.sum() * 100), 1),
                     "local_pct": local, "square_m": s}
    return out


# ---------- administrative areas (Split view) and places for the map ----------
ADMIN_QUERY = """
SELECT v.id, p.g, count(*) FROM road_view v JOIN amenity_poi p ON p.way && v.way AND ST_Intersects(p.way, v.way)
WHERE v.view = %s AND v.way && ST_MakeEnvelope(%s, %s, %s, %s, 3857) GROUP BY 1, 2
"""


@router.get("/admin")
def admin(level: int, minx: float, miny: float, maxx: float, maxy: float, groups: str = ""):
    """{area id: places of the chosen groups} for the areas of a Split view level in the map view."""
    _need()
    gs = {GROUP_IDS.index(g) for g in groups.split(",") if g in GROUP_IDS} or set(range(len(GROUPS)))
    with db() as conn:
        rows = conn.execute(ADMIN_QUERY, (level, minx, miny, maxx, maxy)).fetchall()
    out: dict = {}
    for i, g, n in rows:
        if g in gs:
            out[i] = out.get(i, 0) + n
    return out


@router.get("/points")
def points(minx: float, miny: float, maxx: float, maxy: float, groups: str = ""):
    """The places in the map view (at most 4000): [lon, lat, group, type, name]."""
    _need()
    gs = [GROUP_IDS.index(g) for g in groups.split(",") if g in GROUP_IDS] or list(range(len(GROUPS)))
    with db() as conn:
        rows = conn.execute("SELECT ST_X(q), ST_Y(q), g, s, name FROM (SELECT ST_Transform(way, 4326) AS q, g, s, name FROM amenity_poi "
                            "WHERE way && ST_MakeEnvelope(%s, %s, %s, %s, 3857) AND g = ANY(%s) LIMIT 4000) s",
                            (minx, miny, maxx, maxy, gs)).fetchall()
    return [[round(a, 6), round(b, 6), g, GROUPS[g]["subs"][s][1], n] for a, b, g, s, n in rows]


@router.get("/catalog")
def get_catalog():
    return {"groups": catalog(), "walk_kmh": population.WALK_KMH, "detour": DETOUR, "full_m": FULL_M, "zero_m": ZERO_M,
            "grades": GRADES, "prepared": prepared()}


def clear_caches():
    grid_json.cache_clear()
