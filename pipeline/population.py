"""Import a population grid into the database "population" (table pop_<id>).

    venv/bin/python -m pipeline.population zensus_de
    venv/bin/python -m pipeline.population ghs_eu [--source <tif path or /vsicurl/url>] [--bbox w,s,e,n]

Columns: cx, cy (cell centre in the grid's coordinate system, metres), geom, pop and — only for the census —
age / household / housing values. Missing values are NULL, so every query works with every dataset.
Data: Zensus 2022 © Statistische Ämter des Bundes und der Länder (dl-de/by-2.0); GHS-POP © European Union, JRC (CC BY 4.0).

The grid is built in pop_<id>__new and renamed to pop_<id> only when it is complete (indexed, analysed): until then the app
keeps using the old table (Update) or does not see the dataset at all, so it never runs on a half-built grid.
Speed: COPY (binary for the raster, raw CSV for the census) into an unlogged table, geom as a generated column (no UPDATE
that would rewrite the table and leave it half dead rows), the index built once at the end with enough memory.
"""
import os
import shutil
import signal
import sys
import zipfile

import psycopg

from backend import catalog
from backend.common import DATA, DB_POP, dsn, update_state
from .util import download, index_memory_mb, log, progress, recreate_db, stage, step, steps, watch_index

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
    """Unlogged while it is filled (no WAL per row); geom is computed by the database while copying."""
    conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.execute(f"""CREATE UNLOGGED TABLE {table} (cx int NOT NULL, cy int NOT NULL, pop float8 NOT NULL,
        {", ".join(c + " real" for c in STATS)},
        geom geometry(Point, {srid}) GENERATED ALWAYS AS (ST_SetSRID(ST_MakePoint(cx, cy), {srid})) STORED)""")


def import_zensus(conn, table: str):
    raw = DATA / "zensus"
    step("Download")
    stage(0, 20)
    for i, (zipname, _, col) in enumerate(ZENSUS_TOPICS):
        z = raw / f"{zipname}.zip"
        if not z.exists():
            download(f"{ZENSUS_BASE}/{zipname}.zip", z, f"{i + 1}/10 {col}", 100 * i / 10, 100 * (i + 1) / 10)

    step("Import")      # each CSV as it is into a staging table: no parsing in Python
    stage(20, 55)
    stg = {col: f"stg_{col}" for _, _, col in ZENSUS_TOPICS}
    for i, (zipname, csvname, col) in enumerate(ZENSUS_TOPICS):
        conn.execute(f"DROP TABLE IF EXISTS {stg[col]}")
        conn.execute(f"CREATE UNLOGGED TABLE {stg[col]} (id text, x int, y int, val text, flag text)")
        cols = "id, x, y, val" if col == "pop" else "id, x, y, val, flag"
        with zipfile.ZipFile(raw / f"{zipname}.zip") as zf, zf.open(csvname) as f, \
                conn.cursor().copy(f"COPY {stg[col]} ({cols}) FROM STDIN (FORMAT csv, DELIMITER ';', HEADER true)") as cp:
            size, done, n = zf.getinfo(csvname).file_size, 0, 0
            while chunk := f.read(8 << 20):
                cp.write(chunk)
                done += len(chunk)
                progress(100 * (i + done / size) / 10, f"{i + 1}/10 {col} · {done >> 20} / {size >> 20} MB")
        conn.execute(f"ANALYZE {stg[col]}")       # good row estimates: the merge picks fast joins
        conn.commit()

    step("Merge")       # one pass: the cells of the population file with the other values joined to them
    stage(55, 70)
    progress(0, "joining 10 tables")
    conn.execute("SET work_mem = '64MB'")
    num = "^[0-9]+(,[0-9]+)?$"      # '–' means "no value"; decimals use a comma
    joins = " ".join(f"LEFT JOIN {stg[c]} t{i} ON t{i}.x = p.x AND t{i}.y = p.y AND t{i}.val ~ '{num}'" for i, c in enumerate(STATS))
    vals = ", ".join(f"replace(t{i}.val, ',', '.')::real" for i, _ in enumerate(STATS))
    n = conn.execute(f"INSERT INTO {table} (cx, cy, pop, {', '.join(STATS)}) SELECT p.x, p.y, p.val::int, {vals} FROM {stg['pop']} p {joins}").rowcount
    for t in stg.values():
        conn.execute(f"DROP TABLE {t}")
    conn.commit()
    shutil.rmtree(raw, ignore_errors=True)         # the numbers are in the database now
    return n


def import_ghs(conn, table: str, bbox, source: str | None):
    import numpy as np
    import rasterio
    from rasterio.warp import transform_bounds
    from rasterio.windows import Window, from_bounds

    tif = DATA / "ghs" / "pop2020_1km.tif"
    step("Download")
    stage(0, 15)
    if source is None:
        if not tif.exists():
            download(catalog.GHS_URL, tif, "GHS-POP", 0, 100)
        source = str(tif)

    step("Import")
    stage(15, 60)
    # COPY in binary: one record per cell (3 fields: cx int4, cy int4, pop float8), built for a whole block with numpy
    rec = np.dtype([("n", ">i2"), ("l1", ">i4"), ("cx", ">i4"), ("l2", ">i4"), ("cy", ">i4"), ("l3", ">i4"), ("pop", ">f8")])
    with rasterio.open(source) as src, conn.cursor().copy(f"COPY {table} (cx, cy, pop) FROM STDIN (FORMAT BINARY)") as cp:
        cp.write(b"PGCOPY\n\xff\r\n\x00" + bytes(8))
        win = Window(0, 0, src.width, src.height)
        if bbox:
            x0, y0, x1, y1 = transform_bounds("EPSG:4326", src.crs, *bbox, densify_pts=21)
            win = from_bounds(x0, y0, x1, y1, src.transform).round_offsets().round_lengths().intersection(win)
        t, nodata, cells = src.transform, src.nodata, 0
        top, height, left = int(win.row_off), int(win.height), int(win.col_off)
        for r0 in range(top, top + height, 512):
            h = min(512, top + height - r0)
            blk = src.read(1, window=Window(left, r0, win.width, h))
            keep = (blk > 0.05) & (blk != nodata) if nodata is not None else blk > 0.05
            rows, cols = np.nonzero(keep)
            c, r = left + cols + .5, r0 + rows + .5
            out = np.empty(len(rows), rec)
            out["n"], out["l1"], out["l2"], out["l3"] = 3, 4, 4, 8
            out["cx"] = np.rint(t.a * c + t.b * r + t.c)
            out["cy"] = np.rint(t.d * c + t.e * r + t.f)
            out["pop"] = np.round(blk[rows, cols].astype("float64"), 1)
            cp.write(out.tobytes())
            cells += len(rows)
            progress(100 * (r0 + h - top) / height, f"{cells / 1e6:.1f} M cells")
        cp.write(b"\xff\xff")
    conn.commit()
    shutil.rmtree(tif.parent, ignore_errors=True)
    return cells


def finish(conn, new: str, table: str, has_stats: bool, n: int):
    """Make the table durable, index it, then put it in place of the old one in one go."""
    step("Index")
    stage(70 if has_stats else 60, 97)
    mb = conn.execute(f"SELECT pg_table_size('{new}') >> 20").fetchone()[0]
    progress(0, f"saving {mb:,} MB".replace(",", "."))
    conn.execute(f"ALTER TABLE {new} SET LOGGED")
    conn.commit()
    progress(40, f"index · {n / 1e6:.1f} M cells")
    # the index is built from sorted cells: when the sort fits in memory it is several times faster
    conn.execute(f"SET maintenance_work_mem = '{index_memory_mb()}MB'")
    watch_index(conn, f"CREATE INDEX {new}_geom_idx ON {new} USING gist (geom)", 40, 95, f"index · {n / 1e6:.1f} M cells")
    conn.commit()
    progress(95, "statistics")
    conn.execute(f"ANALYZE {new}")
    conn.commit()

    step("Finish")
    stage(97, 100)
    n, total = conn.execute(f"SELECT count(*), sum(pop) FROM {new}").fetchone()
    if not n:
        raise SystemExit("No cells imported")
    conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.execute(f"ALTER TABLE {new} RENAME TO {table}")
    if conn.execute("SELECT to_regclass(%s) IS NULL", (f"{table}_geom_idx",)).fetchone()[0]:
        conn.execute(f"ALTER INDEX {new}_geom_idx RENAME TO {table}_geom_idx")
    conn.commit()
    return n, total


class Cancelled(KeyboardInterrupt):
    pass


def _cancel(*_):
    raise Cancelled()


def main(ds: str, source: str | None = None, bbox=None):
    info = catalog.POPULATION[ds]
    table, new = f"pop_{ds}", f"pop_{ds}__new"
    zensus = ds == "zensus_de"
    steps(*(["Download", "Import", "Merge", "Index", "Finish"] if zensus else ["Download", "Import", "Index", "Finish"]))
    signal.signal(signal.SIGTERM, _cancel)      # Cancel in Settings: clean up the half-built table first
    try:
        psycopg.connect(dsn(DB_POP)).close()
    except psycopg.OperationalError:
        recreate_db(DB_POP)
    try:
        with psycopg.connect(dsn(DB_POP)) as conn:
            create_table(conn, new, info["srid"])
            conn.commit()
            try:
                cells = import_zensus(conn, new) if zensus else import_ghs(conn, new, bbox or info.get("bbox"), source)
                n, total = finish(conn, new, table, zensus, cells)
            except BaseException:
                conn.rollback()
                with psycopg.connect(dsn(DB_POP), autocommit=True) as c:
                    c.execute(f"DROP TABLE IF EXISTS {new}")
                    for _, _, col in ZENSUS_TOPICS:
                        c.execute(f"DROP TABLE IF EXISTS stg_{col}")
                raise
    except Cancelled:
        log("cancelled")                            # also marks the end time of the job (the log's mtime)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        os.kill(os.getpid(), signal.SIGTERM)       # end as "cancelled", like the other jobs
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
