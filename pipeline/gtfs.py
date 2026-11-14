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
                raise SystemExit("stop_times.txt is not grouped by trip – not supported")
            seen.add(trip_id)
            info = self.trips.get(trip_id)
            if info is None:
                continue
            stops = sorted(((int(p[4]), self.station[p[3]], p[1] or p[2], p[2] or p[1]) for p in grp if p[3] in self.station), key=lambda x: x[0])
            yield (*info, [(sid, hms(a) if a else None, hms(d) if d else None) for _, sid, a, d in stops])


def build(feed: Feed, db: str):
    """Count departures per segment and station; write the first tables of the database."""
    edges = {}          # (a, b, cat) -> [wd a->b, wd b->a, sa a->b, sa b->a, su a->b, su b->a]   (a < b)
    lines = defaultdict(lambda: defaultdict(int))     # (a, b, cat) -> line name -> weekday departures
    departures = defaultdict(lambda: [0, 0, 0])       # (station, cat) -> [weekday, saturday, sunday] departures in the window
    rows = 0
    for n, (cat, short, mask, stops) in enumerate(feed.trip_stops()):
        rows += len(stops)
        if n % 20000 == 0:
            progress(100 * min(1, n / max(1, len(feed.trips))), f"{n:,} trips")
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
        conn.execute("CREATE TABLE transit_meta AS SELECT %s::text AS wd, %s::text AS sa, %s::text AS su", [str(feed.refs[k]) for k in DAYS])
        conn.commit()
        conn.execute("ANALYZE")
        log(f"{conn.execute('SELECT count(*) FROM transit_edge').fetchone()[0]:,} segments stored")
