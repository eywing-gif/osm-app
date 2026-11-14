# OSM Tag Search

Desktop app: search OpenStreetMap tags, point info, population (grid), public transport, land use (OSM, CORINE, built-up).

## Install
```
git clone https://github.com/eywing-gif/osm-app && cd osm-app
sh install.sh        # checks system, creates venv, adds menu entry
python3 app.py       # desktop window; or: venv/bin/uvicorn backend.main:app  → http://localhost:8000
```
Needs Python 3, PyGObject + WebKitGTK 4.1, PostgreSQL + PostGIS, osm2pgsql, git, and a PostgreSQL user that may create databases.
`install.sh` prints the exact commands for your system when something is missing. Arch:

```sh
sudo pacman -S --needed python python-gobject webkit2gtk-4.1 postgresql postgis osm2pgsql git
sudo -u postgres initdb -D /var/lib/postgres/data     # first time only
sudo systemctl enable --now postgresql
sudo -u postgres createuser -s $USER
```

Debian / Ubuntu: `sudo apt install python3-venv python3-gi gir1.2-webkit2-4.1 postgresql postgis osm2pgsql git`, then `sudo -u postgres createuser -s $USER`.
⚙ Settings → **App** shows what is missing.

## Update
⚙ Settings → App → **Check** → **Update** → **Restart**. Works for a git clone and for a zip download
(your `data/` and `venv/` stay).

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
- `backend/` FastAPI: `main` (search), `areas`, `features` (point info), `population` (grid tiles), `landuse` (OSM landuse), `transit`, `datasets` (settings API), `catalog` (what can be downloaded), `appinfo` (version, update), `common` (paths, databases, state)
- `pipeline/` imports, run as `venv/bin/python -m pipeline.<name>`: `osm`, `population`, `built`, `transit` (= `gtfs` → `snap` → `centres` → `lines`), `update`
- `frontend/` `index.html` + `app.css`; scripts in load order: `core` (map, sidebar), `layers` (overlays, legend), `search`, `library`, `inspect`, `landuse`, `transit`; `settings.js` / `settings.css`; `station.html` (station window)

Another dataset: add an entry to `backend/catalog.py` (and an importer in `pipeline/` if the format is new).
Tests / a second set of data: `OSMAPP_DATA=/other/dir OSMAPP_DB_PREFIX=t_ …` uses its own folder and databases.

Data: © OpenStreetMap contributors (ODbL) · Zensus 2022 © Statistische Ämter des Bundes und der Länder (dl-de/by-2.0) · GHS-POP © European Union, JRC · timetables: see Settings.

## License

GPL-3.0, see [LICENSE](LICENSE).
