#!/bin/sh
# Removes OSM Tag Search. In a terminal, in the app folder:   sh uninstall.sh
# Deletes: the menu entry, the app's databases (map, population, timetables), the downloads (data/),
# the Python environment (venv/) and – if you want – the app folder itself.
# Keeps: the system packages (PostgreSQL, …), since other programs may use them (see the end).
#   Options: -y (no questions)
cd "$(dirname "$0")" || exit 1
APP_DIR=$(pwd)
PREFIX=${OSMAPP_DB_PREFIX:-}
DATA=${OSMAPP_DATA:-$APP_DIR/data}
YES=0
[ "$1" = "-y" ] || [ "$1" = "--yes" ] && YES=1
ask() {        # ask "question" -> 0 for yes; default no
  [ "$YES" = 1 ] && return 0
  printf '%s [y/N] ' "$1"
  read -r answer </dev/tty 2>/dev/null || answer=n
  case $answer in [yYjJ]*) return 0 ;; *) return 1 ;; esac
}
ok() { printf '  \033[32m✓\033[0m %s\n' "$*"; }

[ "$(id -u)" = 0 ] && { echo "Please run it as your normal user, without sudo:   sh uninstall.sh"; exit 1; }
echo "This removes OSM Tag Search: menu entry, databases, downloads ($DATA) and venv/."
ask "Continue?" || { echo "Nothing removed."; exit 0; }

rm -f "$HOME/.local/share/applications/osm-tag-search.desktop"
update-desktop-database "$HOME/.local/share/applications" 2>/dev/null
ok "menu entry"

if command -v psql >/dev/null 2>&1 && psql -d postgres -Atqc "select 1" >/dev/null 2>&1; then
  dbs=$(psql -d postgres -Atqc "select datname from pg_database where datname in ('${PREFIX}osm', '${PREFIX}osm_import', '${PREFIX}osm_old', '${PREFIX}population') or datname like '${PREFIX}transit\_%'")
  for db in $dbs; do
    psql -d postgres -qc "DROP DATABASE IF EXISTS \"$db\" WITH (FORCE)" && ok "database $db"
  done
  [ -z "$dbs" ] && ok "no databases"
else
  echo "  ! database server not reachable: databases not removed (osm, population, transit_*)"
fi

rm -rf "$DATA" "$APP_DIR/venv" "${XDG_CACHE_HOME:-$HOME/.cache}/osm-tag-search"
ok "downloads, venv, cache"

if ask "Also delete the app folder $APP_DIR?"; then
  cd / && rm -rf "$APP_DIR" && ok "app folder"
fi
echo
echo "Done. The system packages stay installed. To remove them as well (only if nothing else needs them):"
if command -v pacman >/dev/null 2>&1; then echo "  sudo pacman -Rs postgis osm2pgsql webkit2gtk-4.1 postgresql"
elif command -v apt-get >/dev/null 2>&1; then echo "  sudo apt remove postgis osm2pgsql gir1.2-webkit2-4.1 postgresql"
elif command -v dnf >/dev/null 2>&1; then echo "  sudo dnf remove postgis osm2pgsql webkit2gtk4.1 postgresql-server"
fi
echo "Your database user stays too:  sudo -u postgres dropuser $USER"
