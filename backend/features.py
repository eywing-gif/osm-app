"""Everything OpenStreetMap knows about one point on the map ("Query features").

Returns the areas that contain the point (landuse, districts, buildings, ...) and
the lines / points close to it (roads, rivers, shops, ...), each with all its tags.
Uses the local database or, for places it does not cover, the Overpass API.
"""
import math
import urllib.error
from typing import Literal

import psycopg
from fastapi import APIRouter, HTTPException, Query
from psycopg import sql

from . import areas, population
from .common import db, fetch_json

router = APIRouter()

PER_KIND = 25            # at most this many objects per table / group
EARTH = 156543.03392     # metres per pixel at zoom 0 on the equator (EPSG:3857 units)
OVERPASS_URLS = ["https://overpass-api.de/api/interpreter",
                 "https://overpass.private.coffee/api/interpreter"]

# Keys that best describe what an object *is*, in order of importance.
TITLE_KEYS = ["boundary", "landuse", "natural", "water", "waterway", "building", "amenity", "shop",
              "tourism", "leisure", "building:part", "highway", "railway", "aeroway", "place", "historic", "man_made",
              "power", "office", "craft", "public_transport", "barrier", "route"]


NOISE_PREFIXES = ("building:", "roof:", "addr:", "source")


def title_of(tags: dict) -> str:
    """'Residential (landuse)' style one-liner for an object."""
    if tags.get("boundary") == "administrative" and str(tags.get("admin_level", "")).isdigit():
        return areas.kind_label("administrative", tags["admin_level"])
    for k in TITLE_KEYS:
        v = tags.get(k)
        if v:
            return f"{k}={v}"
    return next((f"{k}={v}" for k, v in sorted(tags.items()) if k not in ("name", "area")), "(no tags)")


def search_radius(lat: float, zoom: int) -> float:
    """About 10 screen pixels, in metres, so clicking next to a road still finds it."""
    return min(500.0, max(4.0, 10 * EARTH * math.cos(math.radians(lat)) / 2 ** zoom))


@router.get("/inspect")
def inspect(lat: float = Query(..., ge=-90, le=90), lon: float = Query(..., ge=-180, le=180),
            zoom: int = Query(16, ge=0, le=22), source: Literal["local", "online"] = "local"):
    radius = search_radius(lat, zoom)
    items = inspect_overpass(lat, lon, radius) if source == "online" else inspect_local(lat, lon, radius, zoom)
    for it in items:
        it["title"] = title_of(it["tags"])
    return {"lat": lat, "lon": lon, "radius_m": round(radius), "source": source, "items": items,
            "population": population.cell_at(lon, lat), "built": population.built_at(lon, lat)}


def inspect_local(lat: float, lon: float, radius: float, zoom: int) -> list[dict]:
    scale = 1 / math.cos(math.radians(lat))   # EPSG:3857 stretches distances by this factor
    r3857 = radius * scale
    tolerance = EARTH / 2 ** zoom             # simplify outlines to ~1 pixel
    tags = sql.SQL("jsonb_strip_nulls(to_jsonb(t) - 'osm_id' - 'way' - 'tags' - 'z_order' - 'way_area')"
                   " || COALESCE(hstore_to_jsonb(t.tags), '{}')")
    point = sql.SQL("ST_Transform(ST_SetSRID(ST_MakePoint({}, {}), 4326), 3857)").format(
        sql.Literal(lon), sql.Literal(lat))

    def query(table: str, kind: str, where: str, order: str):
        return sql.SQL("""
            WITH p AS (SELECT {point} AS g)
            SELECT {kind}, t.osm_id, {tags}, ST_Distance(t.way, p.g), {area},
                   ST_AsGeoJSON(ST_Transform(ST_SimplifyPreserveTopology(t.way, {tol}), 4326), 6)::jsonb
            FROM {table} t, p WHERE {where} ORDER BY {order} LIMIT {limit}""").format(
            point=point, kind=sql.Literal(kind), tags=tags, tol=sql.Literal(tolerance),
            area=sql.SQL("t.way_area") if kind == "area" else sql.SQL("0"),
            table=sql.Identifier(table), where=sql.SQL(where).format(r=sql.Literal(r3857)),
            order=sql.SQL(order), limit=sql.Literal(PER_KIND))

    queries = [
        query("planet_osm_polygon", "area", "t.way && p.g AND ST_Contains(t.way, p.g)", "t.way_area"),
        query("planet_osm_line", "line", "t.way && ST_Expand(p.g, {r}) AND ST_Distance(t.way, p.g) <= {r}"
              # closed ways with area tags are stored in both tables: they are listed as areas only
              " AND NOT EXISTS (SELECT 1 FROM planet_osm_polygon g WHERE g.osm_id = t.osm_id)",
              "ST_Distance(t.way, p.g)"),
        query("planet_osm_point", "point", "t.way && ST_Expand(p.g, {r}) AND ST_Distance(t.way, p.g) <= {r}",
              "ST_Distance(t.way, p.g)"),
    ]
    items = []
    try:
        with db() as conn:
            for q in queries:
                for kind, osm_id, tg, dist, area_m2, geom in conn.execute(q).fetchall():
                    if not tg or (kind != "area" and all(k.startswith(NOISE_PREFIXES) for k in tg)):
                        continue   # e.g. building-part outlines that only carry colour / levels
                    items.append({
                        "kind": kind,
                        # osm2pgsql: areas built from relations have negative ids
                        "osm_type": "node" if kind == "point" else ("relation" if osm_id < 0 else "way"),
                        "osm_id": abs(osm_id), "name": tg.get("name"), "tags": tg,
                        "distance_m": round(dist / scale, 1) if kind != "area" else 0,
                        "area_m2": round(area_m2 / scale ** 2) if kind == "area" else None,
                        "geometry": geom})
    except psycopg.Error as e:
        raise HTTPException(400, f"Query failed: {e.diag.message_primary or e}")
    return items


# ---------- online: Overpass ----------

def overpass_geometry(el: dict) -> dict | None:
    """GeoJSON of an element returned with 'out geom'."""
    if el["type"] == "node":
        return {"type": "Point", "coordinates": [el["lon"], el["lat"]]}
    if el["type"] == "way" and el.get("geometry"):
        pts = [[p["lon"], p["lat"]] for p in el["geometry"]]
        return {"type": "LineString", "coordinates": pts}
    if el["type"] == "relation":
        lines = [[[p["lon"], p["lat"]] for p in m["geometry"]]
                 for m in el.get("members", []) if m.get("type") == "way" and m.get("geometry")]
        return {"type": "MultiLineString", "coordinates": lines} if lines else None
    return None


def inspect_overpass(lat: float, lon: float, radius: float) -> list[dict]:
    query = (f"[out:json][timeout:25];is_in({lat},{lon})->.a;"
             f"(area.a;)->.ar;.ar out tags;"
             f"nwr(around:{radius:.0f},{lat},{lon})[~\".\"~\".\"]->.n;.n out tags geom {PER_KIND * 2};")
    data, last_error = None, "no server reachable"
    for url in OVERPASS_URLS:
        try:
            data = fetch_json(url, data={"data": query}, timeout=60)
            break
        except urllib.error.HTTPError as e:
            last_error = {429: "too many requests", 504: "server busy"}.get(e.code, f"HTTP {e.code}")
        except TimeoutError:
            last_error = "server did not answer within 60 s"
            break
        except (urllib.error.URLError, ValueError) as e:
            last_error = str(getattr(e, "reason", e))
    if data is None:
        raise HTTPException(503, f"Online query failed ({last_error}). Try again in a moment.")

    items = []
    for el in data.get("elements", []):
        tags = el.get("tags") or {}
        if not tags:
            continue
        if el["type"] == "area":   # is_in result: id is derived from the way / relation id
            rel = el["id"] >= 3600000000
            items.append({"kind": "area", "osm_type": "relation" if rel else "way",
                          "osm_id": el["id"] - (3600000000 if rel else 2400000000),
                          "name": tags.get("name"), "tags": tags, "distance_m": 0,
                          "area_m2": None, "geometry": None})
        else:
            items.append({"kind": "point" if el["type"] == "node" else "line",
                          "osm_type": el["type"], "osm_id": el["id"], "name": tags.get("name"),
                          "tags": tags, "distance_m": None, "area_m2": None,
                          "geometry": overpass_geometry(el)})
    return items
