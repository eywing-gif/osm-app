/* Split view: the numbers of a statistic on the map, per grid square (size: footer "Grid") or per administrative area
   (State … Quarter, without gaps: backend/roads.py VIEWS). Chosen in "Search in" → "split by"; one menu entry "Split view".
   The statistic is the one used last: Population, Roads (types from the Roads list), Transit → Supply (day, types) or
   Amenities (scores per square, or the places of the chosen groups).
   Value: total, per km² or per inhabitant. Plain script: the files share one scope and load in this order (see index.html). */
const SPLIT_LEVELS = { 4: ["State", "admin_level 4: Bundesland"], 6: ["County", "admin_level 6: Landkreis, kreisfreie Stadt"],
  8: ["Town", "admin_level 8: Gemeinde, Stadt"], 9: ["Borough", "admin_level 9: Stadtbezirk"], 10: ["Quarter", "admin_level 10: Stadtteil, Ortsteil"] };
const SPLITS = [["grid", "Grid", "Squares – size in the footer"], ...Object.entries(SPLIT_LEVELS).map(([l, [n, h]]) => [l, n, h])];
const SPLIT_SCALE = ["#fff7ec", "#fdd49e", "#fc8d59", "#d7301f", "#7f0000", "#2b0000"];
const SPLIT_MODES = { score: "Score", within15: "15 min", total: "Total", area: "per km²", person: "per person" };
const SCORE_SCALE = ["#b91c1c", "#f97316", "#facc15", "#a3e635", "#16a34a", "#065f46"];   // bad → good (linear scores)
const SPLIT = "Split view";
const dayLabel = () => ({ wd: "Weekday", sa: "Saturday", su: "Sunday" })[typeof tr === "undefined" ? "wd" : tr.day];

// what each statistic shows: units, colour range (log scale; null: from the areas shown), grid tiles, value of an area
const SUBJECTS = {
  population: {
    title: "Inhabitants", modes: ["total", "area"], grid: null,             // the grid is the population density layer
    unit: { total: "inhabitants", area: "inhabitants per km²" }, range: { total: [10, 20000], area: [10, 20000] },
    value: (a, m) => a.population == null ? null : m === "total" ? a.population : a.km2 ? a.population / a.km2 : null,
  },
  roads: {
    title: "Road lanes", modes: ["total", "area", "person"],
    unit: { total: "lane-km", area: "lane-km per km²", person: "lane-m per inhabitant" }, range: { total: [2, 500], area: [2, 500], person: [1, 300] },
    note: () => `${roadSel.size} of ${ROAD_TYPES.length} types (Roads)`,
    grid: (z, x, y, m) => `${API}/roads/grid/${z}/${x}/${y}?` + new URLSearchParams({ types: [...roadSel].join(","), per: m, size: gridSizeFor("roads"), v: dataInfo.population_id }),
    value: (a, m) => {
      const lane = splitData.types.reduce((s, id, i) => s + (roadSel.has(id) ? a.lane_km[i] : 0), 0);
      return m === "total" ? lane : m === "area" ? (a.km2 ? lane / a.km2 : null) : a.population ? lane * 1000 / a.population : null;
    },
  },
  transit: {
    title: "Departures", modes: ["total", "area", "person"],
    unit: { total: "departures", area: "departures per km²", person: "departures per 1,000 inhabitants" },
    range: { total: [5, 5000], area: [5, 5000], person: [10, 10000] },
    note: () => `${dayLabel()}, 07–19 h · ${typeof tr === "undefined" ? "" : tr.modes.size + " types"}`,
    grid: (z, x, y, m) => `${API}/transit/supply/grid/${z}/${x}/${y}?` + new URLSearchParams({ day: tr.day, modes: [...tr.modes].join(","), per: m, size: gridSizeFor("transit"), v: dataInfo.transit_name }),
    extra: (level, box) => fetch(`${API}/transit/supply/admin?` + new URLSearchParams({ level, ...box, day: tr.day, modes: [...tr.modes].join(",") })),
    value: (a, m) => m === "total" ? a.dep : m === "area" ? (a.km2 ? a.dep / a.km2 : null) : a.population ? a.dep * 1000 / a.population : null,
  },
  amenities: {
    title: "Amenities", modes: ["score", "within15", "total", "area", "person"], gridOnly: ["score", "within15"],
    linear: { score: [0, 100], within15: [0, 10] },
    unit: { score: "Amenity score 0–100 (grade 1–6)", within15: "groups within 15 min (of 10)", total: "places", area: "places per km²", person: "places per 1,000 inhabitants" },
    range: { total: [1, 200], area: [1, 500], person: [1, 200] },
    note: () => typeof amenSel === "undefined" ? "" : `${amenSel.size} of 10 groups`,
    grid: (z, x, y, m) => `${API}/amenities/grid/${z}/${x}/${y}?` + new URLSearchParams({ metric: m, groups: [...amenSel].join(","), size: gridSizeFor("amenities"), v: dataInfo.osm.join() }),
    extra: (level, box) => fetch(`${API}/amenities/admin?` + new URLSearchParams({ level, ...box, groups: [...amenSel].join(",") })),
    value: (a, m) => m === "total" ? a.dep : m === "area" ? (a.km2 ? a.dep / a.km2 : null) : a.population ? a.dep * 1000 / a.population : null,
  },
};

let splitView = "off";                                  // off · grid · an admin level
let splitSubject = SUBJECTS[store.get("splitSubject")] ? store.get("splitSubject") : "roads";
let splitMode = store.get("splitMode", store.get("roadPer", "area"));
let splitData = null, splitRange = null, splitCtl = null, splitTimer = 0, splitPopGrid = false, splitBusy = false;
const subject = () => SUBJECTS[splitSubject];
const mode = () => !subject().modes.includes(splitMode) || splitView !== "grid" && subject().gridOnly?.includes(splitMode) ? "area" : splitMode;
const linearNow = () => subject().linear?.[mode()];
const splitOpacity = () => store.get("splitOpacity", store.get("roadOpacity", 0.7));
const rangeNow = () => splitView !== "grid" && mode() === "total" && splitRange ? splitRange : subject().range[mode()];
const splitT = v => {
  const lin = linearNow(), [lo, hi] = lin || rangeNow();
  return Math.min(1, Math.max(0, lin ? (v - lo) / (hi - lo) : Math.log(v / lo) / Math.log(hi / lo)));
};
const splitColor = v => rampColor(linearNow() ? SCORE_SCALE : SPLIT_SCALE, splitT(v));
const splitFmt = v => v == null ? dash : fmt(v, v < 10 ? 1 : 0);

map.createPane("splitadmin").style.zIndex = 245;      // above the population grids, below slope and labels
const splitGrid = new GridCellLayer({ minZoom: 6, maxNativeZoom: 16, maxZoom: 19, opacity: splitOpacity(), pane: "popdensity", spec: {
  url: (z, x, y) => subject().grid(z, x, y, mode()),
  t: v => splitT(v), color: v => splitColor(v), show: v => v > 0, text: v => String(Math.round(v)),
} });
const splitAdmin = L.layerGroup();
const splitGroup = L.layerGroup([], { attribution: "Areas: © OpenStreetMap contributors" });   // the menu entry: grid or areas
overlays[SPLIT] = splitGroup;
layerControl.addOverlay(splitGroup, SPLIT);
const splitGridShown = () => splitView === "grid" && !!subject().grid;

function setSplitView(v) {
  splitBusy = true;          // our own layer changes must not count as clicks in the layer menu
  try { applySplitView(v); } finally { splitBusy = false; }
  renderSplitRow();
  updateLegend();
}
function applySplitView(v) {
  splitView = v; store.set("splitView", v);
  splitFocus = null; focusLayer.clearLayers();
  if (v !== "off") store.set("splitLastView", v);
  const grid = v === "grid" && !!subject().grid, popGrid = v === "grid" && !subject().grid;
  if (grid) splitGroup.addLayer(splitGrid); else splitGroup.removeLayer(splitGrid);
  if (popGrid && !map.hasLayer(popLayer)) { popLayer.addTo(map); splitPopGrid = true; }
  if (!popGrid && splitPopGrid) { map.removeLayer(popLayer); splitPopGrid = false; }
  if (SPLIT_LEVELS[v]) { splitGroup.addLayer(splitAdmin); splitData = null; splitAdmin.clearLayers(); loadSplitAreas(); }
  else { splitGroup.removeLayer(splitAdmin); splitData = null; splitRange = null; }
  if (v === "off" || popGrid) map.removeLayer(splitGroup); else if (!map.hasLayer(splitGroup)) splitGroup.addTo(map);
}
// the statistic used last decides what the split view shows
function setSplitSubject(s) {
  if (s === splitSubject) return;
  splitSubject = s; store.set("splitSubject", s);
  if (splitView !== "off") setSplitView(splitView);
}
// new types / day / grid size: draw again (refetch: the numbers of the areas depend on it too)
function splitRedraw(refetch = false) {
  if (splitView === "grid" && subject().grid) splitGrid.redraw();
  if (SPLIT_LEVELS[splitView]) refetch && subject().extra ? loadSplitAreas() : renderSplitAreas();
  updateLegend();
}

// "split by" buttons in the "Search in" picker; the chip says which one is on
function renderSplitRow() {
  $("split-row").innerHTML = '<span>or split by:</span>' + SPLITS.map(([v, n, h]) =>
    `<button type="button" data-split="${v}" title="${esc(h)}" class="${splitView === v ? "active" : ""}">${n}</button>`).join("");
  if (splitView !== "off") {
    $("area-chip-text").textContent = "Split: " + SPLITS.find(s => s[0] === splitView)[1] + (splitFocus ? " · " + splitFocus.name : "");
    $("area-chip").classList.add("set"); $("area-clear").style.display = "block";
  } else if (!area) {
    $("area-chip-text").textContent = "Visible map area";
    $("area-chip").classList.remove("set"); $("area-clear").style.display = "none";
  }
}
$("split-row").addEventListener("click", e => {
  const b = e.target.closest("[data-split]"); if (!b) return;
  if (area) clearArea(false);                  // the statistics are for the visible map area, split on the map
  toggleAreaPicker(false);
  setSplitView(b.dataset.split);
  if (splitSubject === "transit" && typeof tr !== "undefined" && tr.on) return trSupply();
  if (!Object.values(SUBJECT_QUERY).some(f => f($("q").value))) $("q").value = { population: "population", amenities: "amenities" }[splitSubject] || "roads";
  search();
});
// the layer menu switches it on / off too
map.on("overlayadd", e => { if (!splitBusy && e.layer === splitGroup && splitView === "off") setSplitView(store.get("splitLastView", "grid")); });
map.on("overlayremove", e => { if (!splitBusy && e.layer === splitGroup && splitView !== "off") setSplitView("off"); });
map.on("moveend", () => {
  if (!SPLIT_LEVELS[splitView]) return;
  clearTimeout(splitTimer); splitTimer = setTimeout(loadSplitAreas, 300);
});

// ---- administrative areas: outlines, area, inhabitants, lane-km (/roads/admin) + departures (Transit)
async function loadSplitAreas() {
  const level = splitView, subj = splitSubject;
  if (!SPLIT_LEVELS[level]) return;
  splitCtl?.abort();
  const ctl = splitCtl = new AbortController(), b = map.getBounds();
  const sw = L.CRS.EPSG3857.project(b.getSouthWest()), ne = L.CRS.EPSG3857.project(b.getNorthEast());
  const box = { minx: sw.x, miny: sw.y, maxx: ne.x, maxy: ne.y };
  const json = async r => { const d = await (await r).json(); if (!(await r).ok) throw new Error(d.detail || "failed"); return d; };
  try {
    const [d, extra] = await Promise.all([
      json(fetch(`${API}/roads/admin?${new URLSearchParams({ level, ...box, zoom: map.getZoom() })}`, { signal: ctl.signal })),
      SUBJECTS[subj].extra ? json(SUBJECTS[subj].extra(level, box)) : null]);
    if (level !== splitView || subj !== splitSubject) return;
    if (extra) for (const a of d.areas) a.dep = extra[a.id] || 0;
    splitData = d; renderSplitAreas();
  } catch (e) {
    if (e.name !== "AbortError") { $("notice").hidden = false; $("notice").firstElementChild.textContent = e.message + " – "; }
  }
}
function renderSplitAreas() {
  splitAdmin.clearLayers();
  if (!splitData || !SPLIT_LEVELS[splitView]) return;
  const s = subject(), m = mode();
  if (s.extra && splitData.areas.some(a => a.dep === undefined)) return loadSplitAreas();     // came from another statistic
  const vals = splitData.areas.map(a => s.value(a, m)), pos = vals.filter(v => v > 0);
  splitRange = pos.length ? [Math.max(Math.min(...pos), Math.max(...pos) / 1000), Math.max(...pos) * 1.0001] : [1, 10];
  const op = splitOpacity();
  splitData.areas.forEach((a, i) => {
    const v = vals[i];
    const poly = L.geoJSON(a.geometry, { pane: "splitadmin", style: { color: "#5b1a0e", weight: 1, opacity: 0.8,
      fillColor: v > 0 ? splitColor(v) : "#bbb", fillOpacity: v > 0 ? op : op * 0.4 } });
    const from = a.level !== +splitView ? ` <small>(${SPLIT_LEVELS[a.level][0]}${a.level < +splitView ? ", no smaller units" : ""})</small>` : "";
    const info = `<b>${esc(a.name || "–")}</b>${from}<br>${splitFmt(v)} ${s.unit[m]}<br>
      <small>${fmt(a.km2, 1)} km² · ${a.population == null ? "no population data" : a.population.toLocaleString() + " inhabitants"}</small>`;
    poly.on("click", ev => {       // with the statistic open: its numbers for this area; else a short popup
      L.DomEvent.stop(ev);
      if (splitClickable()) setSplitFocus({ name: a.name || "–", geometry: a.geometry });
      else L.popup().setLatLng(ev.latlng).setContent(info).openOn(map);
    });
    poly.addTo(splitAdmin);
    // the number in the middle, where the area is big enough on the screen
    const bb = poly.getBounds(), p1 = map.latLngToContainerPoint(bb.getSouthWest()), p2 = map.latLngToContainerPoint(bb.getNorthEast());
    if (Math.abs(p2.x - p1.x) > 44 && Math.abs(p1.y - p2.y) > 18)
      L.marker([a.label[1], a.label[0]], { interactive: false, pane: "splitadmin",
        icon: L.divIcon({ className: "rd-lbl", html: `<span>${splitFmt(v)}</span>`, iconSize: null }) }).addTo(splitAdmin);
  });
  updateLegend();
}

// ---- click a square or an area: the statistic's panel shows its numbers ("focus"); × on the chip goes back to the view
let splitFocus = null;
const focusLayer = L.layerGroup().addTo(map);
const SUBJECT_QUERY = { roads: q => isRoadsQuery(q), population: q => isPopulationQuery(q), amenities: q => isAmenityQuery(q) };
const splitClickable = () => splitSubject === "transit" ? typeof tr !== "undefined" && tr.on && tr.view === "supply"
  : !/\b(trn|insp|lib|lu)-open\b/.test($("sidebar").className) && SUBJECT_QUERY[splitSubject]($("q").value);
function setSplitFocus(f) {
  splitFocus = f;
  focusLayer.clearLayers();
  if (f) L.geoJSON(f.geometry, { pane: "area", interactive: false, style: { color: "#2563eb", weight: 3, fill: false } }).addTo(focusLayer);
  renderSplitRow();
  if (splitSubject === "transit") trSupply(); else if ($("q").value.trim()) search();
}
map.on("click", async e => {
  if (splitView !== "grid" || splitSubject === "amenities" || !splitClickable()) return;   // amenities: the rating of the place (amenities.js)
  const tool = subject().grid ? splitSubject : "population", perPerson = splitSubject === "transit" && mode() === "person";
  const base = tool === "population" ? dataInfo.cell_m || 100 : perPerson ? 1000 : GRID_BASE[tool];
  const size = perPerson ? Math.max(gridSizeFor(tool), 1000) : gridSizeFor(tool);
  const r = await fetch(`${API}/roads/square?` + new URLSearchParams({ lon: e.latlng.lng, lat: e.latlng.lat, z: Math.min(map.getZoom(), 16), base, size }));
  const d = await r.json();
  if (r.ok) setSplitFocus({ name: sizeName(d.size_m) + " square", geometry: d.geometry });
});

// ---- legend block: value (total / per km² / per person), scale, strength
legend.insertAdjacentHTML("beforeend", `<div id="leg-split" hidden>
    <button type="button" id="split-close" class="leg-x" title="Hide" aria-label="Hide the split view">×</button><b id="split-title"></b>
    <div class="seg" id="split-mode" style="margin:4px 0;width:270px">${Object.entries(SPLIT_MODES).map(([k, n]) => `<button type="button" data-m="${k}">${n}</button>`).join("")}</div>
    <div class="grad" id="split-grad" style="width:270px"></div>
    <div class="ticks" id="split-ticks" style="position:relative;display:block;height:13px;width:270px"></div>
    <label class="op">Strength <input type="range" id="split-op" min="20" max="100" step="5" value="${Math.round(splitOpacity() * 100)}" aria-label="Split view strength"></label>
    <div class="note" id="split-note"></div></div>`);
function renderSplitLegend() {
  const s = subject(), m = mode();
  document.querySelectorAll("#split-mode button").forEach(b => {
    b.classList.toggle("on", b.dataset.m === m);
    b.hidden = !s.modes.includes(b.dataset.m) || splitView !== "grid" && !!s.gridOnly?.includes(b.dataset.m);
  });
  const lin = linearNow(), pal = lin ? SCORE_SCALE : SPLIT_SCALE;
  $("split-grad").style.background = `linear-gradient(to right, ${pal.map((c, i) => `${c} ${Math.round(i / (pal.length - 1) * 100)}%`).join(", ")})`;
  const [lo, hi] = lin || rangeNow(), dyn = !lin && rangeNow() === splitRange, nice = v => Number(v.toPrecision(v < 10 ? 1 : 2));
  const ticks = m === "score" ? [0, 25, 50, 70, 90, 100] : lin ? [0, 2, 4, 6, 8, 10] : [0, 1, 2, 3, 4].map(i => nice(lo * (hi / lo) ** (i / 4))), last = ticks.at(-1);
  $("split-ticks").innerHTML = ticks.map(v => `<span style="position:absolute;${v === last ? "right:0" : v === ticks[0] ? "left:0" : `left:${Math.round(splitT(v) * 100)}%;transform:translateX(-50%)`}">${v.toLocaleString()}${v === last && !dyn && !lin ? "+" : ""}</span>`).join("");
  const where = SPLIT_LEVELS[splitView] ? SPLIT_LEVELS[splitView][0] : `grid ${gridSizeFor(splitSubject) ? sizeName(gridSizeFor(splitSubject)) : "(auto)"}`;
  $("split-title").textContent = `${s.title} · ${where}`;
  $("split-note").textContent = s.unit[m] + (m === "total" && splitView === "grid" ? " per square" : "") + (s.note ? " · " + s.note() : "");
}
legendHooks.push(() => {
  const on = map.hasLayer(splitGroup);
  $("leg-split").hidden = !on;
  if (on) renderSplitLegend();
  return on;
});
$("split-close").addEventListener("click", () => setSplitView("off"));
$("split-mode").addEventListener("click", e => {
  const b = e.target.closest("button"); if (!b) return;
  splitMode = b.dataset.m; store.set("splitMode", splitMode);
  splitRedraw();
});
$("split-op").addEventListener("input", e => {
  store.set("splitOpacity", e.target.value / 100);
  splitGrid.setOpacity(e.target.value / 100);
  renderSplitAreas();
});

setSplitView(store.get("overlays", []).includes(SPLIT) ? store.get("splitLastView", "grid") : "off");
