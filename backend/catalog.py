"""What can be downloaded: map regions (Geofabrik), population grids, timetable feeds."""
import json
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from .common import DATA, USER_AGENT, fetch_json

# ---- population grids (table pop_<id> in the database "population") ----
# srid / cell: coordinate system and edge length (m) of the square cells; stats: has age / housing columns
POPULATION = {
    "zensus_de": dict(why="Inhabitants, age, housing for Germany – finest detail", name="Germany", source="Zensus 2022", detail="100 m · age, housing", srid=3035, cell=100, mb=190, stats=True,
                      bbox=(5.5, 47.0, 15.7, 55.2)),
    "ghs_eu": dict(why="Inhabitants for all of Europe – coarser, no age / housing", name="Europe", source="GHS-POP 2020", detail="1 km · inhabitants", srid=54009, cell=1000, mb=290, stats=False,
                   bbox=(-32, 34, 45, 72)),
    "ghs_world": dict(why="Inhabitants worldwide – coarse, large", name="World", source="GHS-POP 2020", detail="1 km · inhabitants", srid=54009, cell=1000, mb=290, stats=False),
}
GHS_URL = "https://jrc-ghsl.s3.eu-central-1.amazonaws.com/ghs-pop/r2023a/54009/1000m/2020/GHS_POP_E2020_GLOBE_R2023A_54009_1000_V1_0.tif"

# ---- built-up area (GHS-BUILT-S R2023A, 2020): m² of buildings per cell, total and non-residential (table built_<id>) ----
# Read straight from the JRC server (only the needed window), so the download is the size of the table.
GHS_BUILT = "https://jrc-ghsl.s3.eu-central-1.amazonaws.com/{dir}/r2023a/54009/{res}m/2020/{name}_E2020_GLOBE_R2023A_54009_{res}_V1_0.tif"
BUILT = {
    "ghs_built_de": dict(name="Germany", source="GHS-BUILT-S 2020", detail="100 m · built-up share", why="How much of each 100 m square is built over – Germany",
                         srid=54009, cell=100, res=100, mb=2500, bbox=(5.5, 47.0, 15.7, 55.2)),
    "ghs_built_eu": dict(name="Europe", source="GHS-BUILT-S 2020", detail="1 km · built-up share", why="How much of each km² is built over – Europe",
                         srid=54009, cell=1000, res=1000, mb=450, bbox=(-32, 34, 45, 72)),
    "ghs_built_world": dict(name="World", source="GHS-BUILT-S 2020", detail="1 km · built-up share", why="How much of each km² is built over – worldwide (large)",
                            srid=54009, cell=1000, res=1000, mb=2000),
}

# ---- timetable feeds (GTFS zip, database transit_<id>) ----
FEEDS = {
    "gtfs_de":    dict(why="Timetables Germany – all buses, trams, trains", name="Germany", detail="all modes · DELFI", url="https://download.gtfs.de/germany/free/latest.zip", mb=276),
    "gtfs_de_rv": dict(why="Timetables Germany – regional trains only (small)", name="Germany – regional rail", detail="DELFI", url="https://download.gtfs.de/germany/rv_free/latest.zip", mb=11),
    "gtfs_de_fv": dict(why="Timetables Germany – long-distance trains only (tiny, good for tests)", name="Germany – long-distance rail", detail="DELFI", url="https://download.gtfs.de/germany/fv_free/latest.zip", mb=1),
    "gtfs_nl":    dict(why="Timetables Netherlands – all modes", name="Netherlands", detail="OVapi", url="https://gtfs.ovapi.nl/nl/gtfs-nl.zip", mb=169),
    "gtfs_ch":    dict(why="Timetables Switzerland – all modes", name="Switzerland", detail="geOps", url="https://gtfs.geops.ch/dl/gtfs_complete.zip", mb=220),
    "gtfs_no":    dict(why="Timetables Norway – all modes", name="Norway", detail="Entur", url="https://storage.googleapis.com/marduk-production/outbound/gtfs/rb_norway-aggregated-gtfs.zip", mb=550),
    "gtfs_cz_pid": dict(why="Timetables Prague region – metro, trams, buses", name="Prague region", detail="PID", url="https://data.pid.cz/PID_GTFS.zip", mb=47),
    "gtfs_fr_sncf": dict(why="Timetables France – SNCF trains", name="France – SNCF rail", detail="SNCF", url="https://eu.ftp.opendatasoft.com/sncf/plandata/export-opendata-sncf-gtfs.zip", mb=5),
}

# ---- map regions: the Geofabrik index (cached; works offline with the last copy) ----
GEOFABRIK_INDEX = "https://download.geofabrik.de/index-v1-nogeom.json"
GEOFABRIK_PREFIX = "https://download.geofabrik.de/"
_cache: list | None = None


def osm_regions() -> list[dict]:
    """[{id: 'europe/germany/sachsen', name, parent: 'europe/germany' | None}] sorted by name."""
    global _cache
    if _cache is not None:
        return _cache
    f = DATA / "geofabrik.json"
    if not f.exists() or time.time() - f.stat().st_mtime > 7 * 86400:
        try:
            DATA.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps(fetch_json(GEOFABRIK_INDEX, timeout=60)))
        except OSError:
            pass
        except Exception:
            if not f.exists():
                raise
    feats = json.loads(f.read_text())["features"]
    by_key = {}                       # geofabrik id -> path
    for ft in feats:
        pbf = ft["properties"].get("urls", {}).get("pbf")
        if pbf:
            by_key[ft["properties"]["id"]] = pbf[len(GEOFABRIK_PREFIX):].removesuffix("-latest.osm.pbf")
    out = [{"id": by_key[p["id"]], "name": p["name"], "parent": by_key.get(p.get("parent"))}
           for ft in feats if (p := ft["properties"])["id"] in by_key]
    _cache = sorted(out, key=lambda r: r["name"])
    return _cache


def osm_file(region_id: str):
    return DATA / "osm" / (region_id.replace("/", "_") + ".osm.pbf")


# sizes of the downloads (the index has none): asked from the server when the Settings list shows a region, kept in a file
_sizes: dict | None = None


def osm_sizes(ids: list[str] | None = None) -> dict[str, int]:
    """Bytes of the .osm.pbf of each region; with `ids`, the missing ones are looked up first (HEAD requests)."""
    global _sizes
    f = DATA / "osm_sizes.json"
    if _sizes is None:
        try:
            _sizes = json.loads(f.read_text())
        except (OSError, ValueError):
            _sizes = {}
    todo = [i for i in ids or [] if i not in _sizes]

    def head(i):
        req = urllib.request.Request(f"{GEOFABRIK_PREFIX}{i}-latest.osm.pbf", method="HEAD", headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return i, int(r.headers.get("Content-Length") or 0)
        except Exception:
            return i, 0
    if todo:
        with ThreadPoolExecutor(8) as ex:
            _sizes.update({i: n for i, n in ex.map(head, todo) if n})
        try:
            DATA.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps(_sizes))
        except OSError:
            pass
    return _sizes
