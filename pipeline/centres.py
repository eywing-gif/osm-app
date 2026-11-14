"""Official central places ("Zentrale Orte") of Berlin and Saxony with the area of their city centre.

Level of a central place: 1 Oberzentrum, 2 Mittelzentrum, 3 Grundzentrum.
  Saxony   Oberzentren and Mittelzentren: Landesentwicklungsplan Sachsen 2013 (Z 1.3.6, Z 1.3.7; incl. the oberzentraler
           Staedteverbund Bautzen-Goerlitz-Hoyerswerda and the mittelzentrale Verbuende).
           Grundzentren: Regionalplaene 2020-2024, as shown in the Raumordnungsbericht 2025 of the Saechsisches
           Staatsministerium fuer Infrastruktur und Landesentwicklung (Abb. 4.1.1-2).
  Berlin   is an Oberzentrum.
The "centre" is not the whole town but its city centre: the OSM districts of the inner city (Altstadt, Innenstadt,
Zentrum, Mitte ...) where they exist, otherwise a disc around the middle of the town.
Only the states that are in the map database are used. Everywhere else pipeline.lines falls back to main stations.
Result: tables transit_centre_area (the centres) and transit_centre_region (where the official lists apply).
Data: official lists (see above); geometries © OpenStreetMap contributors (ODbL).
"""
import re

import psycopg

from backend.common import DB_OSM, dsn
from .util import log


OZ = ["Dresden", "Leipzig", "Chemnitz", "Zwickau", "Plauen",
      "Bautzen", "Görlitz", "Hoyerswerda"]                     # the last three: oberzentraler Staedteverbund
MZ = ["Annaberg-Buchholz", "Borna", "Coswig", "Crimmitschau", "Delitzsch", "Dippoldiswalde", "Döbeln", "Eilenburg",
      "Freiberg", "Freital", "Glauchau", "Grimma", "Großenhain", "Kamenz", "Limbach-Oberfrohna", "Löbau", "Marienberg",
      "Markkleeberg", "Meißen", "Mittweida", "Niesky", "Oelsnitz/Vogtl.", "Oschatz", "Pirna", "Radeberg", "Radebeul",
      "Reichenbach im Vogtland", "Riesa", "Schkeuditz", "Stollberg/Erzgeb.", "Torgau", "Weißwasser/O.L.", "Werdau",
      "Wurzen", "Zittau",
      # mittelzentrale Verbuende
      "Auerbach/Vogtl.", "Ellefeld", "Falkenstein/Vogtl.", "Rodewisch",                       # Goeltzschtal
      "Hohenstein-Ernstthal", "Lichtenstein/Sa.", "Oberlungwitz",                             # Sachsenring
      "Aue", "Lauter-Bernsbach", "Lößnitz", "Bad Schlema", "Schneeberg", "Schwarzenberg/Erzgeb."]   # Silberberg
GZ = ["Adorf", "Altenberg", "Bad Düben", "Bad Gottleuba-Berggießhübel", "Bad Lausick", "Bad Schandau", "Beilrode",
      "Belgern-Schildau", "Bernsdorf", "Bernstadt a. d. Eigen", "Bischofswerda", "Borsdorf", "Brand-Erbisdorf", "Brandis",
      "Breitenbrunn/Erzgeb.", "Burgstädt", "Böhlen", "Colditz", "Dahlen", "Dommitzsch", "Ebersbach-Neugersdorf",
      "Eibenstock", "Eppendorf", "Flöha", "Frankenberg/Sa.", "Frauenstein", "Frohburg", "Geithain", "Geyer", "Glashütte",
      "Groitzsch", "Großdubrau", "Großpösna", "Großröhrsdorf", "Großschönau", "Gröditz", "Hainichen", "Hartenstein",
      "Hartha", "Heidenau", "Johanngeorgenstadt", "Kirchberg", "Klingenberg", "Klingenthal/Sa.", "Königsbrück",
      "Königstein/Sächs. Schw.", "Königswartha", "Leisnig", "Lengenfeld", "Lugau/Erzgeb.", "Malschwitz", "Markneukirchen",
      "Markranstädt", "Meerane", "Mügeln", "Naunhof", "Neukirch/Lausitz", "Neustadt in Sachsen", "Nossen", "Nünchritz",
      "Oederan", "Oelsnitz/Erzgeb.", "Olbernhau", "Pausa-Mühltroff", "Pegau", "Penig", "Pockau-Lengefeld", "Pulsnitz",
      "Radeburg", "Radibor", "Reichenbach/O.L.", "Rochlitz", "Rothenburg/O.L.", "Roßwein", "Scheibenberg",
      "Schirgiswalde-Kirschau", "Schlettau", "Schöneck/Vogtl.", "Schönfeld", "Schönheide", "Sebnitz",
      "Sohland a. d. Spree", "Stolpen", "Stützengrün", "Taucha", "Thalheim/Erzgeb.", "Thiendorf", "Thum", "Treuen",
      "Waldenburg", "Waldheim", "Weischlitz", "Weißenberg", "Wermsdorf", "Wildenfels", "Wilsdruff", "Wilthen",
      "Wittichenau", "Zeithain", "Zschopau", "Zschorlau", "Zwenkau", "Zwönitz"]

# The inner city of the biggest places is made of several OSM districts (osm_id of the relation, negative as in osm2pgsql).
CITY_CENTRES = {
    "Leipzig": [-6273540, -6273541, -13793789, -6273543, -6273544],   # Zentrum and Zentrum-Nord, -Ost, -Sued, -West
}
# Chemnitz: the area enclosed by Bahnhofstrasse, Brueckenstrasse and Theaterstrasse (taken from the OSM streets; the
# bounding box in EPSG:3857 just limits the search to the city centre)
STREET_AREAS = {
    "Chemnitz": (("Bahnhofstraße", "Brückenstraße", "Theaterstraße"), (1435000, 6588000, 1442000, 6596000)),
}
# Dresden: the "26er Ring", the ring around the inner city that is named after the old ring tram line 26. Clockwise from the
# Marienbruecke: Antonstrasse - Albertplatz - Bautzner Strasse / Glacisstrasse - Rosa-Luxemburg-Platz - Albertbruecke -
# Sachsenplatz / Sachsenallee - Strassburger Platz - Hauptbahnhof - Ammonstrasse - Freiberger Strasse - Koenneritzstrasse.
# (lon, lat) of the corners; it includes both river banks (Altstadt and Innere Neustadt).
CUSTOM_AREAS = {
    "Dresden": [(13.7313, 51.0610), (13.7405, 51.0660), (13.7463, 51.0635), (13.7507, 51.0610), (13.7535, 51.0592),
                (13.7575, 51.0545), (13.7570, 51.0480), (13.7555, 51.0455), (13.7440, 51.0425), (13.7330, 51.0405),
                (13.7254, 51.0450), (13.7197, 51.0485), (13.7215, 51.0560)],
}
# municipalities whose official name is a combination: the OSM place is called differently
ALIASES = {
    "Reichenbach im Vogtland": ["Reichenbach/Vogtl.", "Reichenbach im Vogtland"],
    "Lauter-Bernsbach": ["Lauter", "Bernsbach"],
    "Bad Gottleuba-Berggießhübel": ["Bad Gottleuba", "Berggießhübel"],
    "Belgern-Schildau": ["Belgern", "Schildau"],
    "Bernstadt a. d. Eigen": ["Bernstadt a.d. Eigen", "Bernstadt auf dem Eigen", "Bernstadt"],
    "Ebersbach-Neugersdorf": ["Ebersbach/Sa.", "Neugersdorf", "Ebersbach"],
    "Pausa-Mühltroff": ["Pausa/Vogtl.", "Pausa", "Mühltroff"],
    "Pockau-Lengefeld": ["Lengefeld", "Pockau"],
    "Sohland a. d. Spree": ["Sohland an der Spree", "Sohland a.d. Spree", "Sohland"],
}
# Berlin has no single "Altstadt": the centre is made of a few discs around its main squares and stations
BERLIN_DISCS = [(13.4132, 52.5219, 1.0),    # Alexanderplatz
                (13.3777, 52.5163, 1.0),    # Brandenburger Tor / Unter den Linden
                (13.3694, 52.5251, 0.8),    # Hauptbahnhof
                (13.3759, 52.5096, 0.7)]    # Potsdamer Platz
INNER_NAMES = ("Innenstadt", "Altstadt", "Zentrum", "Stadtmitte", "Stadtzentrum", "Stadtkern", "Innere Stadt", "Mitte", "City")
DISC_KM = {1: 1.0, 2: 1.0, 3: 0.7}      # Mittel- and Grundzentren: as before (the shrunk circles missed too many stations)
MAX_AREA = {1: 10400000, 2: 22000000, 3: 22000000}   # biggest district (EPSG:3857 m2, about 2.6 x real) that may stand for a centre      # radius of the disc around the middle of a place without a district polygon
SAXONY, BERLIN = -62467, -62422          # osm_id of the state boundaries


def load_places(osm):
    """All named place nodes / place areas inside Saxony, read once: [(name, place, lon, lat)]."""
    osm.execute("CREATE TEMP TABLE sax AS SELECT ST_Union(way) AS g FROM planet_osm_polygon WHERE osm_id = %s AND boundary = 'administrative'" % SAXONY)
    out = []
    for geom, table in (("way", "planet_osm_point"), ("ST_PointOnSurface(way)", "planet_osm_polygon")):
        out += osm.execute(f"""SELECT name, place, ST_X(ST_Transform({geom}, 4326)), ST_Y(ST_Transform({geom}, 4326))
                               FROM {table}, sax WHERE place IN ('city','town','village','suburb','hamlet') AND name IS NOT NULL
                               AND ST_Within({geom}, sax.g)""").fetchall()
    return out


def find_place(places, name: str):
    """Middle of a place: its OSM place node inside Saxony. Names are matched exactly first, then without the
    suffix after '/' ('Stollberg/Erzgeb.' -> 'Stollberg/...'). Returns (lon, lat, osm name) or None."""
    base = re.split(r"[/(]", name)[0].strip()
    norm = lambda t: t.lower().replace(" a. d. ", " a.d. ")
    rank = {"city": 0, "town": 1, "village": 2}
    tests = (lambda n: norm(n) == norm(name),
             lambda n: norm(n).startswith(norm(base) + "/") or norm(n).startswith(norm(base) + " "),
             lambda n: norm(n).startswith(norm(base)))
    for test in tests:
        hits = [p for p in places if test(p[0])]
        if hits:
            n, place, lon, lat = min(hits, key=lambda p: (rank.get(p[1], 3), len(p[0])))
            return lon, lat, n
    return None


def inner_city(osm, lon, lat, max_area):
    """The district that is the inner city: a polygon with a name like Altstadt / Innenstadt / Zentrum next to the middle."""
    return osm.execute("""
        WITH p AS (SELECT ST_Transform(ST_SetSRID(ST_MakePoint(%s, %s), 4326), 3857) AS g)
        SELECT ST_AsText(ST_Transform(way, 4326)), name FROM cand, p
        WHERE ST_DWithin(way, p.g, 2500) AND ST_Area(way) BETWEEN 60000 AND %s
        ORDER BY ST_Distance(way, p.g), ST_Area(way) LIMIT 1""", (lon, lat, max_area)).fetchone()


def main(db):
    osm = psycopg.connect(dsn(DB_OSM))
    tr = psycopg.connect(dsn(db))
    have = {oid: osm.execute("SELECT count(*) FROM planet_osm_polygon WHERE osm_id = %s AND boundary = 'administrative'", (oid,)).fetchone()[0] > 0
            for oid in (SAXONY, BERLIN)}
    tr.execute("DROP TABLE IF EXISTS transit_centre_area")
    tr.execute("""CREATE TABLE transit_centre_area (id serial PRIMARY KEY, name text, level smallint, source text,
                  geom geometry(Geometry, 4326))""")
    tr.execute("DROP TABLE IF EXISTS transit_centre_region")
    tr.execute("CREATE TABLE transit_centre_region (geom geometry(Geometry, 4326))")
    for oid, present in have.items():
        if present:
            wkt = osm.execute("""SELECT ST_AsText(ST_Transform(ST_Simplify(ST_Union(way), 200), 4326)) FROM planet_osm_polygon
                                 WHERE osm_id = %s AND boundary = 'administrative'""", (oid,)).fetchone()[0]
            tr.execute("INSERT INTO transit_centre_region VALUES (ST_GeomFromText(%s, 4326))", (wkt,))
    tr.execute("CREATE INDEX ON transit_centre_region USING gist (geom)")
    tr.commit()
    if not any(have.values()):
        log("no state with official central places in the map data")
        return
    places = load_places(osm) if have[SAXONY] else []
    osm.execute("""CREATE TEMP TABLE cand AS SELECT name, way FROM planet_osm_polygon
                   WHERE name = ANY(%s) AND (admin_level IN ('9','10','11') OR place IN ('suburb','quarter','neighbourhood'))""",
                (list(INNER_NAMES),))
    osm.execute("CREATE INDEX ON cand USING gist (way)")
    log(f"{len(places)} places and {osm.execute('SELECT count(*) FROM cand').fetchone()[0]} inner-city candidates loaded")
    rows = []           # (name, level, source, wkt)
    missing = []

    if have[BERLIN]:        # the district Mitte
        for lon, lat, km in BERLIN_DISCS:
            rows.append(("Berlin (Mitte)", 1, f"disc:{km}", f"POINT({lon} {lat})"))

    for level, names in ((1, OZ), (2, MZ), (3, GZ)) if have[SAXONY] else ():
        for name in names:
            if name in CUSTOM_AREAS:
                ring = CUSTOM_AREAS[name] + CUSTOM_AREAS[name][:1]
                rows.append((name, level, "polygon", "POLYGON((" + ", ".join(f"{x} {y}" for x, y in ring) + "))"))
                continue
            if name in STREET_AREAS:
                names_, bbox = STREET_AREAS[name]
                cell = osm.execute("""
                    WITH env AS (SELECT ST_MakeEnvelope(%s, %s, %s, %s, 3857) AS e),
                    l AS (SELECT ST_Intersection(way, env.e) AS g FROM planet_osm_line, env
                          WHERE name = ANY(%s) AND highway IN ('primary','secondary','tertiary','construction') AND way && env.e),
                    cells AS (SELECT (ST_Dump(ST_Polygonize(g))).geom AS p FROM (SELECT ST_Node(ST_Collect(g)) AS g FROM l) x)
                    SELECT ST_AsText(ST_Transform(p, 4326)) FROM cells ORDER BY ST_Area(p) DESC LIMIT 1""",
                                   (*bbox, list(names_))).fetchone()
                if cell and cell[0]:
                    rows.append((name, level, "polygon", cell[0]))
                    continue
            if name in CITY_CENTRES:
                parts = [osm.execute("SELECT ST_AsText(ST_Transform(way, 4326)) FROM planet_osm_polygon WHERE osm_id = %s", (oid,)).fetchone()
                         for oid in CITY_CENTRES[name]]
                parts = [p[0] for p in parts if p]
                if parts:
                    for w in parts:
                        rows.append((name, level, "polygon", w))
                    continue
            place = next((p for n in [name, *ALIASES.get(name, [])] if (p := find_place(places, n))), None)
            if place is None:
                missing.append(name)
                continue
            lon, lat, _ = place
            poly = inner_city(osm, lon, lat, MAX_AREA[level]) if level <= 2 else None
            if poly:
                rows.append((name, level, "polygon", poly[0]))
            else:
                rows.append((name, level, "disc", f"POINT({lon} {lat})"))
    log(f"{len(rows)} areas for {len({r[0] for r in rows})} places; no place found for: {missing}")

    for name, level, source, wkt in rows:
        if source.startswith("disc"):
            km = float(source.split(":")[1]) if ":" in source else DISC_KM[level]
            tr.execute("""INSERT INTO transit_centre_area (name, level, source, geom)
                          SELECT %s, %s, 'disc', ST_Transform(ST_Buffer(ST_Transform(ST_GeomFromText(%s, 4326), 25833), %s), 4326)""",
                       (name, level, wkt, km * 1000))
        else:
            tr.execute("INSERT INTO transit_centre_area (name, level, source, geom) VALUES (%s, %s, 'polygon', ST_Multi(ST_GeomFromText(%s, 4326)))",
                       (name, level, wkt))
    tr.execute("CREATE INDEX ON transit_centre_area USING gist (geom)")
    # only what the timetable covers (a feed for another country would show them for nothing)
    tr.execute("""DELETE FROM transit_centre_area WHERE NOT geom && (SELECT ST_Expand(ST_Extent(geom), 0.1) FROM transit_station)""")
    tr.commit()
    for lv, n, poly in tr.execute("SELECT level, count(DISTINCT name), count(*) FILTER (WHERE source = 'polygon') FROM transit_centre_area GROUP BY level ORDER BY level"):
        log(f"level {lv}: {n} places, {poly} of the areas are real city-centre districts, the rest are discs")

