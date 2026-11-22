"""What counts as an amenity: 10 groups, their sub-types and the OpenStreetMap tags, plus the weights of the scores.

Groups: the German retail scheme (daily / periodic / aperiodic needs, "Fristigkeit des Bedarfs") for shopping, the other
functions of the 15-minute city (health, errands, education, food, sport, green, culture).

Weights (Amenity Score): taken over from Walk Score where it has the category – groceries 3, restaurants & bars 3
(cafés folded in), shopping 2, banks 1, parks 1, schools 1 + libraries 1, entertainment 1 –, for the others:
health 3 (BBSR: pharmacy and GP are core "Nahversorgung" like the supermarket), sport 1 (15-minute city: one function
among equals), large purchases 0.5 (bought rarely, not on foot). `shares`: how the weight is spread over the nearest,
second nearest … place of the group (Walk Score counts several restaurants / shops, one bank).
"""
# (sub id, label, conditions OR'ed: (key, values) – values None = any value, extra SQL AND'ed or None)
GROUPS = [
    dict(id="daily", name="Daily needs", de="Täglicher Bedarf", weight=3, shares=[0.5, 0.3, 0.2], color="#16a34a", subs=[
        ("supermarket", "Supermarket", [("shop", ["supermarket"])], None),
        ("convenience", "Convenience, kiosk", [("shop", ["convenience", "kiosk", "general", "newsagent"])], None),
        ("bakery", "Bakery", [("shop", ["bakery", "pastry", "confectionery"])], None),
        ("butcher", "Butcher, deli", [("shop", ["butcher", "deli", "cheese", "seafood"])], None),
        ("drugstore", "Drugstore", [("shop", ["chemist"])], None),
        ("greengrocer", "Greengrocer, organic", [("shop", ["greengrocer", "farm", "health_food", "organic"])], None),
        ("beverages", "Beverages", [("shop", ["beverages", "alcohol", "wine"])], None),
    ]),
    dict(id="periodic", name="Shopping", de="Periodischer Bedarf", weight=2, shares=[0.25, 0.225, 0.2, 0.175, 0.15], color="#2563eb", subs=[
        ("clothes", "Clothes", [("shop", ["clothes", "fashion", "boutique", "bag", "fabric", "second_hand"])], None),
        ("shoes", "Shoes", [("shop", ["shoes"])], None),
        ("books", "Books, stationery", [("shop", ["books", "stationery"])], None),
        ("electronics", "Electronics, phones", [("shop", ["electronics", "mobile_phone", "computer", "hifi", "camera"])], None),
        ("sports_shop", "Sports, bicycles", [("shop", ["sports", "outdoor", "bicycle"])], None),
        ("optician", "Optician, hearing aids", [("shop", ["optician", "hearing_aids"])], None),
        ("toys", "Toys, hobby", [("shop", ["toys", "games", "hobby", "craft", "model", "music"])], None),
        ("gifts", "Gifts, jewellery, cosmetics", [("shop", ["gift", "florist", "jewelry", "watches", "perfumery", "cosmetics"])], None),
        ("department", "Department store, mall", [("shop", ["department_store", "mall", "variety_store"])], None),
        ("pets", "Pet supplies", [("shop", ["pet"])], None),
    ]),
    dict(id="aperiodic", name="Large purchases", de="Aperiodischer Bedarf", weight=0.5, shares=[1], color="#7c3aed", subs=[
        ("furniture", "Furniture, interior", [("shop", ["furniture", "kitchen", "bed", "interior_decoration", "carpet", "lighting"])], None),
        ("diy", "DIY, hardware", [("shop", ["doityourself", "hardware", "paint", "trade"])], None),
        ("garden_centre", "Garden centre", [("shop", ["garden_centre"])], None),
        ("car", "Cars, motorbikes", [("shop", ["car", "motorcycle", "car_parts", "tyres"])], None),
        ("appliance", "Household appliances", [("shop", ["appliance"])], None),
    ]),
    dict(id="health", name="Health", de="Gesundheit", weight=3, shares=[0.5, 0.3, 0.2], color="#dc2626", subs=[
        ("pharmacy", "Pharmacy", [("amenity", ["pharmacy"])], None),
        ("doctor", "Doctor", [("amenity", ["doctors"]), ("healthcare", ["doctor"])], None),
        ("dentist", "Dentist", [("amenity", ["dentist"]), ("healthcare", ["dentist"])], None),
        ("hospital", "Hospital, clinic", [("amenity", ["hospital", "clinic"])], None),
        ("therapy", "Therapy", [("healthcare", ["physiotherapist", "psychotherapist", "occupational_therapist", "speech_therapist"])], None),
        ("care", "Care home", [("amenity", ["nursing_home"])], None),
    ]),
    dict(id="errands", name="Errands", de="Erledigungen", weight=1, shares=[0.6, 0.4], color="#ca8a04", subs=[
        ("bank", "Bank", [("amenity", ["bank"])], None),
        ("atm", "Cash machine", [("amenity", ["atm"])], None),
        ("post", "Post, parcels", [("amenity", ["post_office", "parcel_locker"])], None),
        ("hairdresser", "Hairdresser, beauty", [("shop", ["hairdresser", "beauty", "massage", "tattoo"])], None),
        ("services", "Laundry, tailor, repair", [("shop", ["laundry", "dry_cleaning", "tailor", "shoe_repair", "copyshop", "key_cutter"])], None),
        ("public", "Town hall, police", [("amenity", ["townhall", "police", "courthouse"]), ("office", ["government"])], None),
    ]),
    dict(id="education", name="Education", de="Bildung", weight=2, shares=[0.5, 0.5], color="#0891b2", subs=[
        ("kindergarten", "Kindergarten", [("amenity", ["kindergarten", "childcare"])], None),
        ("school", "School", [("amenity", ["school"])], None),
        ("library", "Library", [("amenity", ["library"])], None),
        ("university", "University, college", [("amenity", ["university", "college"])], None),
        ("courses", "Music, language school", [("amenity", ["music_school", "language_school", "dancing_school"])], None),
    ]),
    dict(id="food", name="Food & drink", de="Gastronomie", weight=3, shares=[0.25, 0.15, 0.083, 0.083, 0.075, 0.075, 0.075, 0.075, 0.067, 0.067],
         color="#ea580c", subs=[
        ("restaurant", "Restaurant", [("amenity", ["restaurant"])], None),
        ("cafe", "Café", [("amenity", ["cafe"])], None),
        ("fast_food", "Fast food", [("amenity", ["fast_food", "food_court"])], None),
        ("bar", "Bar, pub", [("amenity", ["bar", "pub", "biergarten"])], None),
        ("ice_cream", "Ice cream", [("amenity", ["ice_cream"])], None),
    ]),
    dict(id="sport", name="Sport & leisure", de="Sport & Freizeit", weight=1, shares=[0.6, 0.4], color="#65a30d", subs=[
        ("climbing", "Climbing", [("sport", ["climbing", "bouldering"])], "{leisure} IS DISTINCT FROM 'pitch'"),
        ("swimming", "Swimming", [("amenity", ["public_bath"]), ("leisure", ["water_park"]), ("sport", ["swimming"])],
         "coalesce({access}, '') NOT IN ('private', 'no', 'customers')"),
        ("fitness", "Fitness", [("leisure", ["fitness_centre"])], None),
        ("sports_centre", "Sports centre, hall", [("leisure", ["sports_centre", "stadium", "ice_rink", "sports_hall"])], None),
        ("pitch", "Pitch", [("leisure", ["pitch", "track"])], "coalesce({access}, '') NOT IN ('private', 'no')"),
        ("playground", "Playground", [("leisure", ["playground"])], None),
        ("leisure", "Bowling, minigolf …", [("leisure", ["bowling_alley", "miniature_golf", "escape_game", "amusement_arcade", "golf_course"])], None),
    ]),
    dict(id="green", name="Parks & green", de="Grün", weight=1, shares=[1], color="#15803d", green=True, subs=[
        ("park", "Park", [("leisure", ["park"])], None),
        ("garden", "Public garden", [("leisure", ["garden"])], "coalesce({access}, 'yes') IN ('yes', 'public', 'permissive') AND {name} IS NOT NULL"),
        ("recreation", "Recreation ground", [("landuse", ["recreation_ground", "village_green"])], None),
        ("forest", "Forest", [("landuse", ["forest"]), ("natural", ["wood"])], None),
        ("nature", "Nature reserve", [("leisure", ["nature_reserve"])], None),
    ]),
    dict(id="culture", name="Culture & sights", de="Kultur & Sehenswertes", weight=1, shares=[0.6, 0.4], color="#db2777", subs=[
        ("museum", "Museum, gallery", [("tourism", ["museum", "gallery"])], None),
        ("theatre", "Theatre, concerts", [("amenity", ["theatre", "arts_centre", "concert_hall"])], None),
        ("cinema", "Cinema", [("amenity", ["cinema"])], None),
        ("attraction", "Sight", [("tourism", ["attraction"])], None),
        ("monument", "Castle, monument", [("historic", ["castle", "monument", "ruins", "city_gate", "fort", "palace"])], None),
        ("nightlife", "Club", [("amenity", ["nightclub"])], None),
        ("community", "Community centre", [("amenity", ["community_centre"])], None),
        ("zoo", "Zoo, theme park", [("tourism", ["zoo", "theme_park", "aquarium"])], None),     # new subs at the end: old tables stay valid
        ("viewpoint", "Viewpoint", [("tourism", ["viewpoint"])], None),
    ]),
]
GROUP_IDS = [g["id"] for g in GROUPS]

# Attractiveness: what is within ATTRACT_M (straight line) of a place, one index per field.
#   weight w   how often people go there (rough visits per household and month, supermarket = 10: food shopping 2–3 × a
#              week, clothes once a month, furniture once a year – German retail scheme "Fristigkeit des Bedarfs").
#   ratio r    diminishing returns: the 2nd place of a type adds r × w, the 3rd r² × w … (geometric / exponential
#              saturation, like Walk Score's falling shares for the 1st, 2nd … restaurant): a type gives at most w / (1 − r).
#              Low r where one is enough (supermarket, school), high r where choice itself is the value ("love of variety":
#              restaurants, clothes – Schiff 2015, Bakens et al.; shops cluster for comparison shopping).
#   Green spaces (group "green") count once per type when their edge is within the radius.
# Fields after the functions of the 15-minute city (Moreno et al. 2021), third places after Oldenburg 1989. Fields overlap.
# Index 0–100: the points as a share of the most a field can give (attract_max), so the fields compare.
# Types: {sub id: w} uses the field's ratio r, {sub id: (w, r)} its own.
ATTRACT_M = 400
ATTRACT = [
    dict(id="a_daily", name="Daily needs", r=0.4, parts={
        "daily": {"supermarket": (10, 0.5), "bakery": 6, "drugstore": 3, "convenience": 2, "butcher": 2, "greengrocer": 2, "beverages": 1.5},
        "health": {"pharmacy": 2}}),
    dict(id="a_periodic", name="Periodic needs", r=0.8, parts={
        "periodic": {"clothes": (1.5, 0.9), "department": (1.5, 0.5), "gifts": 1, "books": 0.8, "shoes": 0.6, "pets": (0.6, 0.5),
                     "electronics": 0.5, "sports_shop": 0.5, "toys": 0.5, "optician": (0.3, 0.5)}}),
    dict(id="a_episodic", name="Episodic needs", r=0.6, parts={
        "aperiodic": {"diy": 0.5, "furniture": (0.3, 0.8), "garden_centre": 0.3, "car": 0.2, "appliance": 0.2}}),
    dict(id="a_health", name="Health", r=0.5, parts={
        "health": {"pharmacy": (3, 0.3), "doctor": (3, 0.75), "dentist": 1, "therapy": 1, "hospital": 1}}),
    dict(id="a_education", name="Education", r=0.4, parts={
        "education": {"kindergarten": (3, 0.5), "school": 3, "library": 1, "university": 1, "courses": (1, 0.6)}}),
    dict(id="a_culture", name="Culture", r=0.7, parts={
        "culture": {"museum": 3, "theatre": 3, "cinema": 2, "nightlife": 1, "community": (1, 0.4)}, "education": {"library": (2, 0.4)}}),
    dict(id="a_sights", name="Sights", r=0.7, parts={
        "culture": {"attraction": 3, "zoo": (3, 0.5), "museum": 2, "monument": 2, "viewpoint": (1, 0.5)}}),
    dict(id="a_food", name="Dining", r=0.85, parts={
        "food": {"restaurant": (3, 0.92), "cafe": (3, 0.75), "fast_food": (2, 0.6), "bar": (1.5, 0.75), "ice_cream": (1.5, 0.5)}}),
    dict(id="a_recreation", name="Recreation & sport", r=0.5, parts={
        "green": {"park": 5, "forest": 3, "recreation": 2, "nature": 2, "garden": 1},
        "sport": {"playground": 2, "fitness": 2, "sports_centre": 2, "swimming": 2, "pitch": 1, "climbing": 1, "leisure": 1}}),
    dict(id="a_third", name="Third places", r=0.75, parts={
        "food": {"cafe": 3, "bar": 2, "ice_cream": (1, 0.5)}, "education": {"library": (2, 0.4)}, "culture": {"community": (2, 0.4)},
        "daily": {"bakery": (1, 0.5)}, "errands": {"hairdresser": (1, 0.5)}}),
]
ATTRACT_SUPPLY = ("a_daily", "a_periodic", "a_episodic")     # "a_supply": the three together


def attract_weights(metric: str) -> dict:
    """{(group index, sub index): (weight, ratio)} of an attractiveness metric (a_supply: daily + periodic + episodic)."""
    ids = ATTRACT_SUPPLY if metric == "a_supply" else (metric,)
    out = {}
    for a in ATTRACT:
        if a["id"] in ids:
            for gid, subs in a["parts"].items():
                g = GROUPS[GROUP_IDS.index(gid)]
                sub_ids = [s[0] for s in g["subs"]]
                for sid, v in subs.items():
                    out[(GROUP_IDS.index(gid), sub_ids.index(sid))] = v if isinstance(v, tuple) else (v, a["r"])
    return out


def attract_max(metric: str) -> float:
    """The most points a field can give (every type saturated): 100 % of the index."""
    green = GROUP_IDS.index("green")
    return sum(w if g == green else w / (1 - r) for (g, _), (w, r) in attract_weights(metric).items())


def attract_catalog() -> list[dict]:
    """For the browser: the fields with [weight, ratio] per type."""
    return [a | {"parts": {g: {s: list(v) if isinstance(v, tuple) else [v, a["r"]] for s, v in subs.items()}
                           for g, subs in a["parts"].items()}} for a in ATTRACT]


GREEN = GROUP_IDS.index("green")
GREEN_MIN_M2 = 5000            # smaller green spaces are not counted (house gardens, verges)

# columns of the osm2pgsql tables; other keys are in the hstore "tags"
COLUMNS = {"access", "amenity", "historic", "landuse", "leisure", "name", "natural", "office", "shop", "sport", "tourism"}


def col(key: str, t: str = "t") -> str:
    return f'{t}."{key}"' if key in COLUMNS else f"{t}.tags -> '{key}'"


def code_sql(t: str = "t", green: bool | None = None) -> str:
    """SQL CASE giving group * 100 + sub index of a row of planet_osm_point / _polygon (NULL: no amenity).
    green: only the green group (True), all but green (False), all (None)."""
    whens = []
    for gi, g in enumerate(GROUPS):
        if green is not None and bool(g.get("green")) != green:
            continue
        for si, (_, _, conds, extra) in enumerate(g["subs"]):
            cond = " OR ".join(f"{col(k, t)} IS NOT NULL" if v is None else f"{col(k, t)} IN ({', '.join(repr(x) for x in v)})" for k, v in conds)
            if extra:
                cond = f"({cond}) AND {extra.format(**{k: col(k, t) for k in COLUMNS})}"
            whens.append(f"WHEN {cond} THEN {gi * 100 + si}")
    return "CASE " + " ".join(whens) + " END"


def version() -> str:
    """Changes when the tags behind the groups change: amenity_poi made with another one must be prepared again."""
    import hashlib
    return hashlib.sha1((code_sql("t", green=False) + code_sql("t", green=True) + str(GREEN_MIN_M2)).encode()).hexdigest()[:12]


def catalog() -> list[dict]:
    """For the browser: groups with their sub-types."""
    return [{k: v for k, v in g.items() if k != "subs"} | {"subs": [{"id": s[0], "name": s[1]} for s in g["subs"]]} for g in GROUPS]
