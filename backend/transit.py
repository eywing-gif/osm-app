"""Public transport segments with their service interval (database transit_<feed>, built by pipeline/transit.py).

A segment is the connection between two neighbouring stops. `n` = departures between 07:00 and 19:00 in the
busier direction (all lines of the mode together), so the interval is 720 / n minutes.
"""
import json
import math
from functools import lru_cache

from fastapi import APIRouter, HTTPException, Query, Response

from .common import transit_db

router = APIRouter(prefix="/transit")

# draw order (bottom -> top) and the zoom level from which a mode is offered (there are millions of bus segments)
CATEGORIES = ["bus", "coach", "tram", "subway", "ferry", "other", "sbahn", "regional", "intercity", "highspeed"]
MIN_ZOOM = {"highspeed": 4, "intercity": 5, "regional": 7, "sbahn": 8, "subway": 9, "tram": 10,
            "ferry": 9, "other": 11, "bus": 12, "coach": 6}
DAYS = {"wd", "sa", "su"}
WINDOW_MIN = 720
WINDOW_H = WINDOW_MIN / 60     # the time window 07:00-19:00 is 12 hours
# interval classes (upper limit in minutes) -> departures needed in the 12 h window
BUCKETS = {5: (WINDOW_MIN // 5, None), 10: (WINDOW_MIN // 10, WINDOW_MIN // 5), 15: (WINDOW_MIN // 15, WINDOW_MIN // 10),
           20: (WINDOW_MIN // 20, WINDOW_MIN // 15), 30: (WINDOW_MIN // 30, WINDOW_MIN // 20),
           60: (WINDOW_MIN // 60, WINDOW_MIN // 30), 999: (1, WINDOW_MIN // 60)}
MAX_ROWS = 25000


def parse_filters(modes: str, buckets: str, day: str):
    if day not in DAYS:
        raise HTTPException(400, "day must be wd, sa or su")
    mods = [m for m in modes.split(",") if m in CATEGORIES]
    bks = [int(b) for b in buckets.split(",") if b.isdigit() and int(b) in BUCKETS]
    return mods, bks


def n_condition(col: str, bks: list[int]) -> str:
    """SQL for 'departures match one of the chosen interval classes'."""
    parts = []
    for b in bks:
        lo, hi = BUCKETS[b]
        parts.append(f"({col} >= {lo}" + (f" AND {col} < {hi})" if hi else ")"))
    return "(" + " OR ".join(parts) + ")" if parts else "false"


def tile_bounds(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    n = 2 ** z
    lon = lambda xx: xx / n * 360 - 180
    lat = lambda yy: math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * yy / n))))
    return lon(x), lat(y + 1), lon(x + 1), lat(y)


def clear_caches():
    tile_json.cache_clear()
    stations_json.cache_clear()


@lru_cache(maxsize=512)
def tile_json(z: int, x: int, y: int, mods: tuple, bks: tuple, day: str, centre: str = "") -> bytes:
    use = [m for m in mods if z >= MIN_ZOOM[m]]
    if not use or not bks:
        return b'{"e":[]}'
    w, s, e, n = tile_bounds(z, x, y)
    pad = (e - w) * 0.05                       # segments just outside still reach into the tile
    px = (e - w) / 256                         # degrees per pixel (longitude)
    tol = px * 0.7                              # simplify real courses to well below a pixel
    lv = parse_levels(centre)
    if lv is None:
        query = f"""
            SELECT cat, n_{day}, alon, alat, blon, blat, id, ov,
                   CASE WHEN path IS NULL THEN NULL ELSE ST_AsGeoJSON(COALESCE(ST_Simplify(path, %s), path), 5) END
            FROM transit_edge
            WHERE line && ST_MakeEnvelope(%s, %s, %s, %s, 4326) AND cat = ANY(%s) AND {n_condition(f"n_{day}", list(bks))}
              AND (alon - blon) ^ 2 + (alat - blat) ^ 2 >= %s          -- shorter than ~1.5 px: invisible anyway
            ORDER BY n_{day} DESC LIMIT {MAX_ROWS}"""
    else:
        # centre filter: only the departures of trips that go into a central place of the chosen level(s)
        n_lvl = f"GREATEST(sum(l.{day}_ab), sum(l.{day}_ba))"
        query = f"""
            SELECT e.cat, {n_lvl} AS n, e.alon, e.alat, e.blon, e.blat, e.id, e.ov,
                   CASE WHEN e.path IS NULL THEN NULL ELSE ST_AsGeoJSON(COALESCE(ST_Simplify(e.path, %s), e.path), 5) END
            FROM transit_edge e JOIN transit_edge_lvl l ON l.edge_id = e.id
            WHERE e.line && ST_MakeEnvelope(%s, %s, %s, %s, 4326) AND e.cat = ANY(%s) AND l.lvl = ANY(ARRAY{lv})
              AND (e.alon - e.blon) ^ 2 + (e.alat - e.blat) ^ 2 >= %s
            GROUP BY e.id
            HAVING {n_condition(n_lvl, list(bks))}
            ORDER BY n DESC LIMIT {MAX_ROWS}"""
    with transit_db() as conn:
        rows = conn.execute(query, (tol, w - pad, s - pad, e + pad, n + pad, use, (1.5 * px) ** 2 * 0.4)).fetchall()
    cats = {c: i for i, c in enumerate(CATEGORIES)}
    out = []
    for c, nn, alon, alat, blon, blat, eid, ov, gj in rows:
        # [mode index, departures, [lon, lat, lon, lat, …], id, ids of overlapping segments of other modes]
        # – the real course if known, else the straight line
        if gj:
            flat = [round(v, 5) for pt in json.loads(gj)["coordinates"] for v in pt]
        else:
            flat = [round(alon, 5), round(alat, 5), round(blon, 5), round(blat, 5)]
        out.append([cats[c], nn, flat, eid, ov])
    return json.dumps({"e": out, "cats": CATEGORIES}, separators=(",", ":")).encode()


@lru_cache(maxsize=512)
def stations_json(z: int, x: int, y: int, mods: tuple, bks: tuple, day: str, centre: str = "") -> bytes:
    use = [m for m in mods if z >= MIN_ZOOM[m]]
    if not use or not bks:
        return b'{"s":[]}'
    w, s, e, n = tile_bounds(z, x, y)
    pad = (e - w) * 0.05
    # one dot per station: all chosen modes together (departures add up); the frequency filter applies to the total
    # centre filter: count only the departures of trips into a central place of the chosen level(s)
    lv = parse_levels(centre)
    source, extra = ("transit_station_lvl", f"AND lvl = ANY(ARRAY{lv})") if lv else ("transit_station", "")
    query = f"""
        SELECT name, lon, lat, array_agg(cat ORDER BY n_{day} DESC), array_agg(n_{day} ORDER BY n_{day} DESC), sum(n_{day}) AS total
        FROM (SELECT name, lon, lat, cat, sum(n_{day}) AS n_{day} FROM {source}
              WHERE geom && ST_MakeEnvelope(%s, %s, %s, %s, 4326) AND cat = ANY(%s) {extra}
              GROUP BY name, lon, lat, cat) t
        WHERE n_{day} > 0
        GROUP BY name, lon, lat
        HAVING {n_condition(f"sum(n_{day})", list(bks))}
        ORDER BY total DESC LIMIT {MAX_ROWS}"""
    with transit_db() as conn:
        rows = conn.execute(query, (w - pad, s - pad, e + pad, n + pad, use)).fetchall()
    cats = {c: i for i, c in enumerate(CATEGORIES)}
    # [lon, lat, name, [[mode index, departures], …], total departures]
    return json.dumps({"s": [[round(lo, 5), round(la, 5), nm, [[cats[c], int(k)] for c, k in zip(cs, ns)], int(tot)]
                             for nm, lo, la, cs, ns, tot in rows], "cats": CATEGORIES},
                      separators=(",", ":"), ensure_ascii=False).encode()


def tile_response(fn, z, x, y, modes, buckets, day, centre):
    if not 0 <= z <= 22 or not (0 <= x < 2 ** z and 0 <= y < 2 ** z):
        raise HTTPException(404, "No such tile")
    mods, bks = parse_filters(modes, buckets, day)
    parse_levels(centre)
    body = fn(z, x, y, tuple(mods), tuple(bks), day, ",".join(sorted(c for c in centre.split(",") if c and c != "all")))
    return Response(body, media_type="application/json", headers={"Cache-Control": "no-cache"})


@router.get("/stations/{z}/{x}/{y}")
def stations(z: int, x: int, y: int, modes: str = "", buckets: str = "", day: str = "wd", centre: str = "all"):
    return tile_response(stations_json, z, x, y, modes, buckets, day, centre)


@router.get("/meta")
def meta():
    with transit_db() as conn:
        wd, sa, su = conn.execute("SELECT wd, sa, su FROM transit_meta").fetchone()
    return {"days": {"wd": wd, "sa": sa, "su": su}, "categories": CATEGORIES, "min_zoom": MIN_ZOOM, "window": "07:00–19:00"}


@router.get("/tile/{z}/{x}/{y}")
def tile(z: int, x: int, y: int, modes: str = "", buckets: str = "", day: str = "wd", centre: str = "all"):
    return tile_response(tile_json, z, x, y, modes, buckets, day, centre)


@router.get("/at")
def at(lat: float = Query(..., ge=-90, le=90), lon: float = Query(..., ge=-180, le=180), zoom: int = 14,
       modes: str = "", buckets: str = "", day: str = "wd", view: str = "lines", centre: str = "all"):
    """The connections (view=lines) or stations (view=stations) next to a clicked point (about 12 screen pixels around it)."""
    mods, bks = parse_filters(modes, buckets, day)
    mods = [m for m in mods if zoom >= MIN_ZOOM[m]]
    if not mods or not bks:
        return {"items": []}
    radius = max(8.0, 12 * 40075016.686 * math.cos(math.radians(lat)) / (256 * 2 ** zoom))   # metres
    deg = radius / 111000 * 1.6
    lv = parse_levels(centre)
    if view == "stations":
        if lv is None:
            query = f"""
                WITH p AS (SELECT ST_SetSRID(ST_MakePoint(%s, %s), 4326) AS g)
                SELECT name, cat, n_{day}, ST_Distance(e.geom::geography, p.g::geography) AS dist, e.lon, e.lat
                FROM transit_station e, p
                WHERE e.geom && ST_Expand(p.g, %s) AND cat = ANY(%s) AND {n_condition(f"n_{day}", bks)}
                  AND ST_DWithin(e.geom::geography, p.g::geography, %s)
                ORDER BY dist, name LIMIT 60"""
        else:      # only the departures of trips into a central place of the chosen level(s)
            query = f"""
                WITH p AS (SELECT ST_SetSRID(ST_MakePoint(%s, %s), 4326) AS g)
                SELECT name, cat, sum(n_{day}) AS n, min(ST_Distance(e.geom::geography, p.g::geography)) AS dist, e.lon, e.lat
                FROM transit_station_lvl e, p
                WHERE e.geom && ST_Expand(p.g, %s) AND cat = ANY(%s) AND lvl = ANY(ARRAY{lv})
                  AND ST_DWithin(e.geom::geography, p.g::geography, %s)
                GROUP BY name, cat, e.lon, e.lat
                HAVING {n_condition(f"sum(n_{day})", bks)}
                ORDER BY dist, name LIMIT 60"""
        with transit_db() as conn:
            rows = conn.execute(query, (lon, lat, deg, mods, radius)).fetchall()
        out: dict[str, dict] = {}                 # one entry per station, with its modes
        for name, c, n, d, slon, slat in rows:
            st = out.setdefault(name, {"name": name, "lon": slon, "lat": slat, "distance_m": round(d), "modes": []})
            st["modes"].append({"cat": c, "departures": n, "interval_min": round(WINDOW_MIN / n, 1)})
        for st in out.values():
            st["modes"].sort(key=lambda m: -m["departures"])
            for m in st["modes"]:
                m["per_hour"] = round(m["departures"] / WINDOW_H, 1)
            total = sum(m["departures"] for m in st["modes"])      # all chosen types together
            st["departures"], st["per_hour"] = total, round(total / WINDOW_H, 1)
            st["interval_min"] = round(WINDOW_MIN / total, 1) if total else None
        return {"radius_m": round(radius), "stations": list(out.values())[:12]}
    if lv is None:
        query = f"""
            WITH p AS (SELECT ST_SetSRID(ST_MakePoint(%s, %s), 4326) AS g)
            SELECT cat, aname, bname, n_{day}, lines, ST_Distance(e.line::geography, p.g::geography) AS dist
            FROM transit_edge e, p
            WHERE e.line && ST_Expand(p.g, %s) AND cat = ANY(%s) AND {n_condition(f"n_{day}", bks)}
              AND ST_DWithin(e.line::geography, p.g::geography, %s)
            ORDER BY dist LIMIT 40"""
    else:
        n_lvl = f"GREATEST(sum(l.{day}_ab), sum(l.{day}_ba))"
        query = f"""
            WITH p AS (SELECT ST_SetSRID(ST_MakePoint(%s, %s), 4326) AS g)
            SELECT e.cat, e.aname, e.bname, {n_lvl} AS n, e.lines, ST_Distance(e.line::geography, p.g::geography) AS dist
            FROM transit_edge e JOIN transit_edge_lvl l ON l.edge_id = e.id, p
            WHERE e.line && ST_Expand(p.g, %s) AND e.cat = ANY(%s) AND l.lvl = ANY(ARRAY{lv})
              AND ST_DWithin(e.line::geography, p.g::geography, %s)
            GROUP BY e.id, p.g
            HAVING {n_condition(n_lvl, bks)}
            ORDER BY dist LIMIT 40"""
    with transit_db() as conn:
        rows = conn.execute(query, (lon, lat, deg, mods, radius)).fetchall()
    return {"radius_m": round(radius), "items": [
        {"cat": c, "from": a, "to": b, "departures": n, "interval_min": round(WINDOW_MIN / n, 1), "lines": ls,
         "distance_m": round(d)} for c, a, b, n, ls, d in rows]}


# Which kind of central place does the line go into? (best level of all stops of the trip)
LEVELS = {"oz": 1, "mz": 2, "gz": 3, "other": 4, "none": 9}
def parse_levels(centre: str) -> list[int] | None:
    """'oz,mz' -> [1, 2]; 'all' or '' -> None (no filter)."""
    names = [c for c in centre.split(",") if c and c != "all"]
    bad = [c for c in names if c not in LEVELS]
    if bad:
        raise HTTPException(400, "centre must be a list of oz, mz, gz, other, none")
    return sorted({LEVELS[c] for c in names}) or None


LEVEL_NAMES = {1: "Oberzentrum", 2: "Mittelzentrum", 3: "Grundzentrum", 4: "Other centre", 9: "No centre"}


@router.get("/station")
def station(lon: float, lat: float, day: str = "wd", centre: str = "all"):
    """Details of one station: departures per hour of the day and every line that leaves from it.
    centre = comma separated list of oz, mz, gz, other, none (or all): only lines that go into a central place of one of
    these levels."""
    if day not in DAYS:
        raise HTTPException(400, "day must be wd, sa or su")
    keep_lvls = parse_levels(centre)
    eps = 2e-6
    where = "lon BETWEEN %s AND %s AND lat BETWEEN %s AND %s"
    box = (lon - eps, lon + eps, lat - eps, lat + eps)
    with transit_db() as conn:
        name = conn.execute(f"SELECT name FROM transit_station WHERE {where} LIMIT 1", box).fetchone()
        hour_rows = conn.execute(f"SELECT lvl, {day} FROM transit_station_hour WHERE {where}", box).fetchall()
        rows = conn.execute(f"""
            SELECT l.id, l.cat, l.short, l.origin, l.dest, l.length_km, l.duration_min, l.stops, sl.lvl,
                   sl.w_{day}, sl.d_{day}, sl.first_{day}, sl.last_{day}, sl.rest_km, sl.rest_min
            FROM transit_station_line sl JOIN transit_line l ON l.id = sl.line
            WHERE sl.{where} AND sl.d_{day} > 0""", box).fetchall()
        here = conn.execute(f"SELECT centre, level FROM transit_station_area WHERE {where} LIMIT 1", box).fetchone()
    if not name:
        raise HTTPException(404, "Station not found")

    # overview for the filter buttons: always over all lines of the station
    levels = {k: {"departures_window": 0, "departures_day": 0} for k in LEVELS}
    for r in rows:
        for k, v in LEVELS.items():
            if r[8] == v:
                levels[k]["departures_window"] += r[9]
                levels[k]["departures_day"] += r[10]
    for d in levels.values():
        d["per_hour"] = round(d["departures_window"] / WINDOW_H, 1)
        d["interval_min"] = round(WINDOW_MIN / d["departures_window"], 1) if d["departures_window"] else None

    merged: dict[int, dict] = {}                       # one entry per line, directions added up
    for lid, cat, short, origin, dest, length, dur, stops, lvl, w, d, first, last, rest_km, rest_min in rows:
        if keep_lvls is not None and lvl not in keep_lvls:
            continue
        m = merged.get(lid)
        if m is None:
            m = merged[lid] = {"id": lid, "cat": cat, "line": short, "origin": origin, "destination": dest,
                               "departures_window": 0, "departures_day": 0, "first": None, "last": None,
                               "length_km": r1(length), "duration_min": r1(dur), "stops": round(stops),
                               "rest_km": r1(rest_km), "rest_min": r1(rest_min), "_best": -1,
                               "centre": LEVEL_NAMES.get(lvl)}
        m["departures_window"] += w
        m["departures_day"] += d
        if first is not None:
            m["first"] = first if m["first"] is None else min(m["first"], first)
        if last is not None:
            m["last"] = last if m["last"] is None else max(m["last"], last)
        if d > m["_best"]:                              # distance still to go: from the direction with most departures
            m["_best"], m["rest_km"], m["rest_min"] = d, r1(rest_km), r1(rest_min)
    lines = sorted(merged.values(), key=lambda m: (-m["departures_window"], -m["departures_day"]))[:400]
    for m in lines:
        m.pop("_best")
        m["per_hour"] = round(m["departures_window"] / WINDOW_H, 1)
        m["interval_min"] = round(WINDOW_MIN / m["departures_window"], 1) if m["departures_window"] else None

    hourly = [0] * 24
    for lvl, arr in hour_rows:
        if keep_lvls is None or lvl in keep_lvls:
            hourly = [a + b for a, b in zip(hourly, arr)]
    by_mode: dict[str, int] = {}
    for ln in lines:
        by_mode[ln["cat"]] = by_mode.get(ln["cat"], 0) + ln["departures_window"]
    total_w = sum(by_mode.values())
    firsts = [ln["first"] for ln in lines if ln["first"] is not None]
    lasts = [ln["last"] for ln in lines if ln["last"] is not None]
    return {"name": name[0], "lon": lon, "lat": lat, "day": day, "centre_filter": centre,
            "in_centre": {"name": here[0], "level": LEVEL_NAMES.get(here[1])} if here else None,
            "levels": levels, "hourly": hourly,
            "total_window": total_w, "per_hour": round(total_w / WINDOW_H, 1),
            "interval_min": round(WINDOW_MIN / total_w, 1) if total_w else None,
            "total_day": sum(ln["departures_day"] for ln in lines),
            "first": min(firsts) if firsts else None, "last": max(lasts) if lasts else None,
            "modes": sorted(({"cat": c, "departures_window": n, "per_hour": round(n / WINDOW_H, 1)}
                             for c, n in by_mode.items()), key=lambda m: -m["departures_window"]),
            "lines": lines}


def r1(v):
    return None if v is None else round(float(v), 1)


@router.get("/stations-summary")
def stations_summary(minx: float, miny: float, maxx: float, maxy: float, zoom: int = 12, modes: str = "", buckets: str = "",
                     day: str = "wd", centre: str = "all"):
    """How many stations are in the visible map area? Total for the current filters and, for the centre filter buttons,
    how many there would be for each level (the other filters stay as they are)."""
    mods, bks = parse_filters(modes, buckets, day)
    use = [m for m in mods if zoom >= MIN_ZOOM[m]]
    lv = parse_levels(centre)
    empty = {"total": 0, "all": 0, "levels": {k: 0 for k in LEVELS}, "hidden_modes": [m for m in mods if m not in use]}
    if not use or not bks:
        return empty
    col = f"n_{day}"
    base = f"""SELECT name, lon, lat, lvl, sum({col}) AS n FROM transit_station_lvl
               WHERE geom && ST_MakeEnvelope(%s, %s, %s, %s, 4326) AND cat = ANY(%s) GROUP BY name, lon, lat, lvl"""
    box = (minx, miny, maxx, maxy, use)
    with transit_db() as conn:
        per_level = dict(conn.execute(f"SELECT lvl, count(*) FROM ({base}) b WHERE {n_condition('n', list(bks))} GROUP BY lvl", box).fetchall())
        def count(where_lv):
            return conn.execute(f"""SELECT count(*) FROM (SELECT name, lon, lat, sum(n) AS n FROM ({base}) b {where_lv}
                                    GROUP BY name, lon, lat) t WHERE n > 0 AND {n_condition('n', list(bks))}""", box).fetchone()[0]
        total = count(f"WHERE lvl = ANY(ARRAY{lv})" if lv else "")
        everything = count("") if lv else total
    return {"total": total, "all": everything, "levels": {k: per_level.get(v, 0) for k, v in LEVELS.items()},
            "hidden_modes": [m for m in mods if m not in use]}


@router.get("/centres")
def centres():
    """The areas that count as 'city centre' of the central places (for the map), and the names per level."""
    with transit_db() as conn:
        rows = conn.execute("""SELECT name, level, source, ST_AsGeoJSON(ST_Simplify(geom, 0.00003), 5) FROM transit_centre_area
                               ORDER BY level, name""").fetchall()
    names: dict[str, list[str]] = {}
    for name, level, _, _ in rows:
        key = {1: "oz", 2: "mz", 3: "gz"}[level]
        if name not in names.setdefault(key, []):
            names[key].append(name)
    return {"type": "FeatureCollection",
            "features": [{"type": "Feature", "properties": {"name": n, "level": lv, "source": src}, "geometry": json.loads(g)}
                         for n, lv, src, g in rows],
            "names": names}
