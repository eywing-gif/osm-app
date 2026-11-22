"""Read a GTFS feed and build the segment tables of the transit database.

The feed has timetables but no line geometry, so the map shows *segments between neighbouring stops*
(straight lines, later snapped to OSM by pipeline.snap). For every segment we count the departures
between 07:00 and 19:00 per direction and mode on a typical weekday, Saturday and Sunday. The interval
is 720 min / departures.

Result: table transit_edge (segments), transit_station (departures per station and mode), transit_meta (reference days).
"""
import csv
import datetime as dt
import re
import zipfile
from collections import defaultdict
from itertools import groupby

import psycopg

from backend.common import dsn
from backend.transit import CATEGORIES
from .util import log, progress

WINDOW = (7 * 3600, 19 * 3600)   # departures counted in this time window
DAYS = ("wd", "sa", "su")        # typical weekday (Tuesday), Saturday, Sunday
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")

# Companies that run long-distance coaches (not local buses)
COACH_AGENCY = re.compile(r"flix|regiojet|student ?agency|blablabus|eurolines|ouibus|national express|fernbus|ecolines|lux ?express|kiwi|itabus|italbus", re.I)


def category(route_type: str, short: str, agency: str = "") -> str:
    """Mode group shown in the app. Rail is told apart by the train type in the line name (ICE, RE, S …),
    long-distance coaches by the company."""
    t = int(route_type)
    if t == 3 and COACH_AGENCY.search(agency or "") and not re.search(r"flixtrain", agency or "", re.I):
        return "coach"
    if t in (0, 900) or 900 <= t < 1000:
        return "tram"
    if t in (1, 401, 402) or 400 <= t <= 405 and t != 403:
        return "subway"
    if t in (3, 11, 200, 700) or 200 <= t < 300 or 700 <= t < 900:
        return "bus"
    if t in (4, 1000, 1200):
        return "ferry"
    if t == 2 or 100 <= t < 200:
        name = (re.match(r"[A-Za-zÄÖÜ]+", short or "") or [""])[0].upper()
        if name in ("ICE", "ECE", "RJX", "TGV", "THA", "EST") or t == 101:
            return "highspeed"
        if name in ("IC", "EC", "EN", "NJ", "RJ", "FLX", "ECM", "IR") or t in (102, 105):
            return "intercity"
        if name == "S" or t == 109:
            return "sbahn"
        if name == "U":
            return "subway"
        return "regional"
    return "other"          # cable cars, funiculars, …


def hms(s: str) -> int:
    h, m, sec = s.split(":")
    return int(h) * 3600 + int(m) * 60 + int(sec)


class Feed:
    """A GTFS zip: reference days, which trips run on them, stations. `trip_stops()` walks the timetable."""

    def __init__(self, path):
        self.zip = zipfile.ZipFile(path)
        self._names = set(self.zip.namelist())
        self._reference_days()
        self._trips()
        self._stations()
        log(f"{len(self.trips):,} trips run on the reference days, {len(self.coord):,} stations")

    def table(self, name):
        if name not in self._names:
            return
        with self.zip.open(name) as f:
            yield from csv.DictReader(l.decode("utf-8-sig") for l in f)

    def _reference_days(self):
        starts = [r["start_date"] for r in self.table("calendar.txt")] or [r["date"] for r in self.table("calendar_dates.txt")]
        ends = [r["end_date"] for r in self.table("calendar.txt")] or starts
        if not starts:
            raise SystemExit("The feed has no calendar.")
        # a typical week a little ahead: services of the feed that ended or have not begun would be missing
        lo, hi = (dt.datetime.strptime(f(starts), "%Y%m%d").date() for f in (min, max))
        hi = max(dt.datetime.strptime(max(ends), "%Y%m%d").date(), hi)
        first = max(lo, min(dt.date.today() + dt.timedelta(days=7), hi - dt.timedelta(days=7)))
        tue = first + dt.timedelta(days=(1 - first.weekday()) % 7)
        self.refs = {"wd": tue, "sa": tue + dt.timedelta(days=4), "su": tue + dt.timedelta(days=5)}
        log("reference days:", {k: str(v) for k, v in self.refs.items()})

    def _trips(self):
        mask = defaultdict(int)         # service id -> bit per reference day
        for r in self.table("calendar.txt"):
            for bit, day in enumerate(DAYS):
                d = self.refs[day]
                if r["start_date"] <= d.strftime("%Y%m%d") <= r["end_date"] and r[WEEKDAYS[d.weekday()]] == "1":
                    mask[r["service_id"]] |= 1 << bit
        for r in self.table("calendar_dates.txt"):
            for bit, day in enumerate(DAYS):
                if r["date"] == self.refs[day].strftime("%Y%m%d"):
                    mask[r["service_id"]] = mask[r["service_id"]] | 1 << bit if r["exception_type"] == "1" else mask[r["service_id"]] & ~(1 << bit)
        agencies = {r["agency_id"]: r["agency_name"] for r in self.table("agency.txt")}
        routes = {}
        for r in self.table("routes.txt"):
            short = r["route_short_name"] or r["route_long_name"]
            routes[r["route_id"]] = (category(r["route_type"], short, agencies.get(r.get("agency_id", ""), next(iter(agencies.values()), ""))), short)
        self.trips = {}                 # trip id -> (category, line name, day mask)
        for r in self.table("trips.txt"):
            m = mask.get(r["service_id"], 0)
            if m and r["route_id"] in routes:
                self.trips[r["trip_id"]] = (*routes[r["route_id"]], m)

    def _stations(self):
        """Platforms are merged into their station."""
        self.station, self.coord, self.names = {}, {}, {}
        for r in self.table("stops.txt"):
            sid = r.get("parent_station") or r["stop_id"]
            self.station[r["stop_id"]] = sid
            if r.get("location_type") == "1" or sid == r["stop_id"]:
                self.coord[sid] = (float(r["stop_lon"]), float(r["stop_lat"]))
                self.names[sid] = r["stop_name"]
        for r in self.table("stops.txt"):       # parents missing from the file: fall back to the platform
            sid = self.station[r["stop_id"]]
            self.coord.setdefault(sid, (float(r["stop_lon"]), float(r["stop_lat"])))
            self.names.setdefault(sid, r["stop_name"])

    def _stop_times(self):
        with self.zip.open("stop_times.txt") as f:
            head = next(csv.reader([f.readline().decode("utf-8-sig")]))
            ix = [head.index(c) for c in ("trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence")]
            for raw in f:
                line = raw.decode()
                parts = next(csv.reader([line])) if '"' in line else line.rstrip("\r\n").split(",")
                yield [parts[i] for i in ix]

    def trip_stops(self):
        """(category, line, day mask, [(station, arrival s, departure s)]) for every trip that runs on a reference day,
        stops in order. Times are seconds after midnight (None if missing); arrival and departure stand in for each other."""
        seen = set()
        for trip_id, grp in groupby(self._stop_times(), key=lambda p: p[0]):
            if trip_id in seen:
                raise NotGrouped("stop_times.txt is not grouped by trip")
            seen.add(trip_id)
            info = self.trips.get(trip_id)
            if info is None:
                continue
            stops = sorted(((int(p[4]), self.station[p[3]], p[1] or p[2], p[2] or p[1]) for p in grp if p[3] in self.station), key=lambda x: x[0])
            yield (*info, [(sid, hms(a) if a else None, hms(d) if d else None) for _, sid, a, d in stops])


class Links:
    """Hops of every line from one station to the next, per direction: departures in the window (per reference day) and
    the mean ride time. The travel-time search (backend/reach.py) runs on this: waiting = half the interval of the line."""

    def __init__(self):
        self.hops = {}       # (from, to, line name, category) -> [wd, sa, su, rides, seconds]

    def add(self, cat, short, mask, stops):
        for (a, _, dep), (b, arr, dep_b) in zip(stops, stops[1:]):
            t = arr if arr is not None else dep_b
            if a == b or dep is None or t is None or t < dep or not WINDOW[0] <= dep < WINDOW[1]:
                continue
            h = self.hops.setdefault((a, b, short, cat), [0, 0, 0, 0, 0])
            for bit in range(3):
                if mask >> bit & 1:
                    h[bit] += 1
            h[3] += 1
            h[4] += t - dep

    def write(self, conn, coord, names):
        """Tables transit_stop (id, name, lon, lat) and transit_link (a, b: stop ids; cat: index in CATEGORIES; line: id; n_*; sec)."""
        ids, line_ids = {}, {}
        for a, b, *_ in self.hops:
            for sid in (a, b):
                if sid in coord and sid not in ids:
                    ids[sid] = len(ids)
        conn.execute("DROP TABLE IF EXISTS transit_link, transit_stop")
        conn.execute("CREATE TABLE transit_stop (id int PRIMARY KEY, name text, lon float8, lat float8)")
        with conn.cursor().copy("COPY transit_stop FROM STDIN") as cp:
            for sid, i in ids.items():
                cp.write_row((i, names.get(sid), *coord[sid]))
        conn.execute("CREATE TABLE transit_link (a int, b int, cat smallint, line int, n_wd int, n_sa int, n_su int, sec int)")
        with conn.cursor().copy("COPY transit_link FROM STDIN") as cp:
            for (a, b, short, cat), (wd, sa, su, rides, sec) in self.hops.items():
                if a in ids and b in ids:
                    cp.write_row((ids[a], ids[b], CATEGORIES.index(cat), line_ids.setdefault((short, cat), len(line_ids)), wd, sa, su, round(sec / rides)))
        conn.execute("CREATE INDEX ON transit_link (a)")
        log(f"{len(self.hops):,} hops of {len(line_ids):,} lines, {len(ids):,} stops")


def build_links(feed: Feed, db: str):
    """Only the hops (for a timetable that was installed before they existed): `python -m pipeline.transit <feed> --from links`."""
    links = Links()
    for n, (cat, short, mask, stops) in enumerate(feed.trip_stops()):
        if n % 20000 == 0:
            progress(100 * min(1, n / max(1, len(feed.trips))), f"{n:,} trips".replace(",", "."))
        links.add(cat, short, mask, stops)
    with psycopg.connect(dsn(db)) as conn:
        links.write(conn, feed.coord, feed.names)
        conn.commit()


def build(feed: Feed, db: str):
    """Count departures per segment and station; write the first tables of the database."""
    edges = {}          # (a, b, cat) -> [wd a->b, wd b->a, sa a->b, sa b->a, su a->b, su b->a]   (a < b)
    lines = defaultdict(lambda: defaultdict(int))     # (a, b, cat) -> line name -> weekday departures
    departures = defaultdict(lambda: [0, 0, 0])       # (station, cat) -> [weekday, saturday, sunday] departures in the window
    rows = 0
    links = Links()
    for n, (cat, short, mask, stops) in enumerate(feed.trip_stops()):
        rows += len(stops)
        links.add(cat, short, mask, stops)
        if n % 20000 == 0:
            progress(100 * min(1, n / max(1, len(feed.trips))), f"{n:,} trips".replace(",", "."))
        for sid, _, dep in stops[:-1]:                 # the last stop is not a departure
            if dep is not None and WINDOW[0] <= dep < WINDOW[1]:
                d = departures[(sid, cat)]
                for bit in range(3):
                    if mask >> bit & 1:
                        d[bit] += 1
        for (a, _, dep), (b, _, _) in zip(stops, stops[1:]):
            if a == b or dep is None or not WINDOW[0] <= dep < WINDOW[1]:
                continue
            key, d = ((a, b, cat), 0) if a < b else ((b, a, cat), 1)
            e = edges.setdefault(key, [0] * 6)
            for bit in range(3):
                if mask >> bit & 1:
                    e[bit * 2 + d] += 1
            if mask & 1:
                lines[key][short] += 1
    log(f"{rows:,} stop times read, {len(edges):,} segments")
    coord, names = feed.coord, feed.names

    with psycopg.connect(dsn(db)) as conn:
        conn.execute("""CREATE TABLE transit_edge (
            id serial PRIMARY KEY, cat text NOT NULL,
            alon float8, alat float8, blon float8, blat float8, aname text, bname text,
            n_wd int, n_sa int, n_su int,       -- departures 07-19 h in the busier direction
            lines text)""")
        with conn.cursor().copy("COPY transit_edge (cat, alon, alat, blon, blat, aname, bname, n_wd, n_sa, n_su, lines) FROM STDIN") as cp:
            for (a, b, cat), e in edges.items():
                if a in coord and b in coord:
                    top = sorted(lines[(a, b, cat)].items(), key=lambda x: -x[1])[:8]
                    cp.write_row((cat, *coord[a], *coord[b], names.get(a), names.get(b), max(e[0], e[1]), max(e[2], e[3]), max(e[4], e[5]),
                                  ", ".join(f"{n} ({c})" for n, c in top) or None))
        conn.execute("ALTER TABLE transit_edge ADD COLUMN geom geometry(LineString, 4326)")
        conn.execute("UPDATE transit_edge SET geom = ST_SetSRID(ST_MakeLine(ST_MakePoint(alon, alat), ST_MakePoint(blon, blat)), 4326)")
        conn.execute("CREATE INDEX ON transit_edge USING gist (geom)")
        # stations: one row per station and mode, n = departures 07-19 h in all directions
        conn.execute("CREATE TABLE transit_station (name text, lon float8, lat float8, cat text, n_wd int, n_sa int, n_su int)")
        with conn.cursor().copy("COPY transit_station FROM STDIN") as cp:
            for (sid, cat), (wd, sa, su) in departures.items():
                if sid in coord and (wd or sa or su):
                    cp.write_row((names.get(sid), *coord[sid], cat, wd, sa, su))
        conn.execute("ALTER TABLE transit_station ADD COLUMN geom geometry(Point, 4326)")
        conn.execute("UPDATE transit_station SET geom = ST_SetSRID(ST_MakePoint(lon, lat), 4326)")
        conn.execute("CREATE INDEX ON transit_station USING gist (geom)")
        links.write(conn, coord, names)
        conn.execute("CREATE TABLE transit_meta AS SELECT %s::text AS wd, %s::text AS sa, %s::text AS su", [str(feed.refs[k]) for k in DAYS])
        conn.commit()
        conn.execute("ANALYZE")
        log(f"{conn.execute('SELECT count(*) FROM transit_edge').fetchone()[0]:,} segments stored")


# ---------- several feeds as one (catalog: parts) ----------
MERGED = ("agency.txt", "routes.txt", "trips.txt", "stops.txt", "stop_times.txt", "calendar.txt", "calendar_dates.txt")


def _prefixed(col: str) -> bool:
    return col == "parent_station" or col.endswith("_id") and col != "direction_id"


def merge(parts: dict, dest):
    """Write one GTFS zip from several ({prefix: zip path}): every id gets '<prefix>:' so the feeds do not clash.
    Only the files Feed reads; columns are the union of all parts. Prefix None: ids stay (regroup()).
    stop_times.txt of a part that is not grouped by trip is sorted (Feed.trip_stops() needs each trip in one run)."""
    import io
    tmp = dest.with_name(dest.name + ".part")
    zips = {k: zipfile.ZipFile(p) for k, p in parts.items()}
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as out:
        for n, name in enumerate(MERGED):
            progress(100 * n / len(MERGED), name)
            heads = {}
            for k, z in zips.items():
                if name in z.namelist():
                    with z.open(name) as f:
                        heads[k] = next(csv.reader([f.readline().decode("utf-8-sig")]), [])
            if not heads:
                continue
            cols = list(dict.fromkeys(c.strip() for h in heads.values() for c in h))
            with out.open(name, "w", force_zip64=True) as raw, io.TextIOWrapper(raw, "utf-8", newline="") as txt:
                w = csv.writer(txt, lineterminator="\n")
                w.writerow(cols)
                for j, (k, head) in enumerate(heads.items()):
                    progress(100 * (n + j / len(heads)) / len(MERGED), f"{name} · {k}")
                    head = [c.strip() for c in head]
                    pos = [head.index(c) if c in head else None for c in cols]
                    pre = [_prefixed(c) for c in cols]
                    rows = _rows(zips[k], name)
                    if name == "stop_times.txt" and (bad := _scattered(zips[k], head.index("trip_id"))):
                        log(f"{k or 'feed'}: {len(bad):,} trips with their stop times not in one run – regrouped")
                        rows = _by_trip(rows, head.index("trip_id"), bad)
                    for r in rows:
                        v = [r[i] if i is not None and i < len(r) else "" for i in pos]
                        w.writerow([f"{k}:{x}" if p and k and x else x for x, p in zip(v, pre)])
    for z in zips.values():
        z.close()
    tmp.rename(dest)


def _rows(z, name):
    with z.open(name) as f:
        rows = csv.reader(l.decode("utf-8-sig") for l in f)
        next(rows, None)
        yield from (r for r in rows if r)


def _scattered(z, trip_col: int) -> set:
    """Trips whose stop times are not in one run (a pass over the trip column; usually none)."""
    seen, bad, last = set(), set(), None
    for r in _rows(z, "stop_times.txt"):
        t = r[trip_col]
        if t != last:
            if t in seen:
                bad.add(t)
            seen.add(t)
            last = t
    return bad


def _by_trip(rows, trip_col: int, bad: set):
    """The rows with each trip in one run. Only the scattered trips are held in memory (all of a feed would need many GB)."""
    held = {}
    for r in rows:
        if r[trip_col] in bad:
            held.setdefault(r[trip_col], []).append(r)
        else:
            yield r
    for grp in held.values():
        yield from grp


class NotGrouped(SystemExit):
    pass


def regroup(path):
    """Rewrite a feed whose stop times are not grouped by trip (pipeline.transit does this when Feed refuses it)."""
    from pathlib import Path
    path = Path(path)
    out = path.with_name(path.stem + "_sorted.zip")
    merge({None: path}, out)
    out.replace(path)
