# Spatial Analytics

Desktop app: search OpenStreetMap tags, point info, population (grid), roads (lane-km), amenities (rating of a place), public transport (lines, reach, supply), land use (OSM, CORINE, built-up).

## Install

Linux only (Ubuntu / Debian, Arch / Manjaro, Fedora, openSUSE). Open a terminal and paste these lines one after the other:

```sh
git clone https://github.com/eywing-gif/osm-app.git ~/osm-app
```
```sh
sh ~/osm-app/install.sh
```

No `git`? Download the [zip](https://github.com/eywing-gif/osm-app/archive/refs/heads/main.zip), unpack it,
open a terminal in the unpacked folder and run `sh install.sh`.

The script asks for your password once and then does everything itself: installs the system packages
(PostgreSQL + PostGIS, osm2pgsql, WebKitGTK, Python), starts the database server, creates your database user,
the Python environment (`venv/`) and a menu entry. If something goes wrong it says what – fix it and simply
run `sh install.sh` again (also the cure after a big system update).

**Start:** application menu → *Spatial Analytics* (or `python3 ~/osm-app/app.py`).
**First start:** ⚙ → download a map region, population grid, timetables.

## Uninstall

```sh
sh ~/osm-app/uninstall.sh
```

Removes the menu entry, the app's databases, the downloads and – if you say yes – the app folder.
System packages (PostgreSQL, …) stay; the script prints the command to remove them too.

## Update
⚙ Settings → App → **Check** → **Update** → **Restart**. Works for a git clone and for a zip download
(your `data/` and `venv/` stay). Data: ⚙ → each dataset has its own **Update**.

## Release (maintainer)
1. Raise `version` in `app.json`, commit, push.
2. `git tag v0.6.0 && git push --tags` — the newest `vX.Y.Z` tag is what the app offers.
3. Set `repo` in `app.json` to `<owner>/<repo>` once.

## Data (⚙ Settings in the app)
| Tab | What | Stored in |
|---|---|---|
| Map | Geofabrik regions (world → states), several at once | db `osm` |
| Population | Zensus 2022 (DE, 100 m), GHS-POP (Europe / world, 1 km) | db `population`, table `pop_<id>` |
| Built-up | GHS-BUILT-S 2020 (DE 100 m, Europe / world 1 km) | db `population`, table `built_<id>` |
| Transit | GTFS feeds | db `transit_<id>` |

Downloads and the import state live in `data/` (`state.json`). One download job runs at a time.

## Layout
- `backend/` FastAPI: `main` (search), `areas`, `features` (point info), `population` (grid tiles), `landuse` (OSM landuse), `roads` (lane-km), `amenities` + `amenity_catalog` (places, scores), `supply` (transit departures by area), `transit`, `reach` (isochrones), `datasets` (settings API), `catalog` (what can be downloaded), `appinfo` (version, update), `common` (paths, databases, state)
- `pipeline/` imports, run as `venv/bin/python -m pipeline.<name>`: `osm` (+ `roads`, `amenities`; `prepare` runs both for an older import), `population`, `built`, `transit` (= `gtfs` → `snap` → `centres` → `lines`), `update`
- `frontend/` `index.html` + `app.css`; scripts in load order: `core` (map, sidebar), `layers` (overlays, legend), `search`, `library`, `inspect`, `landuse`, `roads`, `split` (grid / area views), `amenities`, `transit`; `settings.js` / `settings.css`; `station.html` (station window)

Another dataset: add an entry to `backend/catalog.py` (and an importer in `pipeline/` if the format is new).
Tests / a second set of data: `OSMAPP_DATA=/other/dir OSMAPP_DB_PREFIX=t_ …` uses its own folder and databases.

Data: © OpenStreetMap contributors (ODbL) · Zensus 2022 © Statistische Ämter des Bundes und der Länder (dl-de/by-2.0) · GHS-POP © European Union, JRC · timetables: see Settings.

## License

GPL-3.0, see [LICENSE](LICENSE).
