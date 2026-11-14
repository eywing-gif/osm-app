"""Tag library: browse every key and its values, sorted.

online – the OpenStreetMap taginfo database: all tags worldwide, with usage counts and wiki descriptions.
local  – the keys and values that actually occur in the local database (the imported regions).
"""
import json
import threading
import time
import urllib.error
from pathlib import Path
from typing import Literal

import psycopg
from fastapi import APIRouter, HTTPException, Query
from psycopg import sql

from .common import db, fetch_json
from .tagsql import TABLES, table_columns, tag_condition, tag_expr

router = APIRouter(prefix="/taglib")

TAGINFO = "https://taginfo.openstreetmap.org/api/4"
TAGINFO_PAGE = 999          # largest page taginfo allows
CACHE_SECONDS = 6 * 3600    # taginfo only updates once a day; be gentle with it
KEYS_CACHE = Path.home() / ".cache" / "osm-tag-search" / "local-keys.json"

Source = Literal["local", "online"]
Sort = Literal["name", "count"]


# ---------- taginfo (online) ----------

_cache: dict[tuple, tuple[float, dict]] = {}


def taginfo(path: str, **params) -> dict:
    key = (path, tuple(sorted(params.items())))
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
        return hit[1]
    try:
        data = fetch_json(f"{TAGINFO}/{path}", params, timeout=30)
    except urllib.error.HTTPError as e:
        raise HTTPException(502, f"taginfo answered with an error ({e.code}).")
    except (OSError, ValueError) as e:
        raise HTTPException(503, f"Could not reach taginfo.openstreetmap.org ({getattr(e, 'reason', e)}). "
                                 "Switch to the local source to browse the tags in your own data.")
    if len(_cache) > 400:
        _cache.clear()
    _cache[key] = (time.monotonic(), data)
    return data


_doc_keys: dict = {"at": 0.0, "rows": []}
_doc_lock = threading.Lock()


def documented_key_list() -> list[dict]:
    """Every key that has a wiki page and is actually used (a few thousand), cached for a few hours.
    Fetched completely so that sorting and filtering can be done here (taginfo sorts case-sensitively)."""
    with _doc_lock:
        if _doc_keys["rows"] and time.monotonic() - _doc_keys["at"] < CACHE_SECONDS:
            return _doc_keys["rows"]
        rows, page = [], 1
        while True:
            r = taginfo("keys/all", filter="in_wiki", rp=TAGINFO_PAGE, page=page, sortname="count_all", sortorder="desc")
            # templates like "addr:*" and wiki pages for keys nobody uses are not useful here
            rows += [{"name": d["key"], "count": d["count_all"], "values": d["values_all"]}
                     for d in r["data"] if d["count_all"] > 0 and "*" not in d["key"]]
            if not r["data"] or r["data"][-1]["count_all"] == 0 or page * TAGINFO_PAGE >= r["total"]:
                break   # sorted by use: the rest are unused keys
            page += 1
        _doc_keys.update(at=time.monotonic(), rows=rows)
        return rows


def online_keys(q: str, sort: Sort, documented: bool, page: int, per: int) -> dict:
    if documented:
        rows = [r for r in documented_key_list() if q.lower() in r["name"].lower()]
        rows.sort(key=lambda r: r["name"].lower() if sort == "name" else (-r["count"], r["name"].lower()))
        return {"items": rows[(page - 1) * per: page * per], "has_more": page * per < len(rows),
                "total": len(rows), "next_page": page + 1}
    # every key ever used (hundreds of thousands, most of them typos): let taginfo page through them
    params = dict(filter="all", rp=per, page=page, sortname="key" if sort == "name" else "count_all",
                  sortorder="asc" if sort == "name" else "desc")
    if q:
        params["query"] = q
    r = taginfo("keys/all", **params)
    return {"items": [{"name": d["key"], "count": d["count_all"], "values": d["values_all"]} for d in r["data"]],
            "has_more": page * per < r["total"], "total": r["total"], "next_page": page + 1}


def documented_values(key: str) -> list[dict]:
    """All values of a key that have a wiki page (a few hundred at most), with their descriptions."""
    out = []
    for page in range(1, 6):
        r = taginfo("key/values", key=key, filter="all", rp=TAGINFO_PAGE, page=page, lang="en",
                    sortname="in_wiki", sortorder="desc")
        rows = r["data"]
        out += [d for d in rows if d["in_wiki"]]
        if len(rows) < TAGINFO_PAGE or not all(d["in_wiki"] for d in rows):
            break   # sorted: once a value without wiki page shows up, there are no more documented ones
    return out


def value_item(d: dict) -> dict:
    return {"name": d["value"], "count": d["count"], "description": (d.get("description") or "").strip()}


def online_values(key: str, q: str, sort: Sort, documented: bool, page: int, per: int) -> dict:
    if documented:
        rows = [value_item(d) for d in documented_values(key)]
        if q:
            rows = [r for r in rows if q.lower() in r["name"].lower()]
        rows.sort(key=lambda r: r["name"].lower() if sort == "name" else (-r["count"], r["name"].lower()))
        chunk = rows[(page - 1) * per: page * per]
        return {"items": chunk, "has_more": page * per < len(rows), "total": len(rows), "next_page": page + 1}
    params = dict(key=key, filter="all", rp=per, page=page, lang="en",
                  sortname="value" if sort == "name" else "count",
                  sortorder="asc" if sort == "name" else "desc")
    if q:
        params["query"] = q
    r = taginfo("key/values", **params)
    return {"items": [value_item(d) for d in r["data"]], "has_more": page * per < r["total"], "total": r["total"],
            "next_page": page + 1}


# ---------- local database ----------

_local_keys: list[dict] | None = None
_local_lock = threading.Lock()


def db_version() -> str:
    """Changes whenever the database is replaced by a new import."""
    with db() as conn:
        return conn.execute("SELECT (SELECT oid FROM pg_database WHERE datname = current_database())::text"
                            " || ':' || 'planet_osm_point'::regclass::oid::text").fetchone()[0]


def count_local_keys() -> list[dict]:
    totals: dict[str, int] = {}
    with db() as conn:
        for table in TABLES:
            cols = sorted(table_columns(table))   # keys that osm2pgsql stores as real columns
            row = conn.execute(sql.SQL("SELECT {} FROM {}").format(
                sql.SQL(", ").join(sql.SQL("count({})").format(sql.Identifier(c)) for c in cols),
                sql.Identifier(table))).fetchone()
            for c, n in zip(cols, row):
                if n:
                    totals[c] = totals.get(c, 0) + n
            # all other keys live in the hstore column
            for k, n in conn.execute(sql.SQL("SELECT k, count(*) FROM (SELECT skeys(tags) AS k FROM {}) s GROUP BY k")
                                     .format(sql.Identifier(table))):
                totals[k] = totals.get(k, 0) + n
    return [{"name": k, "count": n} for k, n in totals.items()]


def local_key_list() -> list[dict]:
    global _local_keys
    with _local_lock:
        if _local_keys is not None:
            return _local_keys
        version = db_version()
        try:
            cached = json.loads(KEYS_CACHE.read_text())
            if cached["version"] == version:
                _local_keys = cached["keys"]
                return _local_keys
        except (OSError, ValueError, KeyError):
            pass
        _local_keys = count_local_keys()
        KEYS_CACHE.parent.mkdir(parents=True, exist_ok=True)
        KEYS_CACHE.write_text(json.dumps({"version": version, "keys": _local_keys}))
        return _local_keys


def local_keys(q: str, sort: Sort, page: int, per: int) -> dict:
    rows = [r for r in local_key_list() if q.lower() in r["name"].lower()]
    rows.sort(key=lambda r: r["name"].lower() if sort == "name" else (-r["count"], r["name"].lower()))
    return {"items": rows[(page - 1) * per: page * per], "has_more": page * per < len(rows), "total": len(rows),
            "next_page": page + 1}


def local_values(key: str, q: str, sort: Sort, page: int, per: int) -> dict:
    selects = [sql.SQL("SELECT {e} AS v FROM {t} t WHERE {cond}").format(
        e=tag_expr(t, key), t=sql.Identifier(t), cond=tag_condition(t, key, None)) for t in TABLES]
    query = sql.SQL("SELECT v, count(*) AS c FROM ({}) s WHERE v ILIKE {} GROUP BY v ORDER BY {} LIMIT {} OFFSET {}").format(
        sql.SQL(" UNION ALL ").join(selects), sql.Literal(f"%{q}%"),
        sql.SQL("v") if sort == "name" else sql.SQL("c DESC, v"),
        sql.Literal(per + 1), sql.Literal((page - 1) * per))
    try:
        with db() as conn:
            rows = conn.execute(query).fetchall()
    except psycopg.Error as e:
        raise HTTPException(400, f"Query failed: {e.diag.message_primary or e}")
    return {"items": [{"name": v, "count": c, "description": ""} for v, c in rows[:per]], "has_more": len(rows) > per,
            "next_page": page + 1}


# ---------- endpoints ----------

@router.get("/keys")
def keys(source: Source = "online", q: str = "", sort: Sort = "name", documented: bool = True,
         page: int = Query(1, ge=1), per: int = Query(100, ge=1, le=200)):
    """All keys, sorted by name or by how often they are used."""
    q = q.strip()
    result = local_keys(q, sort, page, per) if source == "local" else online_keys(q, sort, documented, page, per)
    return result | {"source": source}


@router.get("/values")
def values(key: str = Query(..., min_length=1), source: Source = "online", q: str = "", sort: Sort = "name",
           documented: bool = True, page: int = Query(1, ge=1), per: int = Query(100, ge=1, le=200)):
    """All values of one key, sorted by name or by how often they are used."""
    q = q.strip()
    result = (local_values(key, q, sort, page, per) if source == "local"
              else online_values(key, q, sort, documented, page, per))
    return result | {"source": source, "key": key}


def warm():
    """Prepare the key lists in the background so the first click is quick (offline: just skip taginfo)."""
    for job in (local_key_list, documented_key_list):
        try:
            job()
        except Exception:
            pass
