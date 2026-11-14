"""Download a GTFS feed and build its transit database transit_<feed>.

    venv/bin/python -m pipeline.transit gtfs_de [--zip path/to/feed.zip] [--from snap|centres|lines]

Steps: segments + stations (gtfs) → real course from the map data (snap) → city centres (centres) → lines and
statistics per station (lines). Needs the map database for the last three; works without (straight lines).
Data: see backend/catalog.py (DELFI e.V. via gtfs.de, CC BY 4.0, …).
"""
import sys
from pathlib import Path

from backend import catalog
from backend.common import DATA, db_transit_name, update_state
from . import centres, gtfs, lines, snap
from .util import download, log, progress, recreate_db, stage


def main(feed: str, zip_path=None, start="gtfs"):
    info = catalog.FEEDS[feed]
    db = db_transit_name(feed)
    own = zip_path is None                  # our own download: deleted after the import
    zip_path = Path(zip_path) if zip_path else DATA / "gtfs" / f"{feed}.zip"
    if own:
        if not zip_path.exists():
            stage(0, 10)
            download(info["url"], zip_path, "download")
    order = ["gtfs", "snap", "centres", "lines"]
    todo = lambda step: order.index(step) >= order.index(start)       # `start`: resume a failed run at this step
    stage(10, 12)
    progress(0, "reading the feed")
    if todo("gtfs"):
        recreate_db(db)
    feed_data = gtfs.Feed(zip_path)
    if todo("gtfs"):
        stage(12, 45)
        gtfs.build(feed_data, db)
    if todo("snap"):
        stage(45, 75)
        progress(0, "course of the lines")
        snap.main(db)
    if todo("centres"):
        stage(75, 80)
        progress(0, "city centres")
        centres.main(db)
    stage(80, 100)
    lines.main(feed_data, db)
    if own:
        zip_path.unlink(missing_ok=True)       # the numbers are in the database now

    def mark(st):
        t = st.setdefault("transit", {})
        t.setdefault("active", feed)
    update_state(mark)
    log("done")


if __name__ == "__main__":
    a = sys.argv[1:]
    opt = lambda k, d=None: a[a.index(k) + 1] if k in a else d
    main(a[0], opt("--zip"), opt("--from", "gtfs"))
