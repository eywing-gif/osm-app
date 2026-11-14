/* Tag search, results, population statistics, search area, suggestions. Plain script: the files share one scope and load in this order (see index.html). */
// ---------- search ----------
// ---------- data source ----------
const ONLINE_MIN_ZOOM = 11;   // public servers can't handle huge areas
let source = store.get("source", "local");
function setSource(s) {
  source = s;
  store.set("source", s);
  document.querySelectorAll(".seg button[data-src]").forEach(b => {
    b.classList.toggle("on", b.dataset.src === s);
    b.setAttribute("aria-checked", b.dataset.src === s);
  });
}
setSource(source);
document.querySelectorAll("#source button").forEach(b => b.addEventListener("click", () => {
  if (b.dataset.src === source) return;
  setSource(b.dataset.src);
  if ($("q").value.trim()) search();
}));

const setStatus = (text, error = false) => {
  $("status").textContent = text;
  $("status").className = error ? "error" : "";
};

// ---------- results ----------
const LIST_MAX = 300;
let found = [];                       // [{marker, props}] – what is on the map now
let suppressFollowUntil = 0;          // timestamp: "Follow map" stays quiet until then
const HELP_HTML = $("res-list").innerHTML;

const updateMarkerHint = () => $("mk-hint").hidden = map.hasLayer(layer) || !found.length;

function clearResults() {
  layer.clearLayers();
  found = [];
  updateMarkerHint();
  $("res-count").textContent = "–";
  $("res-label").textContent = "objects found";
  $("res-list").innerHTML = HELP_HTML;
}

function showResults(data) {
  layer.clearLayers();
  found = data.features.map(f => {
    const [lon, lat] = f.geometry.coordinates, p = f.properties;
    const marker = L.circleMarker([lat, lon], {
      radius: 6, weight: 1, color: "#fff", fillColor: color(p.category), fillOpacity: .9
    }).bindPopup(() => popup(p), { maxWidth: 320 }).addTo(layer);
    return { marker, p };
  });
  updateMarkerHint();
  const n = found.length, where = area ? `in ${area.name}` : "in the visible map area";
  $("res-count").textContent = n.toLocaleString() + (data.truncated ? "+" : "");
  $("res-label").textContent = n === 1 ? "object found" : "objects found";
  setStatus(n === 0 ? `No results ${where}.`
    : `${where}` + (data.truncated ? (area ? " – showing the first 2,000" : " – zoom in for all") : ""));

  // list: named objects first (A–Z), then the unnamed ones
  const order = found.map((_, i) => i).sort((a, b) => {
    const x = found[a].p.name, y = found[b].p.name;
    return (!x - !y) || (x && x.localeCompare(y));
  });
  $("res-list").innerHTML = n === 0 ? "" : order.slice(0, LIST_MAX).map(i => {
    const p = found[i].p;
    return `<div class="ri" data-i="${i}">
      <span class="dot" style="background:${color(p.category)}"></span>
      <div><div class="rn${p.name ? "" : " none"}">${esc(p.name ?? "(no name)")}</div>
      <div class="rc">${esc(p.category ?? "")}</div></div></div>`;
  }).join("") + (n > LIST_MAX ? `<div class="res-more">Showing the first ${LIST_MAX} of ${n.toLocaleString()} – all are on the map.</div>` : "");
}

// list <-> map: hover highlights the marker, click jumps to it
const itemOf = e => { const el = e.target.closest(".ri"); return el && found[+el.dataset.i]; };
$("res-list").addEventListener("mouseover", e => itemOf(e)?.marker.setRadius(10));
$("res-list").addEventListener("mouseout", e => itemOf(e)?.marker.setRadius(6));
$("res-list").addEventListener("click", e => {
  const item = itemOf(e), m = item?.marker;
  if (!m) { const code = e.target.closest("#help code"); if (code) { $("q").value = code.textContent; updateClear(); search(); } return; }
  suppressFollowUntil = Date.now() + 1500;   // jumping to a result must not replace the results
  map.setView(m.getLatLng(), Math.max(map.getZoom(), 16));
  if (map.hasLayer(layer)) m.openPopup();
  else L.popup({ maxWidth: 320 }).setLatLng(m.getLatLng()).setContent(popup(item.p)).openOn(map);   // markers are hidden
});

let controller = null;
async function search() {
  const q = $("q").value.trim();
  if (!q) return;
  closeSuggest();
  closeLibrary();
  controller?.abort();
  if (isPopulationQuery(q)) return populationSearch();
  if (isRoadsQuery(q)) return roadsSearch();
  if (source === "online" && !area && map.getZoom() < ONLINE_MIN_ZOOM) {
    clearResults();
    setStatus("Zoom in further (or pick an area) to search online.");
    return;
  }
  const ctl = controller = new AbortController();
  const b = map.getBounds();
  const body = { q, source, limit: 2000, bbox: [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()] };
  if (area) body.area = { geometry: area.geometry, osm_type: area.osm_type, osm_id: area.osm_id };
  setStatus(source === "online" ? "Asking the OpenStreetMap server… (can take up to a minute)" : "Searching…");
  $("go").textContent = "Cancel";
  try {
    const res = await fetch(`${API}/search`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body), signal: ctl.signal,
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || res.statusText);
    showResults(data);
  } catch (e) {
    if (e.name === "AbortError") { if (ctl === controller) setStatus("Search cancelled."); return; }
    setStatus("Error: " + e.message, true);
  } finally {
    if (ctl === controller) { controller = null; $("go").textContent = "Search"; }
  }
}

// ---------- population statistics ----------
const isPopulationQuery = q => /^(population|bev[oö]lkerung|einwohner|census|zensus)$/i.test(q.trim());

// the area the statistics are about: the chosen / drawn area, otherwise what is visible on the map
function populationGeometry() {
  if (area) return area.geometry;
  const b = map.getBounds(), clamp = (v, m) => Math.max(-m, Math.min(m, v));
  const w = clamp(b.getWest(), 180), e = clamp(b.getEast(), 180), s = clamp(b.getSouth(), 85), n = clamp(b.getNorth(), 85);
  return { type: "Polygon", coordinates: [[[w, s], [e, s], [e, n], [w, n], [w, s]]] };
}

const dash = "–";
const num = (v, d = 0, unit = "") => v == null ? dash : fmt(v, d) + unit;

function showPopulation(d) {
  layer.clearLayers(); found = []; updateMarkerHint();
  $("res-count").textContent = d.cells ? d.population.toLocaleString() : dash;
  $("res-label").textContent = "inhabitants";
  const where = area ? area.name : "the visible map area";
  if (!d.cells) {
    setStatus(`No population data in ${where}.`);
    $("res-list").innerHTML = "";
    return;
  }
  setStatus(`in ${where} · ${d.dataset}`);
  // rows without a value (a grid that has no age / housing data) are left out
  const row = (label, value, hint = "") => value === dash ? "" : `<div class="row"${hint ? ` title="${esc(hint)}"` : ""}><span>${label}</span><b>${value}</b></div>`;
  const section = (title, ...rows) => rows.join("") ? `<h4>${title}</h4>${rows.join("")}` : "";
  $("res-list").innerHTML = `<div class="pop-stats">
    ${section("Population",
      row("Area", num(d.area_km2, d.area_km2 < 10 ? 2 : 1, " km²")),
      row("Density", num(d.density, 0, " /km²"), "Inhabitants per km² of the whole area, including fields, forests and water"),
      row("Density where people live", num(d.density_populated, 0, ` /km²`), `Only counting the ${d.cell_m} m cells that have residents`))}
    ${section("People", row("Average age", num(d.avg_age, 1, " years")), row("Under 18", num(d.pct_under18, 1, " %")),
      row("65 and older", num(d.pct_over65, 1, " %")), row("Foreign nationals", num(d.pct_foreign, 1, " %")))}
    ${section("Housing", row("Household size", num(d.hh_size, 2, " persons")), row("Living space per person", num(d.living_space, 1, " m²")),
      row("Net rent (excl. utilities)", num(d.rent_sqm, 2, " €/m²"), "Average of rented flats"),
      row("Owner-occupied", num(d.pct_owner, 1, " %"), "Share of flats lived in by their owners"), row("Vacant flats", num(d.pct_vacant, 1, " %")))}
    <div class="note">${esc(d.dataset)} · ${d.cell_m} m grid. Cells cut by the border count in proportion to the part inside${d.border_cells ? ` (about ${d.border_cells.toLocaleString()} cell-equivalents on the border)` : ""}.
      Averages are weighted by population. Very small areas are approximate.</div>
  </div>`;
}

async function populationSearch() {
  const ctl = controller = new AbortController();
  setStatus("Calculating…");
  $("go").textContent = "Cancel";
  try {
    const res = await fetch(`${API}/population`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ geometry: populationGeometry() }), signal: ctl.signal,
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || res.statusText);
    showPopulation(data);
  } catch (e) {
    if (e.name === "AbortError") { if (ctl === controller) setStatus("Cancelled."); return; }
    setStatus("Error: " + e.message, true);
  } finally {
    if (ctl === controller) { controller = null; $("go").textContent = "Search"; }
  }
}

// ---------- search area ----------
// area = details from /areas (name, kind, geometry, area_km2, …) or a drawn shape (drawn: true)
let area = null;
let drawnLayer = null;        // the editable shape, if the area was drawn
let editing = false;
map.createPane("area").style.zIndex = 350;   // above map + slope, below the results
const areaLayers = L.layerGroup().addTo(map);
const AREA_STYLE = { color: "#2563eb", weight: 2.5 };
const fmt = (n, digits = 1) => Number(n).toLocaleString(undefined, { maximumFractionDigits: digits });

function toggleAreaPicker(open = $("area-picker").hidden) {
  $("area-picker").hidden = !open;
  $("area-chip").setAttribute("aria-expanded", open);
  $("area-info").hidden = open || !area;
  if (open) $("area-q").focus();
}
$("area-chip").addEventListener("click", () => toggleAreaPicker());

// -- find areas by name
let areaResults = [];
$("area-form").addEventListener("submit", async e => {
  e.preventDefault();
  const q = $("area-q").value.trim();
  if (q.length < 2) return;
  const box = $("area-results");
  box.innerHTML = `<div class="ar-msg">Searching areas…</div>`;
  try {
    const res = await fetch(`${API}/areas?` + new URLSearchParams({ q }));
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || res.statusText);
    areaResults = data;
    box.innerHTML = data.length ? data.map((a, i) => `
      <div class="ar" data-i="${i}">
        <b>${esc(a.name)}</b>
        <span class="meta">· ${esc(a.kind)}${a.area_km2 != null ? " · " + fmt(a.area_km2) + " km²" : ""}</span>
        ${a.context ? `<div class="meta">${esc(a.context)}</div>` : ""}
      </div>`).join("")
      : `<div class="ar-msg">No area found for “${esc(q)}”.</div>`;
  } catch (err) {
    box.innerHTML = `<div class="ar-msg">Error: ${esc(err.message)}</div>`;
  }
});
$("area-results").addEventListener("click", async e => {
  const el = e.target.closest(".ar");
  if (!el) return;
  const a = areaResults[+el.dataset.i];
  el.classList.add("active");
  el.insertAdjacentHTML("beforeend", `<div class="meta loading">Loading outline…</div>`);
  try {
    await loadOsmArea(a.osm_type, a.osm_id);
  } catch (err) {
    el.querySelector(".loading").textContent = "Error: " + err.message;
  }
});

async function loadOsmArea(osmType, osmId, fit = true) {
  const res = await fetch(`${API}/areas/${osmType}/${osmId}`);
  const info = await res.json();
  if (!res.ok) throw new Error(info.detail || res.statusText);
  setArea(info, null, fit);
}

// -- show / remove the area
function setArea(info, shapeLayer = null, fit = true) {
  clearArea(false);
  area = info;
  if (shapeLayer) {
    drawnLayer = shapeLayer;
  } else {
    L.geoJSON(info.geometry, { pane: "area", interactive: false,
                               style: { ...AREA_STYLE, dashArray: "6 5", fill: false } }).addTo(areaLayers);
  }
  drawMask();
  $("area-chip-text").textContent = info.name;
  $("area-chip").classList.add("set");
  $("area-clear").style.display = "block";
  toggleAreaPicker(false);
  renderAreaInfo();
  if (fit) zoomToArea();
  saveArea();
  if ($("q").value.trim()) search();
}

function clearArea(research = true) {
  const had = !!area;
  area = null;
  areaLayers.clearLayers();
  if (drawnLayer) { drawnLayer.pm?.disable(); map.removeLayer(drawnLayer); drawnLayer = null; }
  editing = false;
  $("area-chip-text").textContent = "Visible map area";
  $("area-chip").classList.remove("set");
  $("area-clear").style.display = "none";
  $("area-info").hidden = true;
  store.set("area", null);
  if (research && had && $("q").value.trim()) search();
}
$("area-clear").addEventListener("click", () => clearArea());

// dim everything outside the area
let mask = null;
function drawMask() {
  if (mask) areaLayers.removeLayer(mask);
  const g = area.geometry;
  const polys = g.type === "Polygon" ? [g.coordinates] : g.coordinates;
  const holes = polys.map(p => p[0].map(([x, y]) => [y, x]));
  mask = L.polygon([[[-90, -360], [-90, 360], [90, 360], [90, -360]], ...holes],
    { pane: "area", interactive: false, stroke: false, fillColor: "#0f172a", fillOpacity: 0.2 }).addTo(areaLayers);
}

function zoomToArea() {
  const [x1, y1, x2, y2] = area.bbox;
  map.fitBounds([[y1, x1], [y2, x2]], { padding: [30, 30] });
}

function wikiUrl(w) {   // "de:Bezirk Pankow" -> https://de.wikipedia.org/wiki/Bezirk_Pankow
  const m = /^([a-z-]+):(.+)$/.exec(w || "");
  return m && `https://${m[1]}.wikipedia.org/wiki/${encodeURIComponent(m[2].replace(/ /g, "_"))}`;
}

function renderAreaInfo() {
  const a = area;
  const pop = a.population && Number(String(a.population).replace(/[^\d]/g, ""));
  const stats = [["Size", `${fmt(a.area_km2, a.area_km2 < 10 ? 2 : 1)} km²`],
                 ["Perimeter", `${fmt(a.perimeter_km)} km`]];
  if (a.radius_m) stats.push(["Radius", a.radius_m >= 1000 ? `${fmt(a.radius_m / 1000, 2)} km` : `${fmt(a.radius_m, 0)} m`]);
  if (pop) {
    stats.push(["Population", fmt(pop, 0) + (a.population_date ? ` (${String(a.population_date).slice(0, 4)})` : "")]);
    stats.push(["Density", `${fmt(pop / a.area_km2, 0)} /km²`]);
  }
  const links = [
    wikiUrl(a.wikipedia) && `<a href="${esc(wikiUrl(a.wikipedia))}" target="_blank">Wikipedia</a>`,
    a.osm_type && `<a href="https://www.openstreetmap.org/${a.osm_type}/${a.osm_id}" target="_blank">OSM</a>`,
  ].filter(Boolean).join(" · ");
  $("area-info").innerHTML = `
    <div class="title">${esc(a.name)}</div>
    <div class="sub">${esc(a.kind)}${a.part_of ? " · in " + esc(a.part_of) : ""}</div>
    <div class="stats">${stats.map(([k, v]) => `<div class="stat"><span>${k}</span><b>${esc(v)}</b></div>`).join("")}</div>
    <div class="area-actions">
      <button type="button" data-act="zoom">Zoom to area</button>
      ${a.drawn ? `<button type="button" data-act="edit">${editing ? "Done editing" : "Edit shape"}</button>` : ""}
      <button type="button" data-act="change">Change…</button>
      ${links ? `<span class="links">${links}</span>` : ""}
    </div>`;
  $("area-info").hidden = !$("area-picker").hidden;
}
$("area-info").addEventListener("click", e => {
  const act = e.target.closest("button")?.dataset.act;
  if (act === "zoom") zoomToArea();
  if (act === "change") toggleAreaPicker(true);
  if (act === "edit" && drawnLayer) {
    editing = !editing;
    editing ? drawnLayer.pm.enable({ allowSelfIntersection: false }) : drawnLayer.pm.disable();
    renderAreaInfo();
  }
});

function saveArea() {
  store.set("area", !area ? null : area.drawn
    ? { drawn: true, shape: area.shape, geometry: area.geometry,
        circle: area.shape === "Circle" ? { ...drawnLayer.getLatLng(), r: drawnLayer.getRadius() } : null }
    : { osm_type: area.osm_type, osm_id: area.osm_id });
}

// -- draw your own area
map.pm.setGlobalOptions({
  pathOptions: { ...AREA_STYLE, fillOpacity: 0.04 },
  templineStyle: AREA_STYLE, hintlineStyle: { ...AREA_STYLE, dashArray: "5 5" },
  allowSelfIntersection: false, snappable: false,
});
const DRAW_HINTS = {
  Polygon: "Click to set corners – click the first point to finish.",
  Rectangle: "Click one corner, then the opposite corner.",
  Circle: "Click the centre, then click again to set the radius.",
};
let drawing = null;
document.querySelectorAll(".draw-row button").forEach(btn => btn.addEventListener("click", () => {
  if (drawing) { const same = drawing === btn.dataset.shape; map.pm.disableDraw(); if (same) return; }
  map.pm.enableDraw(btn.dataset.shape);
}));
map.on("pm:drawstart", e => {
  drawing = e.shape;
  document.querySelectorAll(".draw-row button").forEach(b => b.classList.toggle("active", b.dataset.shape === e.shape));
  setStatus(DRAW_HINTS[e.shape] + " Esc cancels.");
});
map.on("pm:drawend", () => {
  drawing = null;
  document.querySelectorAll(".draw-row button").forEach(b => b.classList.remove("active"));
});
document.addEventListener("keydown", e => { if (e.key === "Escape" && drawing) map.pm.disableDraw(); });
map.on("pm:create", e => useDrawnShape(e.layer, e.shape));

// circle -> polygon (64 corners), everything else as is
function shapeGeometry(layer) {
  if (!(layer instanceof L.Circle)) return layer.toGeoJSON().geometry;
  const c = layer.getLatLng(), d = layer.getRadius() / 6371008.8, rad = Math.PI / 180;
  const la1 = c.lat * rad, lo1 = c.lng * rad, pts = [];
  for (let i = 0; i < 64; i++) {
    const b = 2 * Math.PI * i / 64;
    const la2 = Math.asin(Math.sin(la1) * Math.cos(d) + Math.cos(la1) * Math.sin(d) * Math.cos(b));
    const lo2 = lo1 + Math.atan2(Math.sin(b) * Math.sin(d) * Math.cos(la1), Math.cos(d) - Math.sin(la1) * Math.sin(la2));
    pts.push([lo2 / rad, la2 / rad]);
  }
  return { type: "Polygon", coordinates: [[...pts, pts[0]]] };
}

async function drawnAreaInfo(layer, shape) {
  const geometry = shapeGeometry(layer);
  const res = await fetch(`${API}/areas/info`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ geometry }) });
  const info = await res.json();
  if (!res.ok) throw new Error(info.detail || res.statusText);
  return { ...info, geometry, drawn: true, shape, name: `Drawn ${shape.toLowerCase()}`,
           radius_m: layer instanceof L.Circle ? layer.getRadius() : null };
}

async function useDrawnShape(layer, shape, fit = false) {
  try {
    setArea(await drawnAreaInfo(layer, shape), layer, fit);
  } catch (e) {
    map.removeLayer(layer);
    setStatus("Error: " + e.message, true);
    return;
  }
  let t;
  layer.on("pm:edit", () => {   // reshaped: update info and results
    clearTimeout(t);
    t = setTimeout(async () => {
      if (drawnLayer !== layer) return;
      try {
        area = await drawnAreaInfo(layer, shape);
        drawMask(); renderAreaInfo(); saveArea();
        if ($("q").value.trim()) search();
      } catch (e) { setStatus("Error: " + e.message, true); }
    }, 400);
  });
}

// restore the area from the last session
(function restoreArea() {
  const saved = store.get("area", null);
  if (!saved) return;
  if (saved.drawn) {
    const style = { ...AREA_STYLE, fillOpacity: 0.04 };
    const layer = saved.circle
      ? L.circle([saved.circle.lat, saved.circle.lng], { ...style, radius: saved.circle.r })
      : L.polygon(saved.geometry.coordinates[0].slice(0, -1).map(([x, y]) => [y, x]), style);
    useDrawnShape(layer.addTo(map), saved.shape || "Polygon");
  } else if (saved.osm_type) {
    loadOsmArea(saved.osm_type, saved.osm_id, false).catch(() => store.set("area", null));
  }
})();

// ---------- suggestions ----------
let TAGS = [];          // [{key, value, count}]
let shown = [];         // currently listed suggestions
let active = -1;        // keyboard-highlighted index

fetch(`${API}/tags`).then(r => r.json()).then(t => {
  TAGS = t;
  if (document.activeElement === $("q")) renderSuggest();
}).catch(() => {});

// the part of the input being typed: text after the last "&"
const currentPart = () => {
  const v = $("q").value, i = v.lastIndexOf("&");
  return { prefix: i < 0 ? "" : v.slice(0, i + 1) + " ", term: v.slice(i + 1).trim() };
};
const norm = s => s.toLowerCase().replace(/[\s_]+/g, "_");

function highlight(text, term) {
  if (!term) return esc(text);
  const i = norm(text).indexOf(term);
  if (i < 0) return esc(text);
  return esc(text.slice(0, i)) + "<mark>" + esc(text.slice(i, i + term.length)) + "</mark>"
    + esc(text.slice(i + term.length));
}

function renderSuggest() {
  const { term } = currentPart();
  const t = norm(term);
  if (t) {
    // rank: value starts with term > tag starts with term > value contains > tag contains
    shown = TAGS.map(x => {
      const v = norm(x.value), full = norm(`${x.key}=${x.value}`);
      const s = v.startsWith(t) ? 0 : full.startsWith(t) ? 1 : v.includes(t) ? 2 : full.includes(t) ? 3 : -1;
      return [s, x];
    }).filter(([s]) => s >= 0).sort((a, b) => a[0] - b[0] || b[1].count - a[1].count)
      .slice(0, 50).map(([, x]) => x);
  } else {
    shown = TAGS.slice(0, 50);
  }
  // "Population" is not a tag: it is offered as a statistic about the current area
  const POP_WORDS = ["population", "bevolkerung", "bevölkerung", "einwohner", "census", "zensus", "density", "dichte"];
  if (!currentPart().prefix && (!t || POP_WORDS.some(w => w.startsWith(t) || (t.length >= 3 && w.includes(t))))) {
    shown = [{ special: "population" }, ...shown];
  }
  active = -1;

  const box = $("suggest");
  if (!TAGS.length) {
    box.innerHTML = `<div class="sg-empty">Loading tags…</div>`;
  } else if (!shown.length) {
    box.innerHTML = `<div class="sg-empty">No known tag matches “${esc(term)}”. Press Enter to search anyway.</div>`;
  } else {
    const tKey = t.split("=")[0], tVal = t.includes("=") ? t.split("=")[1] : t;
    box.innerHTML = `<div class="sg-head">${t ? "Matching tags" : "Popular tags"}</div>` +
      shown.map((x, i) => x.special ? `
        <div class="sg special" role="option" data-i="${i}">
          <span class="ico"></span><span><span class="val">Population</span> <span class="key">· density, age, housing</span></span>
        </div>` : `
        <div class="sg" role="option" data-i="${i}">
          <span class="dot" style="background:${color(`${x.key}=${x.value}`)}"></span>
          <span><span class="key">${highlight(x.key, t.includes("=") ? tKey : "")}=</span><span class="val">${highlight(x.value, tVal)}</span></span>
          <span class="count">${x.count.toLocaleString()}</span>
        </div>`).join("");
  }
  openSuggest();
}

function setActive(i) {
  const items = $("suggest").querySelectorAll(".sg");
  if (!items.length) return;
  active = (i + items.length) % items.length;
  items.forEach((el, j) => el.classList.toggle("active", j === active));
  items[active].scrollIntoView({ block: "nearest" });
}

function choose(i) {
  const x = shown[i];
  if (!x) return;
  if (x.special) { $("q").value = "population"; updateClear(); search(); return; }
  $("q").value = currentPart().prefix + `${x.key}=${x.value}`;
  updateClear();
  search();
}

function openSuggest() { $("sidebar").classList.add("open"); $("q").setAttribute("aria-expanded", "true"); }
function closeSuggest() { $("sidebar").classList.remove("open"); $("q").setAttribute("aria-expanded", "false"); }
const updateClear = () => $("clear").style.display = $("q").value ? "block" : "none";

$("q").addEventListener("focus", () => { closeLibrary(); renderSuggest(); });
$("q").addEventListener("input", () => { updateClear(); renderSuggest(); });
$("q").addEventListener("blur", () => setTimeout(closeSuggest, 120));
$("q").addEventListener("keydown", e => {
  const open = $("sidebar").classList.contains("open");
  if (e.key === "ArrowDown") { e.preventDefault(); open ? setActive(active + 1) : renderSuggest(); }
  else if (e.key === "ArrowUp") { e.preventDefault(); if (open) setActive(active - 1); }
  else if (e.key === "Escape") { closeSuggest(); }
  else if ((e.key === "Enter" || e.key === "Tab") && open && active >= 0) { e.preventDefault(); choose(active); }
});
// mousedown (not click) so the input doesn't lose focus first
$("suggest").addEventListener("mousedown", e => {
  e.preventDefault();
  const el = e.target.closest(".sg");
  if (el) choose(+el.dataset.i);
});

$("clear").addEventListener("click", () => {
  $("q").value = ""; updateClear(); clearResults();
  setStatus("Type a tag or pick one from the list.");
  $("q").focus();
});
$("form").addEventListener("submit", e => {
  e.preventDefault();
  if (controller) { controller.abort(); return; }   // button shows "Cancel" while searching
  search();
});
