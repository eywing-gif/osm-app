"""What can be downloaded: map regions (Geofabrik), population grids, timetable feeds."""
import json
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from .common import DATA, USER_AGENT, fetch_json

# ---- population grids (table pop_<id> in the database "population") ----
# srid / cell: coordinate system and edge length (m) of the square cells; stats: has age / housing columns
# mb: download, disk: about how much it takes on disk once installed (MB, measured; only where it differs much)
POPULATION = {
    "zensus_de": dict(why="Inhabitants, age, housing for Germany – finest detail", name="Germany", source="Zensus 2022", detail="100 m · age, housing", srid=3035, cell=100, mb=190, disk=900, stats=True,
                      bbox=(5.5, 47.0, 15.7, 55.2)),
    "ghs_eu": dict(why="Inhabitants for all of Europe – coarser, no age / housing", name="Europe", source="GHS-POP 2020", detail="1 km · inhabitants", srid=54009, cell=1000, mb=290, disk=950, stats=False,
                   bbox=(-32, 34, 45, 72)),
    "ghs_world": dict(why="Inhabitants worldwide – coarse, large", name="World", source="GHS-POP 2020", detail="1 km · inhabitants", srid=54009, cell=1000, mb=290, disk=6100, stats=False),
}
GHS_URL = "https://jrc-ghsl.s3.eu-central-1.amazonaws.com/ghs-pop/r2023a/54009/1000m/2020/GHS_POP_E2020_GLOBE_R2023A_54009_1000_V1_0.tif"

# ---- built-up area (GHS-BUILT-S R2023A, 2020): m² of buildings per cell, total and non-residential (table built_<id>) ----
# Read straight from the JRC server (only the needed window), so the download is the size of the table.
GHS_BUILT = "https://jrc-ghsl.s3.eu-central-1.amazonaws.com/{dir}/r2023a/54009/{res}m/2020/{name}_E2020_GLOBE_R2023A_54009_{res}_V1_0.tif"
BUILT = {
    "ghs_built_de": dict(name="Germany", source="GHS-BUILT-S 2020", detail="100 m · built-up share", why="How much of each 100 m square is built over – Germany",
                         srid=54009, cell=100, res=100, mb=2500, disk=1900, bbox=(5.5, 47.0, 15.7, 55.2)),
    "ghs_built_eu": dict(name="Europe", source="GHS-BUILT-S 2020", detail="1 km · built-up share", why="How much of each km² is built over – Europe",
                         srid=54009, cell=1000, res=1000, mb=450, disk=950, bbox=(-32, 34, 45, 72)),
    "ghs_built_world": dict(name="World", source="GHS-BUILT-S 2020", detail="1 km · built-up share", why="How much of each km² is built over – worldwide (large)",
                            srid=54009, cell=1000, res=1000, mb=2000, disk=4000),
}

# ---- timetable feeds (GTFS zip, database transit_<id>) ----
# only: the feed has just these modes (the map says so), full: except in these areas; bbox: the area it covers (lon/lat), if not a whole country;
# parts: several feeds merged into one (ids get a prefix per part); disk: see above (else about 3.5 × the download)
SNCF = "https://eu.ftp.opendatasoft.com/sncf/plandata/export-opendata-sncf-gtfs.zip"
FR_CITIES = {
    "idf": dict(name="Paris region", detail="Île-de-France Mobilités · metro, RER, tram, bus", url="https://eu.ftp.opendatasoft.com/stif/GTFS/IDFM-gtfs.zip", mb=113, bbox=(1.4, 48.1, 3.6, 49.3)),
    "lyon": dict(name="Lyon", detail="TCL · metro, tram, bus", url="https://www.data.gouv.fr/api/1/datasets/r/abebedc6-28cf-4e2e-9c64-db57a40156f8", mb=50, bbox=(4.6, 45.55, 5.15, 45.95)),
    "marseille": dict(name="Marseille", detail="RTM · metro, tram, bus", url="https://www.data.gouv.fr/api/1/datasets/r/7eef6ec9-9ebb-44f2-becb-2efc522522d6", mb=10, bbox=(5.2, 43.15, 5.65, 43.45)),
    "toulouse": dict(name="Toulouse", detail="Tisséo · metro, tram, bus", url="https://www.data.gouv.fr/api/1/datasets/r/64318177-f5b5-4144-a6be-2d0f22d26c77", mb=11, bbox=(1.15, 43.45, 1.7, 43.8)),
    "nice": dict(name="Nice", detail="Lignes d'Azur · tram, bus", url="https://www.data.gouv.fr/api/1/datasets/r/f5678ab2-c863-4b48-ba1f-9021c7d97634", mb=10, bbox=(6.9, 43.6, 7.45, 44.0)),
    "nantes": dict(name="Nantes", detail="Naolib · tram, bus", url="https://www.data.gouv.fr/api/1/datasets/r/65bdd7e4-a8e7-4a54-893a-808e71c34d68", mb=22, bbox=(-1.8, 47.05, -1.3, 47.35)),
    "strasbourg": dict(name="Strasbourg", detail="CTS · tram, bus", url="https://opendata.cts-strasbourg.eu/google_transit.zip", mb=8, bbox=(7.55, 48.45, 7.95, 48.7)),
    "montpellier": dict(name="Montpellier", detail="TaM · tram, bus", url="https://www.data.gouv.fr/api/1/datasets/r/93a29ce2-cfc8-44ff-b712-bcfd814b7f00", mb=7, bbox=(3.7, 43.5, 4.05, 43.75)),
    "bordeaux": dict(name="Bordeaux", detail="TBM · tram, bus, ferry", url="https://www.data.gouv.fr/api/1/datasets/r/10b87ffe-e6bb-494d-93df-bb6019e223d9", mb=19, bbox=(-0.85, 44.7, -0.4, 45.05)),
    "lille": dict(name="Lille", detail="Ilévia · metro, tram, bus", url="https://www.data.gouv.fr/api/1/datasets/r/c9e5dd3f-8eed-4ad7-aec2-915240599cf6", mb=9, bbox=(2.85, 50.5, 3.3, 50.8)),
    "rennes": dict(name="Rennes", detail="STAR · metro, bus", url="https://www.data.gouv.fr/api/1/datasets/r/0644f537-575e-4cce-9570-06165d6f3b27", mb=13, bbox=(-1.85, 47.95, -1.5, 48.25)),
    "grenoble": dict(name="Grenoble", detail="M réso (TAG) · tram, bus", url="https://www.data.gouv.fr/api/1/datasets/r/b6ec7ba4-09bc-46df-b9a1-79a2c2668cf2", mb=6, bbox=(5.55, 45.05, 5.95, 45.35)),
}
FEEDS = {
    "gtfs_de":    dict(why="Timetables Germany – all buses, trams, trains", name="Germany", detail="all modes · DELFI", url="https://download.gtfs.de/germany/free/latest.zip", mb=276, disk=1100),
    "gtfs_de_rv": dict(why="Timetables Germany – regional trains only (small)", name="Germany – regional rail", detail="DELFI", url="https://download.gtfs.de/germany/rv_free/latest.zip", mb=11, only="regional rail"),
    "gtfs_de_fv": dict(why="Timetables Germany – long-distance trains only (tiny, good for tests)", name="Germany – long-distance rail", detail="DELFI", url="https://download.gtfs.de/germany/fv_free/latest.zip", mb=1, only="long-distance rail"),
    "gtfs_nl":    dict(why="Timetables Netherlands – all modes", name="Netherlands", detail="OVapi", url="https://gtfs.ovapi.nl/nl/gtfs-nl.zip", mb=169),
    "gtfs_ch":    dict(why="Timetables Switzerland – all modes", name="Switzerland", detail="geOps", url="https://gtfs.geops.ch/dl/gtfs_complete.zip", mb=220),
    "gtfs_no":    dict(why="Timetables Norway – all modes", name="Norway", detail="Entur", url="https://storage.googleapis.com/marduk-production/outbound/gtfs/rb_norway-aggregated-gtfs.zip", mb=550),
    "gtfs_cz_pid": dict(why="Timetables Prague region – metro, trams, buses", name="Prague region", detail="PID", url="https://data.pid.cz/PID_GTFS.zip", mb=47, disk=75, bbox=(13.4, 49.6, 15.2, 50.6)),
    "gtfs_fr": dict(why="Timetables France – SNCF trains plus metro, trams, buses of the 12 largest cities", name="France – rail + 12 cities",
                    detail="SNCF · " + ", ".join(c["name"] for c in FR_CITIES.values()), mb=5 + sum(c["mb"] for c in FR_CITIES.values()),
                    disk=400, only="rail", full=[c["bbox"] for c in FR_CITIES.values()],      # outside the cities: trains only
                    parts={"sncf": SNCF, **{k: c["url"] for k, c in FR_CITIES.items()}}),
    "gtfs_fr_sncf": dict(why="Timetables France – SNCF trains (TGV, Intercités, TER)", name="France – SNCF rail", detail="SNCF", url=SNCF, mb=5, disk=30, only="rail"),
    **{f"gtfs_fr_{k}": dict(why=f"Timetables {c['name']} – {c['detail'].split(' · ')[1]}", **c) for k, c in FR_CITIES.items()},
}


def disk_mb(info: dict, factor: float = 3.5) -> int:
    """About how many MB a data set takes on disk once installed."""
    return info.get("disk") or round(info["mb"] * factor + 20)


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
