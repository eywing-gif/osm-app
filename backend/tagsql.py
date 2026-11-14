"""SQL building blocks for the osm2pgsql tables (shared by the search and the tag library)."""
from functools import lru_cache

from psycopg import sql

from .common import db

# Tables we search. Polygons (buildings, parks, ...) are shown at a point on their surface.
TABLES = ["planet_osm_point", "planet_osm_polygon"]


@lru_cache
def table_columns(table: str) -> frozenset[str]:
    """osm2pgsql stores some keys as real columns, the rest in the hstore 'tags'."""
    with db() as conn:
        rows = conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = %s", (table,)
        ).fetchall()
    return frozenset(r[0] for r in rows) - {"osm_id", "way", "tags", "z_order", "way_area"}


def tag_expr(table: str, key: str) -> sql.Composable:
    """SQL expression giving the value of `key` in `table` (column or hstore)."""
    if key in table_columns(table):
        return sql.SQL("t.{}").format(sql.Identifier(key))
    # parentheses matter: '||' binds tighter than the hstore '->' operator
    return sql.SQL("(t.tags -> {})").format(sql.Literal(key))


def tag_condition(table: str, key: str, value: str | None) -> sql.Composable:
    """key=value (or key exists, if value is None)."""
    if key in table_columns(table):
        col = tag_expr(table, key)
        return sql.SQL("{} IS NOT NULL").format(col) if value is None \
            else sql.SQL("{} = {}").format(col, sql.Literal(value))
    # hstore operators ? and @> can use the GIN index on tags (fast for big regions)
    if value is None:
        return sql.SQL("t.tags ? {}").format(sql.Literal(key))
    return sql.SQL("t.tags @> hstore({}, {})").format(sql.Literal(key), sql.Literal(value))
