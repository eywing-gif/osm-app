"""Import GHS-BUILT-S (built-up surface per cell, total and non-residential) into the database "population", table built_<id>.

    venv/bin/python -m pipeline.built ghs_built_eu [--bbox w,s,e,n]

Reads the GeoTIFFs of the JRC (R2023A, 2020, Mollweide) directly, only the window of the dataset. Cells without buildings are left out.
Data: GHS-BUILT-S © European Union, JRC (CC BY 4.0), Pesaresi & Politis 2023.
"""
import sys

import psycopg

from backend import catalog
from backend.common import DB_POP, dsn, update_state
from .util import log, progress, recreate_db


def main(ds: str, bbox=None):
    import numpy as np
    import rasterio
    from rasterio.warp import transform_bounds
    from rasterio.windows import Window, from_bounds

    info = catalog.BUILT[ds]
    table = f"built_{ds}"
    bbox = bbox or info.get("bbox")
    urls = [catalog.GHS_BUILT.format(dir=d, res=info["res"], name=n) for d, n in (("ghs-built-s", "GHS_BUILT_S"), ("ghs-built-s-nres", "GHS_BUILT_S_NRES"))]
    try:
        psycopg.connect(dsn(DB_POP)).close()
    except psycopg.OperationalError:
        recreate_db(DB_POP)
    with psycopg.connect(dsn(DB_POP)) as conn:
        conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.execute(f"CREATE TABLE {table} (cx int NOT NULL, cy int NOT NULL, built float8 NOT NULL, nres float8 NOT NULL)")
        with rasterio.open("/vsicurl/" + urls[0]) as total, rasterio.open("/vsicurl/" + urls[1]) as nres, \
                conn.cursor().copy(f"COPY {table} FROM STDIN") as cp:
            win = Window(0, 0, total.width, total.height)
            if bbox:
                x0, y0, x1, y1 = transform_bounds("EPSG:4326", total.crs, *bbox, densify_pts=21)
                win = from_bounds(x0, y0, x1, y1, total.transform).round_offsets().round_lengths().intersection(win)
            step, top, limit = 512, int(win.row_off), info["cell"] ** 2
            for r0 in range(top, top + int(win.height), step):
                h = min(step, top + int(win.height) - r0)
                progress(95 * (r0 - top) / win.height, "importing")
                w = Window(win.col_off, r0, win.width, h)
                a, b = total.read(1, window=w).astype("float64"), nres.read(1, window=w).astype("float64")
                rows, cols = np.nonzero((a > 0) & (a <= limit))          # nodata is a huge number: outside the limit
                for rr, cc in zip(rows, cols):
                    x, y = total.transform * (win.col_off + cc + .5, r0 + rr + .5)
                    cp.write_row((round(x), round(y), float(a[rr, cc]), float(min(b[rr, cc], a[rr, cc]))))
        progress(97, "indexing")
        conn.execute(f"ALTER TABLE {table} ADD COLUMN geom geometry(Point, {info['srid']})")
        conn.execute(f"UPDATE {table} SET geom = ST_SetSRID(ST_MakePoint(cx, cy), {info['srid']})")
        conn.execute(f"CREATE INDEX ON {table} USING gist (geom)")
        conn.execute(f"ANALYZE {table}")
        n, tot = conn.execute(f"SELECT count(*), sum(built) / 1e6 FROM {table}").fetchone()
        conn.commit()
    log(f"{n:,} cells, {tot:,.0f} km² built-up")

    def mark(st):
        b = st.setdefault("built", {})
        b.setdefault("active", ds)
    update_state(mark)


if __name__ == "__main__":
    a = sys.argv[1:]
    main(a[0], tuple(map(float, a[a.index("--bbox") + 1].split(","))) if "--bbox" in a else None)
