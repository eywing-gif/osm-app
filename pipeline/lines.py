"""Line and station statistics for the station window and the centre filter. Runs after pipeline.centres.

Classifies every departure by the central places its trip reaches (see pipeline.centres):
  lvl 1 Oberzentrum, 2 Mittelzentrum, 3 Grundzentrum, 4 other centre (main stations outside the regions with official
  levels), 9 without centre – the best level among all stops of the trip.
Tables:
  transit_line          one row per line and destination, e.g. "RE 1 -> Frankfurt (Oder)": length, stops, travel time
  transit_station_line  how often each line departs from each station (07-19 h and the whole day, per day type),
                        first / last departure, distance and time still to go from that station to the destination
  transit_station_hour  departures per hour of the day for each station, per day type
  transit_station_area  stations that lie inside a city centre: which centre and its level
  transit_station_lvl   departures per station, mode and centre level (for the station dots on the map)
  transit_edge_lvl      departures per line segment (row of transit_edge) and centre level (to filter the lines on the map)
"""
import math
from collections import Counter, defaultdict

import psycopg

from backend.common import dsn
from .gtfs import WINDOW
from .util import log, progress

NO_TIME = 10 ** 9
CENTRE_MIN_DEPARTURES = 24      # a main station is a centre with at least this many rail departures between 07 and 19 h (2 per hour)
CENTRE_MIN_OTHER = 60           # other stations (towns without a Hbf) need 5 per hour
CENTRE_RADIUS_KM = 3.5          # a trip "goes through the centre" if it stops within this distance of the centre
LVL_OTHER, LVL_NONE = 4, 9


def km(a, b):
    """Distance between two (lon, lat) points in km (flat-earth approximation, fine for neighbouring stops)."""
    dx = (b[0] - a[0]) * math.cos(math.radians((a[1] + b[1]) / 2)) * 111.32
    dy = (b[1] - a[1]) * 110.57
    return math.hypot(dx, dy)


def find_centres(conn):
    """The "city centres": main stations (Hbf / Hauptbahnhof with rail service) first; then, for towns without one,
    the busiest rail station that is at least 25 km away from every centre chosen so far."""
    rail = ("regional", "sbahn", "intercity", "highspeed")
    rows = conn.execute("""SELECT name, lon, lat, sum(n_wd) AS n, name ~* '(hbf|hauptbahnhof)' AS main FROM transit_station
                           WHERE cat = ANY(%s) GROUP BY name, lon, lat HAVING sum(n_wd) >= %s ORDER BY n DESC""",
                        (list(rail), CENTRE_MIN_DEPARTURES)).fetchall()
    cells = defaultdict(list)       # grid of 0.5 degrees for quick neighbour search
    centres = []

    def free(lon, lat, sep):
        cx, cy = int(lon // 0.5), int(lat // 0.5)
        return not any(km((lon, lat), c[1:3]) < sep
                       for dx in (-1, 0, 1) for dy in (-1, 0, 1) for c in cells[(cx + dx, cy + dy)])

    for want_main, sep in ((True, 8), (False, 25)):
        for name, lon, lat, n, is_main in rows:
            if (is_main if want_main else n >= CENTRE_MIN_OTHER):
                if free(lon, lat, sep):
                    c = (name, lon, lat, n)
                    centres.append(c)
                    cells[(int(lon // 0.5), int(lat // 0.5))].append(c)
    return centres


def main(feed, db):
    coord, names, trips = feed.coord, feed.names, feed.trips

    # which stations lie inside a centre? official areas (Berlin, Saxony) by a spatial query ...
    member = {}                     # station id -> (level, centre name)
    with psycopg.connect(dsn(db)) as conn:
        inside = {}
        for lon, lat, level, name in conn.execute("""
                SELECT s.lon, s.lat, a.level, a.name FROM (SELECT DISTINCT lon, lat FROM transit_station) s
                JOIN transit_centre_area a ON ST_Intersects(a.geom, ST_SetSRID(ST_MakePoint(s.lon, s.lat), 4326))"""):
            if (lon, lat) not in inside or level < inside[(lon, lat)][0]:
                inside[(lon, lat)] = (level, name)
        # stations in regions with official levels (transit_centre_region): the main-station rule is not used there
        official = {(lon, lat) for lon, lat in conn.execute("""
                SELECT DISTINCT s.lon, s.lat FROM transit_station s JOIN transit_centre_region r
                  ON ST_Intersects(r.geom, ST_SetSRID(ST_MakePoint(s.lon, s.lat), 4326))""")}
        centres = find_centres(conn)
    log(f"{len(inside):,} station locations in official city centres; {len(centres)} main-station centres elsewhere")
    # ... and elsewhere: within CENTRE_RADIUS_KM of a main station centre (a level of its own, no official ranking)
    grid = defaultdict(list)
    for c in centres:
        grid[(int(c[1] // 0.5), int(c[2] // 0.5))].append(c)
    for sid, (lon, lat) in coord.items():
        if (lon, lat) in inside:
            member[sid] = inside[(lon, lat)]
        elif (lon, lat) not in official:
            gx, gy = int(lon // 0.5), int(lat // 0.5)
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for c in grid.get((gx + dx, gy + dy), ()):
                        if km((lon, lat), c[1:3]) <= CENTRE_RADIUS_KM:
                            member[sid] = (LVL_OTHER, c[0])
    log(f"{len(member):,} stations lie in a city centre")

    # ---- one pass over all stop times
    # per line (mode, name, destination station): totals over its trips
    line_acc = {}       # g -> [trips, sum km, sum minutes, sum stops]
    origins = defaultdict(Counter)
    # per station, line and centre level: [window wd, sa, su | whole day wd, sa, su | first, last wd | first, last sa | first, last su
    #                                      | sum km to go, sum min to go, trips]
    sl = {}
    hist = defaultdict(lambda: defaultdict(lambda: [[0] * 24 for _ in range(3)]))   # station -> level -> 3 x 24 departures per hour
    edge_lvl = {}       # (station a, station b, mode, centre level) -> [wd a->b, wd b->a, sa a->b, sa b->a, su a->b, su b->a]   (a < b)
    for n, (cat, short, m, st) in enumerate(feed.trip_stops()):
        if n % 20000 == 0:
            progress(90 * min(1, n / max(1, len(trips))), f"{n:,} trips")
        if len(st) < 2:
            continue
        sids = [s[0] for s in st]
        g = (cat, short, sids[-1])
        cum = [0.0]         # distance from the start to each stop
        for a, b in zip(sids, sids[1:]):
            cum.append(cum[-1] + (km(coord[a], coord[b]) if a in coord and b in coord else 0.0))
        total_km = cum[-1]
        t0, t1 = st[0][2], st[-1][1]        # departure at the start, arrival at the end
        total_min = (t1 - t0) / 60 if t0 is not None and t1 is not None and t1 >= t0 else 0.0
        acc = line_acc.setdefault(g, [0, 0.0, 0.0, 0])
        acc[0] += 1; acc[1] += total_km; acc[2] += total_min; acc[3] += len(st)
        origins[g][sids[0]] += 1
        # the best (lowest) level of a centre this trip stops in
        lv = [member[x][0] if x in member else LVL_NONE for x in sids]
        # A trip only counts for a centre if it really goes *through* it: at least two stops inside, or it starts / ends
        # there. A bus that just touches the rim of a centre with one stop is tangential: it counts as "without centre".
        best = LVL_NONE
        for level in sorted(set(lv) - {LVL_NONE}):
            inside = [j for j, v in enumerate(lv) if v == level]
            if len(inside) >= 2 or inside[0] == 0 or inside[0] == len(lv) - 1:
                best = level
                break
        # departures per line segment (between two neighbouring stops) for the centre filter of the line view
        for (a, _, t), (b, _, _) in zip(st, st[1:]):
            if a == b or t is None or not WINDOW[0] <= t < WINDOW[1]:
                continue
            key, d = ((a, b, cat, best), 0) if a < b else ((b, a, cat, best), 1)
            e = edge_lvl.setdefault(key, [0] * 6)
            for bit in range(3):
                if m >> bit & 1:
                    e[bit * 2 + d] += 1
        for i, (sid, _, dep) in enumerate(st[:-1]):
            if dep is None:
                continue
            s = sl.setdefault((sid, g, best), [0, 0, 0, 0, 0, 0, NO_TIME, -1, NO_TIME, -1, NO_TIME, -1, 0.0, 0.0, 0])
            h = hist[sid][best]
            in_window = WINDOW[0] <= dep < WINDOW[1]
            for bit in range(3):
                if m >> bit & 1:
                    s[3 + bit] += 1
                    if in_window:
                        s[bit] += 1
                    s[6 + bit * 2] = min(s[6 + bit * 2], dep)
                    s[7 + bit * 2] = max(s[7 + bit * 2], dep)
                    h[bit][(dep // 3600) % 24] += 1
            s[12] += total_km - cum[i]
            if t0 is not None and t1 is not None:
                s[13] += max(0.0, (t1 - dep) / 60)
            s[14] += 1
    log(f"{len(sl):,} station/line combinations, {len(line_acc):,} lines, {len(edge_lvl):,} segment/level combinations")
    progress(95, "writing")

    with psycopg.connect(dsn(db)) as conn:
        conn.execute("""CREATE TABLE transit_line (id int PRIMARY KEY, cat text, short text, origin text, dest text,
                        length_km real, duration_min real, stops real, trips int)""")
        conn.execute("""CREATE TABLE transit_station_line (lon float8, lat float8, line int, lvl smallint,
                        w_wd int, w_sa int, w_su int, d_wd int, d_sa int, d_su int,
                        first_wd int, last_wd int, first_sa int, last_sa int, first_su int, last_su int,
                        rest_km real, rest_min real)""")
        conn.execute("CREATE TABLE transit_station_hour (lon float8, lat float8, lvl smallint, wd int[], sa int[], su int[])")
        conn.execute("CREATE TABLE transit_station_area (lon float8, lat float8, centre text, level smallint)")
        ids = {}
        with conn.cursor().copy("COPY transit_line FROM STDIN") as cp:
            for i, (g, (n, sum_km, sum_min, sum_stops)) in enumerate(line_acc.items(), 1):
                ids[g] = i
                origin = origins[g].most_common(1)[0][0]
                cp.write_row((i, g[0], g[1], names.get(origin), names.get(g[2]), sum_km / n, sum_min / n, sum_stops / n, n))
        with conn.cursor().copy("COPY transit_station_line FROM STDIN") as cp:
            for (sid, g, lvl), s in sl.items():
                if sid in coord:
                    n = s[14]
                    fl = [None if v in (NO_TIME, -1) else v for v in s[6:12]]
                    cp.write_row((*coord[sid], ids[g], lvl, *s[:6], *fl, s[12] / n, s[13] / n))
        with conn.cursor().copy("COPY transit_station_hour FROM STDIN") as cp:
            for sid, by_lvl in hist.items():
                if sid in coord:
                    for lvl, h in by_lvl.items():
                        cp.write_row((*coord[sid], lvl, h[0], h[1], h[2]))
        with conn.cursor().copy("COPY transit_station_area FROM STDIN") as cp:
            for sid, (lvl, name) in member.items():
                if sid in hist and sid in coord:
                    cp.write_row((*coord[sid], name, lvl))
        for t in ("transit_station_line", "transit_station_hour", "transit_station_area"):
            conn.execute(f"CREATE INDEX ON {t} (lon, lat)")
        # for the station dots on the map: departures per station, mode and centre level
        conn.execute("""CREATE TABLE transit_station_lvl AS
            SELECT sl.lon, sl.lat, min(st.name) AS name, l.cat, sl.lvl,
                   sum(sl.w_wd)::int AS n_wd, sum(sl.w_sa)::int AS n_sa, sum(sl.w_su)::int AS n_su
            FROM transit_station_line sl JOIN transit_line l ON l.id = sl.line
            LEFT JOIN (SELECT DISTINCT lon, lat, name FROM transit_station) st ON st.lon = sl.lon AND st.lat = sl.lat
            GROUP BY sl.lon, sl.lat, l.cat, sl.lvl""")
        conn.execute("ALTER TABLE transit_station_lvl ADD COLUMN geom geometry(Point, 4326)")
        conn.execute("UPDATE transit_station_lvl SET geom = ST_SetSRID(ST_MakePoint(lon, lat), 4326)")
        conn.execute("CREATE INDEX ON transit_station_lvl USING gist (geom)")
        # departures per segment and centre level: matched with the segments of the main import by their stations
        conn.execute("""CREATE TEMP TABLE edge_stg (alon float8, alat float8, blon float8, blat float8, cat text, lvl smallint,
                        wd_ab int, wd_ba int, sa_ab int, sa_ba int, su_ab int, su_ba int)""")
        with conn.cursor().copy("COPY edge_stg FROM STDIN") as cp:
            for (a, b, cat, lvl), e in edge_lvl.items():
                if a in coord and b in coord:
                    cp.write_row((*coord[a], *coord[b], cat, lvl, *e))
        conn.execute("""CREATE TABLE transit_edge_lvl AS
            SELECT e.id AS edge_id, g.lvl, g.wd_ab, g.wd_ba, g.sa_ab, g.sa_ba, g.su_ab, g.su_ba
            FROM edge_stg g JOIN transit_edge e ON e.cat = g.cat AND e.alon = g.alon AND e.alat = g.alat AND e.blon = g.blon AND e.blat = g.blat""")
        conn.execute("CREATE INDEX ON transit_edge_lvl (edge_id)")
        conn.commit()
        log(f"{conn.execute('SELECT count(*) FROM transit_edge_lvl').fetchone()[0]:,} segment/level rows stored")
