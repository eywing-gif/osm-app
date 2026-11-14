"""How far does public transport get you in a given time? (isochrone of a clicked point)

Model (average day, no exact timetable): walk to the stops around the point, ride, change, walk on. A line is
boarded after waiting half its interval (07-19 h, capped); riding takes the mean time of the line's hops; changing
between stops up to TRANSFER_M apart is a walk. To the first stop and from the last one: on foot (speed and detour
factor as the catchment) or by bike (BIKE_KMH).
The result is the union of circles around the points reached: radius = what is left of the time, walked.
Data: tables transit_stop / transit_link of the timetable database (pipeline/gtfs.py, class Links).
"""
import heapq
import math
from functools import lru_cache

import numpy as np
import psycopg
from fastapi import APIRouter, HTTPException, Query

from . import population
from .common import active_transit, transit_db
from .transit import CATEGORIES, DAYS

router = APIRouter(prefix="/transit")

MAX_WAIT = 30 * 60        # s: waiting for a rare line is not longer than this
BOARD = 60                # s: getting from the platform into the vehicle
TRANSFER_M = 200          # change between stops this close by foot
MAX_WALK = 15 * 60        # s: longest way (walk / ride) from the clicked point to the first stop
# bike, door to door incl. stops at junctions: GPS studies of city cyclists 14–18 km/h (London, peak ~16),
# medians 17.6–21.7 km/h (Hamburg app data), 18 km/h occasional riders (Helsinki) -> 15 km/h, careful
BIKE_KMH = 15.0
ACCESS = {"walk": population.WALK_KMH, "bike": BIKE_KMH}
MIN_RADIUS = 40           # m: smaller circles are left out
WINDOW_S = 12 * 3600
DEG_M = 111320.0          # metres per degree of latitude


class Graph:
    """The lines of a timetable as arrays: hops sorted by start stop (CSR), stop positions, a grid for neighbours."""

    def __init__(self, db: str):
        with transit_db() as conn:
            try:
                stops = np.array(conn.execute("SELECT id, lon, lat FROM transit_stop ORDER BY id").fetchall(), dtype=np.float64)
                chunks = []
                with conn.cursor(name="links") as cur:
                    cur.execute("SELECT a, b, cat, line, n_wd, n_sa, n_su, sec FROM transit_link")
                    while rows := cur.fetchmany(200000):
                        chunks.append(np.array(rows, dtype=np.int32))
            except psycopg.errors.UndefinedTable:
                raise HTTPException(409, "Timetable too old – Settings → Transit → Update")
        if not len(stops) or not chunks:
            raise HTTPException(409, "Timetable has no lines – Settings → Transit → Update")
        self.lon, self.lat = stops[:, 1], stops[:, 2]
        links = np.concatenate(chunks)
        links = links[np.argsort(links[:, 0], kind="stable")]
        self.start = np.searchsorted(links[:, 0], np.arange(len(stops) + 1)).tolist()
        self.lonl, self.latl = self.lon.tolist(), self.lat.tolist()
        self.b, self.cat, self.line, self.sec = (links[:, i].tolist() for i in (1, 2, 3, 7))
        self.n = {d: links[:, 4 + i].tolist() for i, d in enumerate(("wd", "sa", "su"))}
        self.cos = math.cos(math.radians(float(np.median(self.lat))))
        gx, gy = self._cell(self.lon, self.lat)
        key = gx * 1_000_000 + gy
        order = np.argsort(key, kind="stable")
        self.grid_keys, self.grid_ids = key[order], order

    @staticmethod
    def _cell(lon, lat):
        return np.floor(np.asarray(lon) / 0.004).astype(np.int64) + 100_000, np.floor(np.asarray(lat) / 0.004).astype(np.int64) + 100_000

    def around(self, lon: float, lat: float, radius: float):
        """(stop ids, distances in m) of the stops within radius metres (straight line)."""
        dy = (self.lat - lat) * DEG_M
        dx = (self.lon - lon) * DEG_M * math.cos(math.radians(lat))
        d = np.hypot(dx, dy)
        ids = np.nonzero(d <= radius)[0]
        return ids, d[ids]

    def neighbours(self, s: int):
        """Other stops within TRANSFER_M of stop s: [(id, metres)]."""
        gx, gy = self._cell(self.lonl[s], self.latl[s])
        out = []
        for ix in (gx - 1, gx, gx + 1):
            for iy in (gy - 1, gy, gy + 1):
                key = int(ix) * 1_000_000 + int(iy)
                lo, hi = np.searchsorted(self.grid_keys, [key, key + 1])
                for o in self.grid_ids[lo:hi].tolist():
                    if o != s:
                        d = math.hypot((self.lonl[o] - self.lonl[s]) * DEG_M * math.cos(math.radians(self.latl[s])), (self.latl[o] - self.latl[s]) * DEG_M)
                        if d <= TRANSFER_M:
                            out.append((o, d))
        return out


@lru_cache(maxsize=2)
def graph(db: str) -> Graph:
    return Graph(db)


def clear_caches():
    graph.cache_clear()


def search(g: Graph, lon: float, lat: float, limit: int, day: str, allowed: set[int], mps: float, access_mps: float) -> dict[int, float]:
    """Earliest arrival (s) at every stop reachable within `limit` seconds.
    mps / access_mps: straight-line metres per second on foot (changes) / to the first stop (walk or bike)."""
    best_off, best_on = {}, {}
    heap = []
    ids, dist = g.around(lon, lat, min(limit, MAX_WALK) * access_mps)
    for s, d in zip(ids.tolist(), dist.tolist()):
        best_off[s] = d / access_mps
        heap.append((d / access_mps, s, -1))
    heapq.heapify(heap)
    n_day, start, nb, cat, line, sec = g.n[day], g.start, g.b, g.cat, g.line, g.sec
    while heap:
        t, s, l = heapq.heappop(heap)
        if l < 0:
            if t > best_off[s]:
                continue
            for o, d in g.neighbours(s):                          # change by foot
                t2 = t + d / mps
                if t2 <= limit and t2 < best_off.get(o, 1e18):
                    best_off[o] = t2
                    heapq.heappush(heap, (t2, o, -1))
        elif t > best_on.get((s, l), 1e18):
            continue
        for i in range(start[s], start[s + 1]):
            if l >= 0 and line[i] != l or cat[i] not in allowed:   # on board: stay on the line; else board any line
                continue
            if l < 0:
                n = n_day[i]
                if not n:
                    continue
                t2 = t + min(WINDOW_S / n / 2, MAX_WAIT) + BOARD + sec[i]
            else:
                t2 = t + sec[i]
            b = nb[i]
            if t2 > limit:
                continue
            if t2 < best_on.get((b, line[i]), 1e18):
                best_on[(b, line[i])] = t2
                heapq.heappush(heap, (t2, b, line[i]))
            if t2 < best_off.get(b, 1e18):
                best_off[b] = t2
                heapq.heappush(heap, (t2, b, -1))
    return best_off


UNION = """
SELECT ST_AsGeoJSON(ST_SimplifyPreserveTopology(u, 0.00008), 5)::jsonb, ST_Area(u::geography) / 1e6 FROM (
  SELECT ST_UnaryUnion(ST_Collect(ST_Buffer(ST_SetSRID(ST_MakePoint(x, y), 4326)::geography, r, 6)::geometry)) AS u
  FROM unnest(%s::float8[], %s::float8[], %s::float8[]) AS t(x, y, r)) s
"""


@router.get("/reach")
def reach(lat: float = Query(..., ge=-90, le=90), lon: float = Query(..., ge=-180, le=180), minutes: int = Query(30, ge=1, le=180),
          day: str = "wd", modes: str = "", access: str = "walk", detour: float = population.DETOUR):
    if day not in DAYS:
        raise HTTPException(400, "day must be wd, sa or su")
    allowed = {i for i, c in enumerate(CATEGORIES) if c in modes.split(",")} if modes else set(range(len(CATEGORIES)))
    g = graph(active_transit())
    if access not in ACCESS:
        raise HTTPException(400, "access must be walk or bike")
    mps, access_mps = population.WALK_KMH / 3.6 / detour, ACCESS[access] / 3.6 / detour
    limit = minutes * 60
    best = search(g, lon, lat, limit, day, allowed, mps, access_mps)
    xs, ys, rs = [lon], [lat], [limit * access_mps]    # straight from the point itself
    for s, t in best.items():
        r = (limit - t) * access_mps
        if r >= MIN_RADIUS:
            xs.append(g.lonl[s]); ys.append(g.latl[s]); rs.append(r)
    with transit_db() as conn:
        geometry, km2 = conn.execute(UNION, (xs, ys, rs)).fetchone()
    try:
        stats = population.area_stats(geometry)
        people = stats["population"] if stats["cells"] else None      # None: no data here
    except HTTPException:
        people = None                                  # no population data installed
    return {"geometry": geometry, "area_km2": round(km2, 1 if km2 >= 10 else 2), "population": people, "stops": len(best),
            "minutes": minutes, "access": access, "speed_kmh": ACCESS[access], "detour": detour}
