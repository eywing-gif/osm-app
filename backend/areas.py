"""Areas to limit a search to: districts, towns, protected areas … or a drawn shape.

Named areas come from the local database first (the imported regions) and from
Nominatim (OpenStreetMap's place search, worldwide) otherwise.
"""
import json

import psycopg
from fastapi import APIRouter, HTTPException, Query
from psycopg import sql
from pydantic import BaseModel

from .common import db, fetch_json

router = APIRouter(prefix="/areas")

NOMINATIM = "https://nominatim.openstreetmap.org"
NOMINATIM_INTERVAL = 1.1  # usage policy: max. 1 request per second

AREA_BOUNDARIES = ["administrative", "protected_area", "national_park"]
AREA_PLACES = ["state", "region", "county", "city", "town", "village", "borough",
               "suburb", "quarter", "neighbourhood", "island"]

# German admin levels (https://wiki.openstreetmap.org/wiki/Tag:boundary=administrative)
ADMIN_LABELS = {2: "Country", 4: "State", 5: "Government region", 6: "District / independent city",
                7: "Municipal association", 8: "Municipality / town", 9: "Borough",
                10: "Locality / quarter", 11: "Neighbourhood"}

# numeric admin level, NULL if missing or not a number
LEVEL = "CASE WHEN {t}.admin_level ~ '^[0-9]+$' THEN {t}.admin_level::int END"


def kind_label(boundary=None, admin_level=None, place=None, fallback=None) -> str:
    if boundary == "administrative" and str(admin_level).isdigit():
        return ADMIN_LABELS.get(int(admin_level), f"Admin level {admin_level}")
    if boundary == "national_park":
        return "National park"
    if boundary == "protected_area":
        return "Protected area"
    if place:
        return place.replace("_", " ").capitalize()
    return (fallback or "Area").replace("_", " ").capitalize()


def osm2pgsql_id(osm_type: str, osm_id: int) -> int:
    """osm2pgsql stores relations with negative ids."""
    return -osm_id if osm_type == "relation" else osm_id


# ---------- finding areas by name ----------

@router.get("")
def find_areas(q: str = Query(..., min_length=2), online: bool = True):
    results = local_candidates(q)
    if online:
        seen = {(r["osm_type"], r["osm_id"]) for r in results}
        try:
            results += [r for r in nominatim_candidates(q) if (r["osm_type"], r["osm_id"]) not in seen]
        except (OSError, ValueError):
            pass  # offline or Nominatim unavailable: local results only
    return results[:12]


def local_candidates(q: str) -> list[dict]:
    query = f"""
        SELECT osm_id, max(name), max(boundary), max(admin_level), max(place),
               sum(ST_Area(ST_Transform(way, 4326)::geography)) / 1e6 AS km2
        FROM planet_osm_polygon p
        WHERE name ILIKE %(like)s
          AND (boundary = ANY(%(boundaries)s) OR place = ANY(%(places)s))
        GROUP BY osm_id
        ORDER BY lower(max(name)) = lower(%(q)s) DESC,
                 lower(max(name)) LIKE lower(%(q)s) || '%%' DESC,
                 max({LEVEL.format(t='p')}) NULLS LAST, km2 DESC
        LIMIT 10"""
    with db() as conn:
        rows = conn.execute(query, dict(q=q, like=f"%{q}%", boundaries=AREA_BOUNDARIES,
                                        places=AREA_PLACES)).fetchall()
    return [{"source": "local", "osm_type": "relation" if i < 0 else "way", "osm_id": abs(i),
             "name": name, "kind": kind_label(b, lvl, place), "area_km2": round(km2, 2),
             "context": None} for i, name, b, lvl, place, km2 in rows]


def nominatim_candidates(q: str) -> list[dict]:
    rows = fetch_json(f"{NOMINATIM}/search", dict(q=q, format="jsonv2", limit=10),
                      min_interval=NOMINATIM_INTERVAL)
    out, place_point = [], None
    for r in rows:
        if r.get("osm_type") == "node" and r.get("category") == "place" and place_point is None:
            place_point = r  # place mapped only as a point (common for suburbs)
        if r.get("osm_type") not in ("relation", "way") or r.get("category") in (
                "highway", "railway", "waterway", "building"):
            continue
        out.append(nominatim_candidate(r))
    if place_point and len(out) < 3:
        # offer the smallest boundary around that point instead (one extra request)
        # zoom 12 = city district / municipality; finer levels are usually points again
        around = fetch_json(f"{NOMINATIM}/reverse", dict(
            lat=place_point["lat"], lon=place_point["lon"], zoom=12, format="jsonv2"),
            min_interval=NOMINATIM_INTERVAL)
        if around.get("osm_type") in ("relation", "way") and around.get("category") == "boundary":
            c = nominatim_candidate(around)
            c["context"] = f"area around {place_point.get('name')} · {c['context'] or ''}".rstrip(" ·")
            out.insert(0, c)
    return out


def nominatim_candidate(r: dict) -> dict:
    parts = [p.strip() for p in r.get("display_name", "").split(",")]
    return {"source": "online", "osm_type": r["osm_type"], "osm_id": int(r["osm_id"]),
            "name": r.get("name") or parts[0],
            "kind": kind_label(fallback=r.get("addresstype") or r.get("type")),
            "area_km2": None, "context": ", ".join(parts[1:4]) or None}


# ---------- details of one area ----------

def stats(conn, geom: sql.Composable, exclude_id: int = 0, own_level: int = 99) -> dict:
    """Size, outline and enclosing admin areas of a geometry (EPSG:3857 SQL expression)."""
    query = sql.SQL("""
        WITH s AS (SELECT {geom} AS g), w AS (SELECT g, ST_Transform(g, 4326) AS g4 FROM s)
        SELECT ST_Area(g4::geography) / 1e6, ST_Perimeter(g4::geography) / 1e3,
               ST_AsGeoJSON(ST_Transform(ST_SimplifyPreserveTopology(g, 5), 4326), 6)::jsonb,
               ST_XMin(g4), ST_YMin(g4), ST_XMax(g4), ST_YMax(g4),
               (SELECT string_agg(name, ', ' ORDER BY lvl DESC) FROM (
                   SELECT DISTINCT ON (lvl) p.name, {level} AS lvl
                   FROM planet_osm_polygon p
                   WHERE p.boundary = 'administrative' AND p.osm_id <> {exclude}
                     AND {level} < {own}
                     AND p.way && ST_PointOnSurface(w.g) AND ST_Contains(p.way, ST_PointOnSurface(w.g))
                     -- only units around the whole area, not just its centre
                     -- (bounding box test: cheap, unlike intersecting detailed borders)
                     AND p.way ~ w.g
                   ORDER BY lvl) parents)
        FROM w""").format(geom=geom, level=sql.SQL(LEVEL.format(t="p")),
                          exclude=sql.Literal(exclude_id), own=sql.Literal(own_level))
    row = conn.execute(query).fetchone()
    if row is None or row[0] is None:
        raise HTTPException(404, "Area not found")
    km2, perim, geometry, *bbox, part_of = row
    if geometry["type"] not in ("Polygon", "MultiPolygon"):
        raise HTTPException(400, "This object is not an area.")
    if part_of:  # e.g. town and municipal association often share the name
        part_of = ", ".join(dict.fromkeys(part_of.split(", ")))
    return {"area_km2": round(km2, 3), "perimeter_km": round(perim, 2), "geometry": geometry,
            "bbox": bbox, "part_of": part_of}


def geojson_geom(geometry: dict) -> sql.Composable:
    return sql.SQL("ST_Transform(ST_MakeValid(ST_SetSRID(ST_GeomFromGeoJSON({}), 4326)), 3857)").format(
        sql.Literal(json.dumps(geometry)))


@router.get("/{osm_type}/{osm_id}")
def area_details(osm_type: str, osm_id: int):
    if osm_type not in ("relation", "way"):
        raise HTTPException(400, "Only ways and relations can be areas.")
    pid = osm2pgsql_id(osm_type, osm_id)
    with db() as conn:
        row = conn.execute("""
            SELECT jsonb_strip_nulls(to_jsonb(p) - 'way' - 'tags') || COALESCE(hstore_to_jsonb(p.tags), '{}')
            FROM planet_osm_polygon p WHERE osm_id = %s ORDER BY ST_Area(way) DESC LIMIT 1""",
                           (pid,)).fetchone()
        if row:  # in the local database
            tags = row[0]
            level = int(tags["admin_level"]) if str(tags.get("admin_level")).isdigit() else 99
            info = stats(conn, sql.SQL("(SELECT ST_Union(way) FROM planet_osm_polygon WHERE osm_id = {})")
                         .format(sql.Literal(pid)), pid, level)
            info |= {"source": "local", "kind": kind_label(tags.get("boundary"), tags.get("admin_level"),
                                                           tags.get("place"))}
        else:    # anywhere else: ask Nominatim for the outline
            tags, info = online_area(conn, osm_type, osm_id)
    return info | {
        "osm_type": osm_type, "osm_id": osm_id, "name": tags.get("name"),
        "population": tags.get("population"), "population_date": tags.get("population:date"),
        "wikipedia": tags.get("wikipedia"), "wikidata": tags.get("wikidata"),
        "website": tags.get("website"),
    }


def online_area(conn, osm_type: str, osm_id: int):
    try:
        rows = fetch_json(f"{NOMINATIM}/lookup", dict(
            osm_ids=f"{osm_type[0].upper()}{osm_id}", format="jsonv2", polygon_geojson=1,
            polygon_threshold=0.00005, extratags=1, namedetails=1), min_interval=NOMINATIM_INTERVAL)
    except (OSError, ValueError) as e:
        raise HTTPException(503, f"Could not reach the online place search ({e}).")
    if not rows:
        raise HTTPException(404, "Area not found")
    r = rows[0]
    tags = (r.get("extratags") or {}) | {"name": r.get("name") or r.get("display_name", "").split(",")[0]}
    info = stats(conn, geojson_geom(r["geojson"]), 0,
                 int(tags["admin_level"]) if str(tags.get("admin_level")).isdigit() else 99)
    parts = [p.strip() for p in r.get("display_name", "").split(",")]
    info |= {"source": "online", "part_of": info["part_of"] or ", ".join(parts[1:4]) or None,
             "kind": kind_label(tags.get("boundary"), tags.get("admin_level"), tags.get("place"),
                                r.get("addresstype") or r.get("type"))}
    return tags, info


# ---------- drawn shapes ----------

class Shape(BaseModel):
    geometry: dict


@router.post("/info")
def shape_info(shape: Shape):
    if shape.geometry.get("type") not in ("Polygon", "MultiPolygon"):
        raise HTTPException(400, "Shape must be a polygon.")
    with db() as conn:
        try:
            info = stats(conn, geojson_geom(shape.geometry))
        except psycopg.Error as e:
            raise HTTPException(400, f"Invalid shape: {e.diag.message_primary or e}")
    return info | {"source": "drawn", "name": "Drawn area", "kind": "Custom shape"}
