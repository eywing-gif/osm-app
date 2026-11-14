#!/bin/sh
# Install OSM Tag Search: checks the system, creates the Python environment, adds a menu entry.
#   sh install.sh
set -e
cd "$(dirname "$0")"
need() { command -v "$1" >/dev/null 2>&1; }

# the packages / first steps per distribution
if need pacman; then
  PKGS="sudo pacman -S --needed python python-gobject webkit2gtk-4.1 postgresql postgis osm2pgsql git"
  PGSTART="sudo -u postgres initdb -D /var/lib/postgres/data   # first time only
  sudo systemctl enable --now postgresql
  sudo -u postgres createuser -s $USER"
elif need dnf; then
  PKGS="sudo dnf install python3 python3-gobject webkit2gtk4.1 postgresql-server postgis osm2pgsql git"
  PGSTART="sudo postgresql-setup --initdb   # first time only
  sudo systemctl enable --now postgresql
  sudo -u postgres createuser -s $USER"
else
  PKGS="sudo apt install python3-venv python3-gi gir1.2-webkit2-4.1 postgresql postgis osm2pgsql git"
  PGSTART="sudo -u postgres createuser -s $USER"
fi

missing=""
for c in python3 psql createdb osm2pgsql; do need "$c" || missing="$missing $c"; done
if [ -n "$missing" ]; then
  echo "Missing:$missing"
  echo "Install:  $PKGS"
  echo "Then set up PostgreSQL (once):"; echo "  $PGSTART"
  exit 1
fi
python3 -c 'import gi; gi.require_version("WebKit2", "4.1")' 2>/dev/null ||
  echo "Note: the desktop window needs python-gobject + WebKitGTK 4.1 (see above); without it the app opens in the browser"

psql -d postgres -Atc "select 1" >/dev/null 2>&1 || {
  echo "PostgreSQL: no access for $USER. Start it and create your user:"; echo "  $PGSTART"; exit 1; }
[ "$(psql -d postgres -Atc "select count(*) from pg_available_extensions where name='postgis'")" -gt 0 ] ||
  { echo "PostGIS missing. Install:  $PKGS"; exit 1; }

python3 -m venv venv
venv/bin/pip install -q --upgrade pip
venv/bin/pip install -q -r requirements.txt
sh install-desktop.sh
echo
echo "Done. Start from the application menu, or:  python3 app.py"
echo "Then ⚙ Settings → download map, population, timetables."
