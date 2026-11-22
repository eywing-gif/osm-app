#!/bin/sh
# Installs Spatial Analytics. In a terminal, in the app folder:   sh install.sh
# It installs the system packages (asks for your password once), sets up the PostgreSQL database server,
# creates the Python environment (venv/) and adds "Spatial Analytics" to the application menu.
# Safe to run again at any time (e.g. after a system update): it only does what is missing.
#   Options: -y (no questions)   --no-menu (no menu entry)
set -e
cd "$(dirname "$0")"
APP_DIR=$(pwd)

bold() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
ok() { printf '    \033[32m✓\033[0m %s\n' "$*"; }
fail() { printf '\n\033[31mError:\033[0m %s\n\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1; }
YES=0; MENU=1
for a in "$@"; do
  case $a in -y|--yes) YES=1 ;; --no-menu) MENU=0 ;; *) fail "Unknown option: $a" ;; esac
done
ask() {        # ask "question" -> 0 for yes (default)
  [ "$YES" = 1 ] && return 0
  printf '%s [Y/n] ' "$1"
  read -r answer </dev/tty 2>/dev/null || answer=y
  case $answer in [nN]*) return 1 ;; *) return 0 ;; esac
}

[ "$(id -u)" = 0 ] && fail "Please run it as your normal user, without sudo:   sh install.sh"
[ "$(uname)" = Linux ] || fail "This app runs on Linux only."

# ---------- 1. + 2. system packages, database server, database user (needs your password: setup-system.sh) ----------
bold "1/5 System packages, 2/5 database server"
missing=""
for c in python3 psql osm2pgsql git; do need "$c" || missing="$missing $c"; done
python3 -c 'import venv, ensurepip' 2>/dev/null || missing="$missing python-venv"
python3 -c 'import gi; gi.require_version("WebKit2", "4.1")' 2>/dev/null || missing="$missing webkitgtk"
psql -d postgres -Atqc "select 1" >/dev/null 2>&1 || missing="$missing database"
if [ -n "$missing" ]; then
  echo "    Missing:$missing"
  ask "Set it up now? (asks for your password)" || fail "Cancelled. Run this again when you are ready."
  sudo sh "$APP_DIR/setup-system.sh" "$USER" || fail "The system setup failed (see above)."
fi
ok "packages"
psql -d postgres -Atqc "select 1" >/dev/null 2>&1 || fail "No access to the database as $USER (check pg_hba.conf: local connections should use 'peer' or 'trust')."
ok "database server, user $USER"
[ "$(psql -d postgres -Atqc "select count(*) from pg_available_extensions where name = 'postgis'")" -gt 0 ] ||
  fail "PostGIS is missing for this PostgreSQL version. Install the PostGIS package that matches 'psql --version'."
ok "PostGIS"

# ---------- 3. Python environment ----------
bold "3/5 Python environment (venv/, takes a minute)"
if [ -x venv/bin/python ] && ! venv/bin/python -c "import sys" >/dev/null 2>&1; then
  echo "    The old environment no longer works (Python was updated): making a new one."
  rm -rf venv
fi
[ -x venv/bin/python ] || python3 -m venv venv || fail "Could not create venv/ (package python3-venv missing?)."
venv/bin/python -m pip install --quiet --upgrade pip || fail "pip does not work – is the internet connection OK?"
venv/bin/python -m pip install --quiet -r requirements.txt || fail "Installing the Python packages failed (see above)."
venv/bin/python -c "import backend.main" || fail "The app does not load (see above)."
ok "venv"

# ---------- 4. menu entry ----------
bold "4/5 Application menu"
if [ "$MENU" = 1 ]; then sh install-desktop.sh >/dev/null && ok "Spatial Analytics"; else echo "    skipped"; fi

# ---------- 5. check ----------
bold "5/5 Check"
if python3 -c 'import gi; gi.require_version("WebKit2", "4.1")' 2>/dev/null; then
  ok "desktop window"
else
  echo "    ! WebKitGTK is missing: the app will open in your web browser instead of its own window."
fi
printf '\n\033[32mDone.\033[0m Start "Spatial Analytics" from the application menu, or:  python3 %s/app.py\n' "$APP_DIR"
echo "First start: click ⚙ (Settings) and download the data you need (map, population, timetables)."
echo "Remove it again:  sh $APP_DIR/uninstall.sh"
