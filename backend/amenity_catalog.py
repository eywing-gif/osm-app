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
        ("attraction", "Sight, viewpoint, zoo", [("tourism", ["attraction", "viewpoint", "zoo", "theme_park", "aquarium"])], None),
        ("monument", "Castle, monument", [("historic", ["castle", "monument", "ruins", "city_gate", "fort", "palace"])], None),
        ("nightlife", "Club", [("amenity", ["nightclub"])], None),
        ("community", "Community centre", [("amenity", ["community_centre"])], None),
    ]),
]
GROUP_IDS = [g["id"] for g in GROUPS]
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


def catalog() -> list[dict]:
    """For the browser: groups with their sub-types."""
    return [{k: v for k, v in g.items() if k != "subs"} | {"subs": [{"id": s[0], "name": s[1]} for s in g["subs"]]} for g in GROUPS]
