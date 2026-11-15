/* Roads: lane-km of an area (the chosen OSM area, a drawn shape or the map view), by type; ratios per km² / per person.
   The type choice (roadSel, layers.js) is shared with the "Road lanes (1 km)" map layer. Plain script, see index.html. */
const isRoadsQuery = q => /^(roads?|streets?|stra(ß|ss)en|fahrstreifen|lanes)$/i.test(q.trim());
let roadData = null;

async function roadsSearch() {
  const ctl = controller = new AbortController();
  setStatus("Calculating…");
  $("go").textContent = "Cancel";
  try {
    const res = await fetch(`${API}/roads/stats`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ geometry: populationGeometry() }), signal: ctl.signal,
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || res.statusText);
    roadData = data; showRoads();
  } catch (e) {
    if (e.name === "AbortError") { if (ctl === controller) setStatus("Cancelled."); return; }
    setStatus("Error: " + e.message, true);
  } finally {
    if (ctl === controller) { controller = null; $("go").textContent = "Search"; }
  }
}

function showRoads() {
  const d = roadData, by = Object.fromEntries(d.types.map(t => [t.id, t]));
  layer.clearLayers(); found = []; updateMarkerHint();
  const sum = k => [...roadSel].reduce((a, id) => a + (by[id]?.[k] || 0), 0);
  const lane = sum("lane_km"), km = sum("km"), tagged = sum("tagged_km");
  $("res-count").textContent = fmt(lane, lane < 100 ? 1 : 0);
  $("res-label").textContent = "lane-km";
  setStatus(`in ${area ? area.name : "the visible map area"}`);
  const row = (label, value, hint = "") => `<div class="row"${hint ? ` title="${esc(hint)}"` : ""}><span>${label}</span><b>${value}</b></div>`;
  $("res-list").innerHTML = `<div class="pop-stats">
    <h4>Map</h4>
    <div class="rd-map">${[["off", "Off", "No map view"], ["grid", "Grid", "1 km squares"], ...Object.entries(ROAD_LEVELS).map(([l, [n, h]]) => [l, n, h])]
      .map(([v, n, h]) => `<button type="button" data-rv="${v}" title="${esc(h)}" class="${roadView === v ? "on" : ""}">${n}</button>`).join("")}</div>
    <h4>Ratio</h4>
    ${row("Per km²", num(d.area_km2 ? lane / d.area_km2 : null, 1, " lane-km"), `Area ${fmt(d.area_km2, 2)} km²`)}
    ${row("Per person", d.population ? num(lane * 1000 / d.population, 1, " m") : dash, d.population ? `${d.population.toLocaleString()} inhabitants` : "No population data here")}
    ${row("Road length", num(km, 1, " km"))}
    <h4>Types <span class="rd-all"><a data-rd="cars">cars</a> <a data-rd="all">all</a> <a data-rd="none">none</a></span></h4>
    ${ROAD_TYPES.map(([id, name]) => `<label class="row rd-type${roadSel.has(id) ? "" : " off"}" title="${esc(ROAD_HINT[id])}">
      <span><input type="checkbox" data-rd="${id}"${roadSel.has(id) ? " checked" : ""}> ${name}</span>
      <b>${by[id] ? fmt(by[id].lane_km, 1) : "0"} <small>lane-km</small></b></label>`).join("")}
    <div class="note">Lanes: lanes tag on ${km ? Math.round(tagged / km * 100) : 0} % of the length, else estimated
      (two-way 2, one-way 1, motorway 4 / 2; service, tracks, paths 1). Dual carriageways count both halves.
      Cut at the border of the area.</div>
  </div>`;
}

$("res-list").addEventListener("change", e => {
  const id = e.target.dataset?.rd;
  if (!id || !roadData) return;
  e.target.checked ? roadSel.add(id) : roadSel.delete(id);
  roadsChanged();
});
$("res-list").addEventListener("click", e => {
  const v = e.target.closest("[data-rv]");
  if (v) return setRoadView(v.dataset.rv);
  const a = e.target.closest("a[data-rd]");
  if (!a || !roadData) return;
  roadSel.clear();
  for (const [id] of ROAD_TYPES) if (a.dataset.rd === "all" || a.dataset.rd === "cars" && !ROAD_OFF.includes(id)) roadSel.add(id);
  roadsChanged();
});
function roadsChanged() {
  store.set("roadTypes3", [...roadSel]);
  showRoads();
  if (map.hasLayer(overlays[ROADS])) overlays[ROADS].redraw();
  roadAdminRender();
  updateLegend();
}

// ---------- map view: 1 km grid (layer "Road lanes") or administrative areas, value: total / per km² / per person ----------
map.createPane("roadadmin").style.zIndex = 245;     // above the population grids, below slope and labels
let adminData = null, adminCtl = null, adminTimer = 0;
function setRoadView(v) {
  roadView = v; store.set("roadView", v);
  const grid = overlays[ROADS];
  if (v === "grid") { if (!map.hasLayer(grid)) grid.addTo(map); } else if (map.hasLayer(grid)) map.removeLayer(grid);
  if (ROAD_LEVELS[v]) { roadAdmin.addTo(map); adminData = null; roadAdmin.clearLayers(); loadAdmin(); }
  else { map.removeLayer(roadAdmin); adminData = null; roadAdminRange = null; }
  if (roadData && isRoadsQuery($("q").value)) showRoads();
  updateLegend();
}
// the layer menu switches the grid on / off too
map.on("overlayadd", e => { if (e.layer === overlays[ROADS] && roadView !== "grid") setRoadView("grid"); });
map.on("overlayremove", e => { if (e.layer === overlays[ROADS] && roadView === "grid") setRoadView("off"); });
map.on("moveend", () => {
  if (!ROAD_LEVELS[roadView]) return;
  clearTimeout(adminTimer); adminTimer = setTimeout(loadAdmin, 300);
});

async function loadAdmin() {
  const level = roadView;
  if (!ROAD_LEVELS[level]) return;
  adminCtl?.abort();
  const ctl = adminCtl = new AbortController(), b = map.getBounds();
  const sw = L.CRS.EPSG3857.project(b.getSouthWest()), ne = L.CRS.EPSG3857.project(b.getNorthEast());
  try {
    const r = await fetch(`${API}/roads/admin?${new URLSearchParams({ level, minx: sw.x, miny: sw.y, maxx: ne.x, maxy: ne.y, zoom: map.getZoom() })}`, { signal: ctl.signal });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || r.statusText);
    if (level !== roadView) return;
    adminData = d; roadAdminRender();
  } catch (e) {
    if (e.name !== "AbortError") { $("notice").hidden = false; $("notice").firstElementChild.textContent = e.message + " – "; }
  }
}

// value of an area for the chosen types and mode (null: no inhabitants known)
function adminValue(a) {
  const lane = adminData.types.reduce((s, id, i) => s + (roadSel.has(id) ? a.lane_km[i] : 0), 0);
  return roadPer === "total" ? lane : roadPer === "area" ? (a.km2 ? lane / a.km2 : null) : a.population ? lane * 1000 / a.population : null;
}
const roadFmt = v => v == null ? dash : fmt(v, v < 10 ? 1 : 0);

function roadAdminRender() {
  roadAdmin.clearLayers();
  if (!adminData || !ROAD_LEVELS[roadView]) return;
  const vals = adminData.areas.map(adminValue), pos = vals.filter(v => v > 0);
  roadAdminRange = pos.length ? [Math.max(Math.min(...pos), Math.max(...pos) / 1000), Math.max(...pos) * 1.0001] : [1, 10];
  const op = store.get("roadOpacity", 0.7);
  adminData.areas.forEach((a, i) => {
    const v = vals[i];
    const poly = L.geoJSON(a.geometry, { pane: "roadadmin", style: { color: "#5b1a0e", weight: 1, opacity: 0.8,
      fillColor: v > 0 ? rampColor(ROAD_SCALE, roadT(v)) : "#bbb", fillOpacity: v > 0 ? op : op * 0.4 } });
    poly.bindPopup(`<b>${esc(a.name || "–")}</b><br>${roadFmt(adminValue({ ...a }))} ${ROAD_UNIT[roadPer]}<br>
      <small>${fmt(a.km2, 1)} km² · ${a.population == null ? "no population data" : a.population.toLocaleString() + " inhabitants"}</small>`);
    poly.addTo(roadAdmin);
    // the number in the middle, where the area is big enough on the screen
    const bb = poly.getBounds(), p1 = map.latLngToContainerPoint(bb.getSouthWest()), p2 = map.latLngToContainerPoint(bb.getNorthEast());
    if (Math.abs(p2.x - p1.x) > 44 && Math.abs(p1.y - p2.y) > 18)
      L.marker([a.label[1], a.label[0]], { interactive: false, pane: "roadadmin",
        icon: L.divIcon({ className: "rd-lbl", html: `<span>${roadFmt(v)}</span>`, iconSize: null }) }).addTo(roadAdmin);
  });
  updateLegend();
}
setRoadView(map.hasLayer(overlays[ROADS]) ? "grid" : store.get("roadView", "off"));
$("road-btn").addEventListener("click", () => {       // Roads tool = the roads statistics in the sidebar
  closeOtherTools();
  $("q").value = "roads"; search();
});
