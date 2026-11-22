"""Download a GTFS feed and build its transit database transit_<feed>.

    venv/bin/python -m pipeline.transit gtfs_de [--zip path/to/feed.zip] [--from snap|centres|lines|links]

Steps: segments + stations (gtfs) → real course from the map data (snap) → city centres (centres) → lines and
statistics per station (lines). Needs the map database for the last three; works without (straight lines).
Data: see backend/catalog.py (DELFI e.V. via gtfs.de, CC BY 4.0, …).
"""
import shutil
import sys
from pathlib import Path

from backend import catalog
from backend.common import DATA, db_transit_name, update_state
from . import centres, gtfs, lines, snap
from .util import download, log, progress, recreate_db, stage, step, steps


def main(feed: str, zip_path=None, start="gtfs"):
    info = catalog.FEEDS[feed]
    db = db_transit_name(feed)
    own = zip_path is None                  # our own download: deleted after the import
    zip_path = Path(zip_path) if zip_path else DATA / "gtfs" / f"{feed}.zip"
    order = ["gtfs", "snap", "centres", "lines", "links"]
    todo = lambda name: order.index(name) >= order.index(start)       # `start`: resume a failed run at this step
    names = {"gtfs": "Timetable", "snap": "Tracks", "centres": "Centres", "lines": "Lines", "links": "Hops"}
    steps(*(["Download"] if own else []), *(["Merge"] if own and "parts" in info else []),
          *[names[n] for n in order[:-1] if todo(n)], *(["Hops"] if start == "links" else []))
    if own and not zip_path.exists():
        step("Download")
        if "parts" in info:
            fetch_parts(info["parts"], zip_path)
        else:
            stage(0, 10)
            download(info["url"], zip_path, "download")
    stage(10, 12)
    progress(0, "reading the feed")
    if todo("gtfs"):
        step("Timetable")
        recreate_db(db)
    feed_data = gtfs.Feed(zip_path)
    if todo("gtfs"):
        stage(12, 45)
        try:
            gtfs.build(feed_data, db)
        except gtfs.NotGrouped:             # a few feeds (e.g. Nice) list stop times out of order: sort, then again
            log("stop times not sorted by trip – sorting")
            progress(0, "sorting the stop times")
            feed_data.zip.close()
            gtfs.regroup(zip_path)
            recreate_db(db)
            feed_data = gtfs.Feed(zip_path)
            gtfs.build(feed_data, db)
    if todo("snap"):
        step("Tracks")
        stage(45, 75)
        progress(0, "course of the lines")
        snap.main(db)
    if todo("centres"):
        step("Centres")
        stage(75, 80)
        progress(0, "city centres")
        centres.main(db)
    if todo("lines"):
        step("Lines")
        stage(80, 100)
        lines.main(feed_data, db)
    if start == "links":                    # an older timetable: add the hops for the travel-time search
        step("Hops")
        stage(12, 100)
        gtfs.build_links(feed_data, db)
    if own:
        zip_path.unlink(missing_ok=True)       # the numbers are in the database now

    def mark(st):
        t = st.setdefault("transit", {})
        t.setdefault("active", feed)
    update_state(mark)
    log("done")


def fetch_parts(parts: dict, dest: Path):
    """Download several feeds and merge them into dest. A part whose server fails is left out (said in the log)."""
    tmp = dest.parent / (dest.stem + "_parts")
    got = {}
    for n, (key, url) in enumerate(parts.items()):
        stage(8 * n / len(parts), 8 * (n + 1) / len(parts))
        f = tmp / f"{key}.zip"
        try:
            if not f.exists():
                download(url, f, f"{key} ({n + 1}/{len(parts)})")
            got[key] = f
        except Exception as e:
            log(f"{key} left out – download failed: {e}")
    if not got:
        raise SystemExit("No part of the feed could be downloaded.")
    step("Merge")
    stage(8, 10)
    gtfs.merge(got, dest)
    log(f"merged {len(got)} of {len(parts)} feeds" + (f" (missing: {', '.join(k for k in parts if k not in got)})" if len(got) < len(parts) else ""))
    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    a = sys.argv[1:]
    opt = lambda k, d=None: a[a.index(k) + 1] if k in a else d
    main(a[0], opt("--zip"), opt("--from", "gtfs"))
