"""OSM tag search API.

Run from the project root:
    venv/bin/uvicorn backend.main:app --reload
Then open http://localhost:8000
"""
import json
import threading
import urllib.error
from functools import lru_cache
from pathlib import Path
from typing import Literal

import psycopg
from psycopg import sql
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import appinfo, areas, datasets, features, landuse, population, reach, roads, taglib, transit
from .common import db, fetch_json, geojson_bbox
from .tagsql import TABLES, tag_condition, tag_expr

FRONTEND = Path(__file__).resolve().parent.parent / "frontend" / "index.html"
STATION_PAGE = FRONTEND.with_name("station.html")

# Keys tried when the user types only a value, e.g. "bakery" -> shop=bakery.
CATEGORY_KEYS = ["amenity", "shop", "tourism", "leisure", "craft", "office",
                 "healthcare", "historic", "sport", "man_made", "natural", "public_transport"]

MAX_RESULTS = 2000

# Extra (hstore) keys offered in the suggestion list besides CATEGORY_KEYS.
SUGGEST_EXTRA_KEYS = ["cuisine", "religion", "vending", "recycling_type"]

# A bare word that is one of these is treated as "key exists" (e.g. "wheelchair").
KNOWN_KEYS = {"cuisine", "wheelchair", "opening_hours", "website", "phone", "wifi",
              "internet_access", "outdoor_seating", "diet:vegan", "diet:vegetarian"}

# Public Overpass servers, tried in order. They are free but rate limited,
# so the frontend only searches online when zoomed in.
OVERPASS_URLS = ["https://overpass-api.de/api/interpreter",
                 "https://overpass.private.coffee/api/interpreter"]
MAX_ONLINE_AREA = 1.0  # square degrees (~ a large city); bigger areas are refused

app = FastAPI(title="OSM tag search")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"])
app.include_router(areas.router)
app.include_router(taglib.router)
app.include_router(features.router)
app.include_router(population.router)
app.include_router(transit.router)
app.include_router(reach.router)
app.include_router(roads.router)
app.include_router(datasets.router)
app.include_router(appinfo.router)
app.include_router(landuse.router)


class FrontendFiles(StaticFiles):
    """The page's css / js: revalidated on every load (the desktop window would otherwise show old versions)."""
    async def get_response(self, path, scope):
        r = await super().get_response(path, scope)
        r.headers["Cache-Control"] = "no-cache"
        return r


app.mount("/static", FrontendFiles(directory=FRONTEND.parent), name="static")


@app.exception_handler(psycopg.OperationalError)
@app.exception_handler(psycopg.errors.UndefinedTable)
@app.exception_handler(psycopg.errors.UndefinedColumn)
async def data_missing(request, exc):
    """A database or table is not there (yet): the user has to download it in the settings."""
    return JSONResponse({"detail": "Data not installed – open Settings and download it."}, status_code=503)


def parse_query(q: str) -> tuple[str, object]:
    """
    'amenity=cafe'        -> ("tags", [("amenity", "cafe")])
    'cuisine=*' / 'wifi'  -> ("tags", [("cuisine", None)])   key present, any value
    'amenity=restaurant & cuisine=italian' -> both conditions (AND)
    'bakery'              -> ("word", "bakery")  any category key = bakery, or name contains it
    'name:Alexanderplatz' -> ("name", "Alexanderplatz")
    """
    q = q.strip()
    if q.lower().startswith("name:"):
        return "name", q[5:].strip()
    parts = [p.strip() for p in q.replace(" and ", "&").split("&") if p.strip()]
    if len(parts) == 1 and "=" not in parts[0] and parts[0] not in KNOWN_KEYS:
        return "word", parts[0]
    conds = []
    for p in parts:
        k, _, v = (s.strip() for s in p.partition("="))
        conds.append((k, None if v in ("", "*") else v))
    return "tags", conds


def where_clause(table: str, q: str) -> tuple[sql.Composable, sql.Composable]:
    """Returns (filter, category expression) for the given table."""
    mode, arg = parse_query(q)
    category = sql.SQL("COALESCE({})").format(
        sql.SQL(", ").join(tag_expr(table, k) for k in CATEGORY_KEYS))
    if mode == "name":
        return sql.SQL("t.name ILIKE {}").format(sql.Literal(f"%{arg}%")), category
    if mode == "word":
        any_key = sql.SQL(" OR ").join(tag_condition(table, k, arg) for k in CATEGORY_KEYS)
        return sql.SQL("({}) OR t.name ILIKE {}").format(any_key, sql.Literal(f"%{arg}%")), category
    conds = [tag_condition(table, k, v) for k, v in arg]
    first_key = arg[0][0]
    category = sql.SQL("{} || '=' || {}").format(sql.Literal(first_key), tag_expr(table, first_key))
    return sql.SQL(" AND ").join(conds), category


@app.get("/station")
def station_page():
    """The detail window of a station (opened from the transit mode of the map)."""
    return FileResponse(STATION_PAGE, headers={"Cache-Control": "no-store"})


@app.get("/")
def index():
    # never cached: the desktop window would otherwise keep showing an old version of the page
    return FileResponse(FRONTEND, headers={"Cache-Control": "no-store"})


class AreaFilter(BaseModel):
    geometry: dict                  # GeoJSON (Multi)Polygon in WGS84
    osm_type: str | None = None     # set for OSM areas (lets Overpass use its own copy)
    osm_id: int | None = None


class SearchRequest(BaseModel):
    q: str = Field(min_length=1)
    source: Literal["local", "online"] = "local"
    bbox: tuple[float, float, float, float] = (-180, -90, 180, 90)  # minx, miny, maxx, maxy
    limit: int = Field(500, ge=1, le=MAX_RESULTS)
    area: AreaFilter | None = None  # if set: search inside this area instead of bbox


@app.post("/search")
def search(req: SearchRequest):
    if req.area:
        if req.area.geometry.get("type") not in ("Polygon", "MultiPolygon"):
            raise HTTPException(400, "Area must be a polygon.")
        bbox = geojson_bbox(req.area.geometry)
    else:
        bbox = req.bbox
    if req.source == "online":
        return search_overpass(req.q, *bbox, req.limit, req.area)
    return search_local(req.q, *bbox, req.limit, req.area)


@app.get("/search")
def search_get(q: str = Query(..., min_length=1),
               minx: float = -180, miny: float = -90, maxx: float = 180, maxy: float = 90,
               limit: int = Query(500, le=MAX_RESULTS),
               source: Literal["local", "online"] = "local"):
    return search(SearchRequest(q=q, source=source, bbox=(minx, miny, maxx, maxy), limit=limit))


def search_local(q, minx, miny, maxx, maxy, limit, area: AreaFilter | None = None):
    if area:
        prefix = sql.SQL("WITH area AS (SELECT {} AS g) ").format(areas.geojson_geom(area.geometry))
        source = sql.SQL("{table} t, area a")
        where = sql.SQL("t.way && a.g AND ST_Intersects(t.way, a.g)")
    else:
        prefix = sql.SQL("")
        source = sql.SQL("{table} t")
        where = sql.SQL("t.way && ST_Transform(ST_MakeEnvelope({}, {}, {}, {}, 4326), 3857)").format(
            *map(sql.Literal, (minx, miny, maxx, maxy)))
    selects = []
    for table in TABLES:
        cond, cat = where_clause(table, q)
        # all tags (real columns + hstore) as one JSON object for the popup
        tag_json = sql.SQL(
            "jsonb_strip_nulls(to_jsonb(t) - 'osm_id' - 'way' - 'tags' - 'z_order' - 'way_area')"
            " || COALESCE(hstore_to_jsonb(t.tags), '{}')")
        selects.append(sql.SQL("""
            SELECT {kind}, t.osm_id, t.name, {cat} AS category, {tags} AS tags,
                   ST_AsGeoJSON(ST_Transform(ST_PointOnSurface(t.way), 4326))::jsonb AS geom
            FROM {source}
            WHERE ({cond}) AND {where}
        """).format(kind=sql.Literal(table.removeprefix("planet_osm_")), cat=cat, tags=tag_json,
                    source=source.format(table=sql.Identifier(table)), cond=cond, where=where))
    query = prefix + sql.SQL(" UNION ALL ").join(selects) + sql.SQL(" LIMIT {}").format(sql.Literal(limit + 1))

    try:
        with db() as conn:
            rows = conn.execute(query).fetchall()
    except psycopg.Error as e:
        raise HTTPException(400, f"Query failed: {e.diag.message_primary or e}")

    features = []
    for kind, osm_id, name, category, tags, geom in rows[:limit]:
        # osm2pgsql: polygons from relations have negative ids
        osm_type = "node" if kind == "point" else ("relation" if osm_id < 0 else "way")
        features.append({
            "type": "Feature", "geometry": geom,
            "properties": {"osm_type": osm_type, "osm_id": abs(osm_id), "name": name,
                           "category": category, "tags": tags},
        })
    return {"type": "FeatureCollection", "features": features, "truncated": len(rows) > limit}


# ---------- online search via the Overpass API ----------

def ql_str(s: str) -> str:
    """Quote a string for Overpass QL."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def regex_escape(s: str) -> str:
    """Escape POSIX regex special characters (for name searches)."""
    return "".join("\\" + c if c in ".^$*+?()[]{}|\\" else c for c in s)


def overpass_area(area: AreaFilter | None) -> tuple[str, str]:
    """(statement defining the area, filter to append to each query)."""
    if area is None:
        return "", ""
    if area.osm_type in ("relation", "way") and area.osm_id:
        # Overpass keeps its own copy of named OSM areas, addressed by a derived id
        offset = 3600000000 if area.osm_type == "relation" else 2400000000
        return f"area({offset + area.osm_id})->.a;", "(area.a)"
    # drawn shape: polygon filter with "lat lon lat lon ..." (outer ring of the first polygon)
    g = area.geometry
    ring = g["coordinates"][0] if g["type"] == "Polygon" else g["coordinates"][0][0]
    step = max(1, len(ring) // 400)  # keep the query short
    pts = " ".join(f"{lat:.6f} {lon:.6f}" for lon, lat in ring[::step])
    return "", f'(poly:"{pts}")'


def overpass_query(q: str, bbox: str, limit: int, area: AreaFilter | None = None) -> str:
    define, f = overpass_area(area)
    mode, arg = parse_query(q)
    if mode == "name":
        stmts = [f"nwr[name~{ql_str(regex_escape(arg))},i]{f};"]
    elif mode == "word":
        stmts = [f"nwr[{ql_str(k)}={ql_str(arg)}]{f};" for k in CATEGORY_KEYS]
        stmts.append(f"nwr[name~{ql_str(regex_escape(arg))},i]{f};")
    else:
        filters = "".join(f"[{ql_str(k)}]" if v is None else f"[{ql_str(k)}={ql_str(v)}]" for k, v in arg)
        stmts = [f"nwr{filters}{f};"]
    return f"[out:json][timeout:25][bbox:{bbox}];{define}({''.join(stmts)});out center tags {limit + 1};"


def search_overpass(q, minx, miny, maxx, maxy, limit, area: AreaFilter | None = None):
    if (maxx - minx) * (maxy - miny) > MAX_ONLINE_AREA:
        raise HTTPException(400, "Area too large for online search – zoom in or pick a smaller area.")
    query = overpass_query(q, f"{miny},{minx},{maxy},{maxx}", limit, area)
    data, last_error = None, "no server reachable"
    for url in OVERPASS_URLS:
        try:
            data = fetch_json(url, data={"data": query}, timeout=60)
            break
        except urllib.error.HTTPError as e:  # quick refusal: try the next server
            last_error = {429: "too many requests", 504: "server busy"}.get(e.code, f"HTTP {e.code}")
        except TimeoutError:  # already waited a minute: don't make it two
            last_error = "server did not answer within 60 s"
            break
        except (urllib.error.URLError, ValueError) as e:
            last_error = str(getattr(e, "reason", e))
    if data is None:
        raise HTTPException(503, f"Online search failed ({last_error}). The public servers are "
                                 "probably busy – try again in a moment.")

    mode, arg = parse_query(q)
    features = []
    for el in data.get("elements", []):
        pos = el if "lat" in el else el.get("center")
        if not pos:
            continue
        tags = el.get("tags", {})
        if mode == "tags":
            k = arg[0][0]
            category = f"{k}={tags.get(k)}"
        else:
            category = next((tags[k] for k in CATEGORY_KEYS if k in tags), None)
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [pos["lon"], pos["lat"]]},
            "properties": {"osm_type": el["type"], "osm_id": el["id"], "name": tags.get("name"),
                           "category": category, "tags": tags},
        })
    return {"type": "FeatureCollection", "features": features[:limit],
            "truncated": len(features) > limit}


# ---------- tag suggestions ----------

TAG_CACHE = Path.home() / ".cache" / "osm-tag-search" / "tags.json"


@lru_cache
def _popular_tags():
    """Counting tags takes ~20 s, so the result is stored on disk and only
    recomputed when the database content changes (new import or update)."""
    with db() as conn:
        # changes whenever the database is re-imported (new database / new tables)
        version = conn.execute(
            "SELECT (SELECT oid FROM pg_database WHERE datname = current_database())::text"
            " || ':' || 'planet_osm_point'::regclass::oid::text").fetchone()[0]
    try:
        cached = json.loads(TAG_CACHE.read_text())
        if cached["version"] == version and cached.get("keys") == CATEGORY_KEYS + SUGGEST_EXTRA_KEYS:
            return cached["tags"]
    except (OSError, ValueError, KeyError):
        pass
    tags = _count_tags()
    TAG_CACHE.parent.mkdir(parents=True, exist_ok=True)
    TAG_CACHE.write_text(json.dumps(
        {"version": version, "keys": CATEGORY_KEYS + SUGGEST_EXTRA_KEYS, "tags": tags}))
    return tags


def _count_tags():
    selects = []
    for table in TABLES:
        for k in CATEGORY_KEYS + SUGGEST_EXTRA_KEYS:
            e = tag_expr(table, k)
            selects.append(sql.SQL("SELECT {k} AS key, {e} AS value FROM {t} t WHERE {e} IS NOT NULL").format(
                k=sql.Literal(k), e=e, t=sql.Identifier(table)))
    query = sql.SQL("SELECT key, value, count(*) FROM ({}) s GROUP BY 1, 2 HAVING count(*) >= 5 ORDER BY 3 DESC LIMIT 800").format(
        sql.SQL(" UNION ALL ").join(selects))
    with db() as conn:
        return [{"key": k, "value": v, "count": c} for k, v, c in conn.execute(query).fetchall()]


@app.get("/tags")
def tags():
    """Popular key=value pairs, used for autocomplete."""
    return _popular_tags()


@app.on_event("startup")
def warm_tag_cache():
    # counting all tags takes a while, so do it once in the background
    threading.Thread(target=_popular_tags, daemon=True).start()
    threading.Thread(target=taglib.warm, daemon=True).start()   # keys for the tag library
