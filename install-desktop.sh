#!/bin/sh
# Adds "OSM Tag Search" to the application menu (re-run after moving the project folder).
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/.local/share/applications/osm-tag-search.desktop"
cat > "$DEST" <<EOF
[Desktop Entry]
Type=Application
Name=OSM Tag Search
Comment=Search OpenStreetMap tags on a map
Exec=/usr/bin/python3 "$DIR/app.py"
Icon=$DIR/icon.svg
Terminal=false
Categories=Education;Geography;
Keywords=OpenStreetMap;OSM;map;tags;
StartupWMClass=osm-tag-search
EOF
update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
echo "Installed $DEST"
