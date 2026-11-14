"""Import a population grid into the database "population" (table pop_<id>).

    venv/bin/python -m pipeline.population zensus_de
    venv/bin/python -m pipeline.population ghs_eu [--source <tif path or /vsicurl/url>] [--bbox w,s,e,n]

Columns: cx, cy (cell centre in the grid's coordinate system, metres), geom, pop and — only for the census —
age / household / housing values. Missing values are NULL, so every query works with every dataset.
Data: Zensus 2022 © Statistische Ämter des Bundes und der Länder (dl-de/by-2.0); GHS-POP © European Union, JRC (CC BY 4.0).
"""
import csv
import io
import shutil
import sys
import zipfile

import psycopg

from backend import catalog
from backend.common import DATA, DB_POP, dsn, update_state
from .util import download, log, progress, recreate_db

ZENSUS_BASE = "https://www.destatis.de/static/DE/zensus/gitterdaten"
# zip name : csv name : column
ZENSUS_TOPICS = [
    ("Zensus2022_Bevoelkerungszahl", "Zensus2022_Bevoelkerungszahl_100m-Gitter.csv", "pop"),
    ("Durchschnittsalter_in_Gitterzellen", "Zensus2022_Durchschnittsalter_100m-Gitter.csv", "avg_age"),
    ("Anteil_unter_18-jaehrige_in_Gitterzellen", "Zensus2022_Anteil_unter_18_100m-Gitter.csv", "pct_under18"),
    ("Anteil_ab_65-jaehrige_in_Gitterzellen", "Zensus2022_Anteil_ueber_65_100m-Gitter.csv", "pct_over65"),
    ("Auslaenderanteil_in_Gitterzellen", "Zensus2022_Anteil_Auslaender_100m-Gitter.csv", "pct_foreign"),
    ("Durchschnittliche_Haushaltsgroesse_in_Gitterzellen", "Zensus2022_Durchschn_Haushaltsgroesse_100m-Gitter.csv", "hh_size"),
    ("Zensus2022_Durchschn_Nettokaltmiete", "Zensus2022_Durchschn_Nettokaltmiete_100m-Gitter.csv", "rent_sqm"),
    ("Eigentuemerquote_in_Gitterzellen", "Zensus2022_Eigentuemerquote_100m-Gitter.csv", "pct_owner"),
    ("Leerstandsquote_in_Gitterzellen", "Zensus2022_Leerstandsquote_100m-Gitter.csv", "pct_vacant"),
    ("Durchschnittliche_Wohnflaeche_je_Bewohner_in_Gitterzellen", "Zensus2022_Durchschn_Flaeche_je_Bewohner_100m-Gitter.csv", "living_space"),
]
STATS = [c for _, _, c in ZENSUS_TOPICS[1:]]


def create_table(conn, table: str, srid: int):
    conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.execute(f"""CREATE TABLE {table} (cx int NOT NULL, cy int NOT NULL, pop float8 NOT NULL,
        {", ".join(c + " real" for c in STATS)})""")


def finish(conn, table: str, srid: int):
    conn.execute(f"ALTER TABLE {table} ADD COLUMN geom geometry(Point, {srid})")
    conn.execute(f"UPDATE {table} SET geom = ST_SetSRID(ST_MakePoint(cx, cy), {srid})")
    conn.execute(f"CREATE INDEX ON {table} USING gist (geom)")
    conn.execute(f"ANALYZE {table}")
    return conn.execute(f"SELECT count(*), sum(pop) FROM {table}").fetchone()


def import_zensus(conn, table: str):
    raw = DATA / "zensus"
    create_table(conn, table, 3035)
    conn.execute(f"CREATE UNIQUE INDEX ON {table} (cx, cy)")
    for i, (zipname, csvname, col) in enumerate(ZENSUS_TOPICS):
        lo, hi = 100 * i / 10, 100 * (i + 1) / 10
        z = raw / f"{zipname}.zip"
        if not z.exists():
            download(f"{ZENSUS_BASE}/{zipname}.zip", z, col, lo * .8, (lo + (hi - lo) * .5) * .8)
        progress(lo * .8 + 4, f"importing {col}")
        conn.execute("CREATE TEMP TABLE stg (id text, x int, y int, val text, flag text) ON COMMIT DROP")
        with zipfile.ZipFile(z) as zf, zf.open(csvname) as f, conn.cursor().copy("COPY stg FROM STDIN") as cp:
            rd = csv.reader(io.TextIOWrapper(f, encoding="utf-8-sig"), delimiter=";")
            next(rd)
            for row in rd:
                cp.write_row((row + [None] * 5)[:5])
        if col == "pop":
            conn.execute(f"INSERT INTO {table} (cx, cy, pop) SELECT x, y, val::int FROM stg")
        else:   # '–' means "no value"; decimals use a comma
            conn.execute(f"""UPDATE {table} c SET {col} = replace(s.val, ',', '.')::real FROM stg s
                             WHERE c.cx = s.x AND c.cy = s.y AND s.val ~ '^[0-9]+(,[0-9]+)?$'""")
        conn.commit()
    shutil.rmtree(raw, ignore_errors=True)         # the numbers are in the database now


def import_ghs(conn, table: str, bbox, source: str | None):
    import numpy as np
    import rasterio
    from rasterio.warp import transform_bounds
    from rasterio.windows import Window, from_bounds

    tif = DATA / "ghs" / "pop2020_1km.tif"
    if source is None:
        if not tif.exists():
            download(catalog.GHS_URL, tif, "GHS-POP", 0, 45)
        source = str(tif)
    create_table(conn, table, 54009)
    with rasterio.open(source) as src, conn.cursor().copy(f"COPY {table} (cx, cy, pop) FROM STDIN") as cp:
        win = Window(0, 0, src.width, src.height)
        if bbox:
            x0, y0, x1, y1 = transform_bounds("EPSG:4326", src.crs, *bbox, densify_pts=21)
            win = from_bounds(x0, y0, x1, y1, src.transform).round_offsets().round_lengths().intersection(win)
        step = 512
        for r0 in range(int(win.row_off), int(win.row_off + win.height), step):
            h = min(step, int(win.row_off + win.height) - r0)
            progress(45 + 45 * (r0 - win.row_off) / win.height, "importing")
            blk = src.read(1, window=Window(win.col_off, r0, win.width, h))
            nodata = src.nodata
            rows, cols = np.nonzero((blk > 0.05) & (blk != nodata) if nodata is not None else blk > 0.05)
            for rr, cc in zip(rows, cols):
                x, y = src.transform * (win.col_off + cc + .5, r0 + rr + .5)
                cp.write_row((round(x), round(y), round(float(blk[rr, cc]), 1)))
    shutil.rmtree(tif.parent, ignore_errors=True)


def main(ds: str, source: str | None = None, bbox=None):
    info = catalog.POPULATION[ds]
    table = f"pop_{ds}"
    try:
        psycopg.connect(dsn(DB_POP)).close()
    except psycopg.OperationalError:
        recreate_db(DB_POP)
    with psycopg.connect(dsn(DB_POP)) as conn:
        if ds == "zensus_de":
            import_zensus(conn, table)
        else:
            import_ghs(conn, table, bbox or info.get("bbox"), source)
        conn.commit()
        progress(95, "indexing")
        n, total = finish(conn, table, info["srid"])
        conn.commit()
    log(f"{n:,} cells, {total:,.0f} inhabitants")

    def mark(st):
        p = st.setdefault("population", {})
        p.setdefault("installed", [])
        if ds not in p["installed"]:
            p["installed"].append(ds)
        p.setdefault("active", ds)
    update_state(mark)


if __name__ == "__main__":
    a = sys.argv[1:]
    opt = lambda k: a[a.index(k) + 1] if k in a else None
    main(a[0], opt("--source"), tuple(map(float, opt("--bbox").split(","))) if opt("--bbox") else None)
