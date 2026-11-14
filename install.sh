#!/bin/sh
# Installs OSM Tag Search. In a terminal, in the app folder:   sh install.sh
# It installs the system packages (asks for your password once), sets up the PostgreSQL database server,
# creates the Python environment (venv/) and adds "OSM Tag Search" to the application menu.
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
as_postgres() { (cd / && sudo -u postgres "$@"); }

[ "$(id -u)" = 0 ] && fail "Please run it as your normal user, without sudo:   sh install.sh"
[ "$(uname)" = Linux ] || fail "This app runs on Linux only."

# ---------- 1. system packages ----------
bold "1/5 System packages"
if need pacman; then
  DISTRO=arch; PKGS="python python-gobject webkit2gtk-4.1 postgresql postgis osm2pgsql git"
  INSTALL="sudo pacman -S --needed --noconfirm $PKGS"
elif need apt-get; then
  DISTRO=debian; PKGS="python3 python3-venv python3-gi gir1.2-webkit2-4.1 postgresql postgresql-contrib postgis osm2pgsql git"
  INSTALL="sudo apt-get update && sudo apt-get install -y $PKGS"
elif need dnf; then
  DISTRO=fedora; PKGS="python3 python3-gobject webkit2gtk4.1 postgresql-server postgresql-contrib postgis osm2pgsql git"
  INSTALL="sudo dnf install -y $PKGS"
elif need zypper; then
  DISTRO=suse; PKGS="python3 python3-gobject python3-gobject-Gdk typelib-1_0-WebKit2-4_1 postgresql-server postgresql-contrib postgis osm2pgsql git"
  INSTALL="sudo zypper install -y $PKGS"
else
  fail "Unknown Linux distribution. Install by hand: Python 3 (with venv), PyGObject, WebKitGTK 4.1, PostgreSQL, PostGIS, osm2pgsql, git – then run this again."
fi
missing=""
for c in python3 psql osm2pgsql git; do need "$c" || missing="$missing $c"; done
python3 -c 'import venv, ensurepip' 2>/dev/null || missing="$missing python-venv"
python3 -c 'import gi; gi.require_version("WebKit2", "4.1")' 2>/dev/null || missing="$missing webkitgtk"
if [ -n "$missing" ]; then
  echo "    Missing:$missing"
  echo "    Will run:  $INSTALL"
  ask "Install them now? (asks for your password)" || fail "Cancelled. Install the packages above, then run this again."
  sh -c "$INSTALL" || fail "Installing the packages failed (see above)."
fi
ok "packages"

# ---------- 2. database server ----------
bold "2/5 Database server (PostgreSQL)"
if ! psql -d postgres -Atqc "select 1" >/dev/null 2>&1; then
  case $DISTRO in        # a new server needs its data folder once (Debian / Ubuntu do this themselves)
    arch) sudo test -f /var/lib/postgres/data/PG_VERSION ||
            as_postgres initdb --locale=C.UTF-8 --encoding=UTF8 -D /var/lib/postgres/data ;;
    fedora) sudo test -f /var/lib/pgsql/data/PG_VERSION || sudo postgresql-setup --initdb ;;
  esac
  if need systemctl; then
    systemctl is-active --quiet postgresql 2>/dev/null || sudo systemctl enable --now postgresql
  fi
  i=0
  until as_postgres psql -Atqc "select 1" >/dev/null 2>&1; do
    i=$((i + 1)); [ $i -gt 30 ] && fail "The database server does not start. Try:  sudo systemctl status postgresql"
    sleep 1
  done
  # a database user with your login name, allowed to create databases
  psql -d postgres -Atqc "select 1" >/dev/null 2>&1 || as_postgres createuser --superuser "$USER" || fail "Could not create the database user $USER."
fi
psql -d postgres -Atqc "select 1" >/dev/null 2>&1 || fail "No access to the database as $USER (check pg_hba.conf: local connections should use 'peer' or 'trust')."
ok "running, user $USER"
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
if [ "$MENU" = 1 ]; then sh install-desktop.sh >/dev/null && ok "OSM Tag Search"; else echo "    skipped"; fi

# ---------- 5. check ----------
bold "5/5 Check"
if python3 -c 'import gi; gi.require_version("WebKit2", "4.1")' 2>/dev/null; then
  ok "desktop window"
else
  echo "    ! WebKitGTK is missing: the app will open in your web browser instead of its own window."
fi
printf '\n\033[32mDone.\033[0m Start "OSM Tag Search" from the application menu, or:  python3 %s/app.py\n' "$APP_DIR"
echo "First start: click ⚙ (Settings) and download the data you need (map, population, timetables)."
echo "Remove it again:  sh $APP_DIR/uninstall.sh"
