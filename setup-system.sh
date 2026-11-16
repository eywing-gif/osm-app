#!/bin/sh
# The part of the setup that needs administrator rights: system packages, the database server and a database user.
# Run by install.sh (with sudo) and by the app's "Repair" button (with pkexec: a password window).
#   sudo sh setup-system.sh <user name> [--job]      (--job: progress lines for the app)
# Safe to run again: it only does what is missing.
set -e
USER_NAME=${1:?usage: sh setup-system.sh <user name>}
[ "$(id -u)" = 0 ] || { echo "Error: needs administrator rights (sudo / pkexec)."; exit 1; }
export DEBIAN_FRONTEND=noninteractive LC_ALL=C.UTF-8
need() { command -v "$1" >/dev/null 2>&1; }
as_postgres() { (cd / && runuser -u postgres -- "$@"); }
step() { [ "$JOB" = 1 ] && echo "@progress $1 $2"; echo "== $2"; }
JOB=0; [ "${2:-}" = "--job" ] && JOB=1

# ---------- system packages ----------
if need pacman; then
  DISTRO=arch; INSTALL="pacman -S --needed --noconfirm python python-gobject webkit2gtk-4.1 postgresql postgis osm2pgsql git"
elif need apt-get; then
  DISTRO=debian; INSTALL="apt-get update && apt-get install -y python3 python3-venv python3-gi gir1.2-webkit2-4.1 postgresql postgresql-contrib postgis osm2pgsql git"
elif need dnf; then
  DISTRO=fedora; INSTALL="dnf install -y python3 python3-gobject webkit2gtk4.1 postgresql-server postgresql-contrib postgis osm2pgsql git"
elif need zypper; then
  DISTRO=suse; INSTALL="zypper --non-interactive install python3 python3-gobject python3-gobject-Gdk typelib-1_0-WebKit2-4_1 postgresql-server postgresql-contrib postgis osm2pgsql git"
else
  echo "Error: unknown Linux distribution – install PostgreSQL, PostGIS, osm2pgsql, git, PyGObject and WebKitGTK 4.1 by hand."
  exit 1
fi
missing=""
for c in python3 psql osm2pgsql git; do need "$c" || missing="$missing $c"; done
python3 -c 'import venv, ensurepip' 2>/dev/null || missing="$missing python-venv"
python3 -c 'import gi; gi.require_version("WebKit2", "4.1")' 2>/dev/null || missing="$missing webkitgtk"
if [ -n "$missing" ]; then
  step 10 "installing packages:$missing"
  sh -c "$INSTALL"
fi

# ---------- database server ----------
step 60 "database server"
case $DISTRO in        # a new server needs its data folder once (Debian / Ubuntu do this themselves)
  arch) [ -f /var/lib/postgres/data/PG_VERSION ] || as_postgres initdb --locale=C.UTF-8 --encoding=UTF8 -D /var/lib/postgres/data ;;
  fedora) [ -f /var/lib/pgsql/data/PG_VERSION ] || postgresql-setup --initdb ;;
esac
if need systemctl; then
  systemctl enable postgresql >/dev/null 2>&1 || true          # start with the computer
  systemctl is-active --quiet postgresql || systemctl start postgresql
fi
i=0
until as_postgres psql -Atqc "select 1" >/dev/null 2>&1; do
  i=$((i + 1)); [ $i -gt 30 ] && { echo "Error: the database server does not start (systemctl status postgresql)."; exit 1; }
  sleep 1
done

# ---------- database user (the login name, allowed to create databases) ----------
step 90 "database user $USER_NAME"
if [ "$(as_postgres psql -Atqc "select count(*) from pg_roles where rolname = '$USER_NAME'")" = 0 ]; then
  as_postgres createuser --superuser "$USER_NAME"
fi
[ "$(as_postgres psql -Atqc "select count(*) from pg_available_extensions where name = 'postgis'")" -gt 0 ] ||
  { echo "Error: PostGIS is missing for this PostgreSQL version."; exit 1; }
step 100 "ready"
