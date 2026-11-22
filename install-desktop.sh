#!/bin/sh
# Adds "Spatial Analytics" to the application menu (re-run after moving the project folder).
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
APPS="$HOME/.local/share/applications"
DEST="$APPS/spatial-analytics.desktop"
rm -f "$APPS/osm-tag-search.desktop"          # the entry under the old name
mkdir -p "$APPS"
cat > "$DEST" <<EOT
[Desktop Entry]
Type=Application
Name=Spatial Analytics
Comment=Analyse places on a map: population, roads, amenities, transit
Exec=/usr/bin/python3 "$DIR/app.py"
Icon=$DIR/frontend/icon.svg
Terminal=false
Categories=Education;Geography;
Keywords=OpenStreetMap;OSM;map;GIS;analytics;transit;population;
StartupWMClass=spatial-analytics
EOT
update-desktop-database "$APPS" 2>/dev/null || true
echo "Installed $DEST"
