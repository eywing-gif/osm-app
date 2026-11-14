"""OSM land use (landuse=*) of the map view: the values with their area, and the polygons to draw."""
import json
import math

from fastapi import APIRouter, HTTPException, Query

from .common import db

router = APIRouter(prefix="/landuse")
MIN_ZOOM = 9            # below this a view has too many polygons
MAX_FEATURES = 4000
EARTH = 40075016.686    # circumference in EPSG:3857 metres

BOX = "ST_Transform(ST_MakeEnvelope(%(w)s, %(s)s, %(e)s, %(n)s, 4326), 3857)"


def check(zoom: int):
    if zoom < MIN_ZOOM:
        raise HTTPException(400, f"Zoom in (from zoom {MIN_ZOOM}).")


@router.get("/stats")
def stats(minx: float, miny: float, maxx: float, maxy: float, zoom: int = 12):
    """Land use values in the view, biggest first: area in km² (clipped to the view) and number of polygons."""
    check(zoom)
    scale2 = math.cos(math.radians((miny + maxy) / 2)) ** 2          # EPSG:3857 stretches areas by 1 / cos²
    with db() as conn:
        rows = conn.execute(f"""
            WITH box AS (SELECT {BOX} AS g)
            SELECT landuse, sum(ST_Area(ST_Intersection(way, box.g))) * %(k)s / 1e6, count(*)
            FROM planet_osm_polygon, box
            WHERE landuse IS NOT NULL AND way && box.g
            GROUP BY landuse ORDER BY 2 DESC LIMIT 60""", dict(w=minx, s=miny, e=maxx, n=maxy, k=scale2)).fetchall()
    return {"values": [{"value": v, "km2": round(a, 3), "count": n} for v, a, n in rows]}


@router.get("/features")
def features(minx: float, miny: float, maxx: float, maxy: float, zoom: int = 12, values: str = Query("")):
    """GeoJSON polygons (simplified to about a pixel); `values` = comma list, empty = all. The biggest come first."""
    check(zoom)
    tol = EARTH / (256 * 2 ** zoom)
    want = [v for v in values.split(",") if v]
    with db() as conn:
        rows = conn.execute(f"""
            WITH box AS (SELECT {BOX} AS g)
            SELECT landuse, ST_AsGeoJSON(ST_Transform(ST_SimplifyPreserveTopology(way, %(tol)s), 4326), 5)
            FROM planet_osm_polygon, box
            WHERE landuse IS NOT NULL AND way && box.g AND (%(all)s OR landuse = ANY(%(want)s))
              AND way_area > %(tol)s * %(tol)s
            ORDER BY way_area DESC LIMIT {MAX_FEATURES}""",
                            dict(w=minx, s=miny, e=maxx, n=maxy, tol=tol, all=not want, want=want)).fetchall()
    return {"type": "FeatureCollection", "truncated": len(rows) >= MAX_FEATURES,
            "features": [{"type": "Feature", "properties": {"v": v}, "geometry": json.loads(g)} for v, g in rows]}
