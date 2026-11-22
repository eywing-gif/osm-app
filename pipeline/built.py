"""Import GHS-BUILT-S (built-up surface per cell, total and non-residential) into the database "population", table built_<id>.

    venv/bin/python -m pipeline.built ghs_built_eu [--bbox w,s,e,n]

Reads the GeoTIFFs of the JRC (R2023A, 2020, Mollweide) directly, only the window of the dataset. Cells without buildings are left out.
Data: GHS-BUILT-S © European Union, JRC (CC BY 4.0), Pesaresi & Politis 2023.

Like pipeline.population: built in built_<id>__new (unlogged, binary COPY, geom as a generated column), made durable,
indexed and analysed, then renamed to built_<id> — the app never sees a half-built grid.
"""
import os
import signal
import sys

import psycopg

from backend import catalog
from backend.common import DB_POP, dsn, update_state
from .util import index_memory_mb, log, progress, recreate_db, stage, step, steps, watch_index


def create_table(conn, table: str, srid: int):
    conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.execute(f"""CREATE UNLOGGED TABLE {table} (cx int NOT NULL, cy int NOT NULL, built float8 NOT NULL, nres float8 NOT NULL,
        geom geometry(Point, {srid}) GENERATED ALWAYS AS (ST_SetSRID(ST_MakePoint(cx, cy), {srid})) STORED)""")


def import_cells(conn, table: str, info: dict, bbox) -> int:
    import numpy as np
    import rasterio
    from rasterio.warp import transform_bounds
    from rasterio.windows import Window, from_bounds

    step("Import")
    stage(0, 60)
    urls = [catalog.GHS_BUILT.format(dir=d, res=info["res"], name=n) for d, n in (("ghs-built-s", "GHS_BUILT_S"), ("ghs-built-s-nres", "GHS_BUILT_S_NRES"))]
    # COPY in binary: one record per cell (4 fields: cx int4, cy int4, built float8, nres float8), built per block with numpy
    rec = np.dtype([("n", ">i2"), ("l1", ">i4"), ("cx", ">i4"), ("l2", ">i4"), ("cy", ">i4"),
                    ("l3", ">i4"), ("built", ">f8"), ("l4", ">i4"), ("nres", ">f8")])
    with rasterio.open("/vsicurl/" + urls[0]) as total, rasterio.open("/vsicurl/" + urls[1]) as nres, \
            conn.cursor().copy(f"COPY {table} (cx, cy, built, nres) FROM STDIN (FORMAT BINARY)") as cp:
        cp.write(b"PGCOPY\n\xff\r\n\x00" + bytes(8))
        win = Window(0, 0, total.width, total.height)
        if bbox:
            x0, y0, x1, y1 = transform_bounds("EPSG:4326", total.crs, *bbox, densify_pts=21)
            win = from_bounds(x0, y0, x1, y1, total.transform).round_offsets().round_lengths().intersection(win)
        t, limit, cells = total.transform, info["cell"] ** 2, 0
        top, height, left = int(win.row_off), int(win.height), int(win.col_off)
        for r0 in range(top, top + height, 512):
            h = min(512, top + height - r0)
            w = Window(left, r0, win.width, h)
            a, b = total.read(1, window=w).astype("float64"), nres.read(1, window=w).astype("float64")
            rows, cols = np.nonzero((a > 0) & (a <= limit))          # nodata is a huge number: outside the limit
            c, r = left + cols + .5, r0 + rows + .5
            out = np.empty(len(rows), rec)
            out["n"], out["l1"], out["l2"], out["l3"], out["l4"] = 4, 4, 4, 8, 8
            out["cx"] = np.rint(c * t.a + r * t.b + t.c)
            out["cy"] = np.rint(c * t.d + r * t.e + t.f)
            av = a[rows, cols]
            out["built"], out["nres"] = av, np.minimum(b[rows, cols], av)
            cp.write(out.tobytes())
            cells += len(rows)
            progress(100 * (r0 + h - top) / height, f"{cells / 1e6:.1f} M cells")
        cp.write(b"\xff\xff")
    conn.commit()
    return cells


def finish(conn, new: str, table: str, n: int):
    """Make the table durable, index it, then put it in place of the old one in one go."""
    step("Index")
    stage(60, 97)
    mb = conn.execute(f"SELECT pg_table_size('{new}') >> 20").fetchone()[0]
    progress(0, f"saving {mb:,} MB".replace(",", "."))
    conn.execute(f"ALTER TABLE {new} SET LOGGED")
    conn.commit()
    progress(40, f"index · {n / 1e6:.1f} M cells")
    conn.execute(f"SET maintenance_work_mem = '{index_memory_mb()}MB'")
    watch_index(conn, f"CREATE INDEX {new}_geom_idx ON {new} USING gist (geom)", 40, 95, f"index · {n / 1e6:.1f} M cells")
    conn.commit()
    progress(95, "statistics")
    conn.execute(f"ANALYZE {new}")
    conn.commit()

    step("Finish")
    stage(97, 100)
    n, tot = conn.execute(f"SELECT count(*), sum(built) / 1e6 FROM {new}").fetchone()
    if not n:
        raise SystemExit("No cells imported")
    conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.execute(f"ALTER TABLE {new} RENAME TO {table}")
    if conn.execute("SELECT to_regclass(%s) IS NULL", (f"{table}_geom_idx",)).fetchone()[0]:
        conn.execute(f"ALTER INDEX {new}_geom_idx RENAME TO {table}_geom_idx")
    conn.commit()
    return n, tot


class Cancelled(KeyboardInterrupt):
    pass


def _cancel(*_):
    raise Cancelled()


def main(ds: str, bbox=None):
    info = catalog.BUILT[ds]
    table, new = f"built_{ds}", f"built_{ds}__new"
    steps("Import", "Index", "Finish")
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
                cells = import_cells(conn, new, info, bbox or info.get("bbox"))
                n, tot = finish(conn, new, table, cells)
            except BaseException:
                conn.rollback()
                with psycopg.connect(dsn(DB_POP), autocommit=True) as c:
                    c.execute(f"DROP TABLE IF EXISTS {new}")
                raise
    except Cancelled:
        log("cancelled")                            # also marks the end time of the job (the log's mtime)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        os.kill(os.getpid(), signal.SIGTERM)       # end as "cancelled", like the other jobs
    log(f"{n:,} cells, {tot:,.0f} km² built-up")

    def mark(st):
        b = st.setdefault("built", {})
        b.setdefault("active", ds)
    update_state(mark)


if __name__ == "__main__":
    a = sys.argv[1:]
    main(a[0], tuple(map(float, a[a.index("--bbox") + 1].split(","))) if "--bbox" in a else None)
