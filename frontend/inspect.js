/* Point info tool. Plain script: the files share one scope and load in this order (see index.html). */
// ---------- "Point info" tool: all OSM objects at a clicked point, shown in the sidebar ----------
const inspectHl = L.layerGroup().addTo(map);     // outline of the entry under the mouse
const inspectMark = L.layerGroup().addTo(map);   // the clicked point and the search radius
let inspecting = false, inspectSeq = 0, inspectData = null;
const INSP_HELP = '<div class="empty">Click anywhere on the map. All areas (land use, districts, buildings …) containing that point and the roads and places close to it are listed here.</div>';

function setInspect(on) {
  if (on === inspecting) return;
  if (on) closeOtherTools("inspect");
  inspecting = on;
  $("sidebar").classList.toggle("insp-open", on);
  $("insp-btn").classList.toggle("insp-on", on);
  $("insp-btn").setAttribute("aria-expanded", String(on));
  map.getContainer().classList.toggle("inspecting", on);
  if (on) { $("insp-body").innerHTML = INSP_HELP; $("insp-sub").textContent = "Click the map"; }
  else { inspectSeq++; inspectHl.clearLayers(); inspectMark.clearLayers(); inspectData = null; }
}
$("insp-btn").addEventListener("click", () => setInspect(!inspecting));
$("insp-close").addEventListener("click", () => setInspect(false));
map.on("pm:drawstart", () => setInspect(false));

const fmtArea = m2 => m2 >= 1e6 ? (m2 / 1e6).toLocaleString(undefined, { maximumFractionDigits: 1 }) + " km²"
  : m2 >= 1e4 ? (m2 / 1e4).toLocaleString(undefined, { maximumFractionDigits: 1 }) + " ha" : m2.toLocaleString() + " m²";

function inspectItem(it, i) {
  const rows = Object.entries(it.tags).sort().map(([k, v]) => {
    const val = /^https?:\/\//.test(v) ? `<a href="${esc(v)}" target="_blank">${esc(v)}</a>` : esc(v);
    return `<tr><td>${esc(k)}</td><td>${val}</td></tr>`;
  }).join("");
  const note = it.kind === "area" ? (it.area_m2 ? fmtArea(it.area_m2) : "")
    : it.distance_m != null ? (it.distance_m < 1 ? "here" : Math.round(it.distance_m) + " m") : "";
  return `<details data-i="${i}"><summary><span class="t">${esc(it.name ?? it.title)}</span>
    <span class="n">${it.name ? esc(it.title) + (note ? " · " : "") : ""}${esc(note)}</span></summary>
    <div class="tags"><table>${rows}</table>
    <a href="https://www.openstreetmap.org/${it.osm_type}/${it.osm_id}" target="_blank">${it.osm_type} ${it.osm_id} on OpenStreetMap</a></div></details>`;
}

// the population grid cell under the click: its outline on the map, numbers in the list
const STAT_ROWS = [["avg_age", "Average age", " years"], ["pct_under18", "Under 18", " %"], ["pct_over65", "65 and older", " %"],
  ["pct_foreign", "Foreign nationals", " %"], ["hh_size", "Household size", ""], ["living_space", "Living space / person", " m²"],
  ["rent_sqm", "Net rent", " €/m²"], ["pct_owner", "Owner-occupied", " %"], ["pct_vacant", "Vacant", " %"]];
function inspectBuilt(b) {
  if (!b) return "";
  return `<h4>Built-up · 1 km square</h4><div class="tags"><table>
    <tr><td>Per person</td><td><b>${b.per_person == null ? "–" : b.per_person.toLocaleString()}</b> m²</td></tr>
    <tr><td>Share</td><td><b>${b.share}</b> %</td></tr></table><small>GHS-BUILT-S 2020</small></div>`;
}
function inspectPopulation(p) {
  if (!p) return "";
  const rows = STAT_ROWS.filter(([k]) => p.stats[k] != null).map(([k, label, unit]) => `<tr><td>${label}</td><td>${p.stats[k]}${unit}</td></tr>`).join("");
  return `<h4>Population · ${p.cell_m} m cell</h4><div class="tags"><table>
    <tr><td>Inhabitants</td><td><b>${p.population.toLocaleString()}</b></td></tr>
    <tr><td>Density</td><td><b>${p.density.toLocaleString()}</b> /km²</td></tr>${rows}</table>
    <small>${esc(p.dataset)}</small></div>`;
}

function inspectHtml(d) {
  const group = (title, kinds) => {
    const list = d.items.map((it, i) => kinds.includes(it.kind) ? inspectItem(it, i) : "").join("");
    return list ? `<h4>${title}</h4>${list}` : "";
  };
  return inspectPopulation(d.population) + inspectBuilt(d.built) + group("At this point", ["area"]) + group(`Nearby (within ${d.radius_m} m)`, ["line", "point"])
    || '<div class="empty">No tagged objects here.</div>';
}

function showInspectHl(it) {
  inspectHl.clearLayers();
  if (!it?.geometry) return;
  L.geoJSON(it.geometry, { interactive: false, style: { color: "#e11d48", weight: 3, fillOpacity: .12 },
    pointToLayer: (_, ll) => L.circleMarker(ll, { radius: 9, color: "#e11d48", weight: 3, fillOpacity: .15, interactive: false }),
  }).addTo(inspectHl);
}
const itemAt = ev => inspectData?.items[+ev.target.closest("details")?.dataset.i];
$("insp-body").addEventListener("mouseover", ev => { if (ev.target.closest("summary")) showInspectHl(itemAt(ev)); });
$("insp-body").addEventListener("mouseout", () => inspectHl.clearLayers());
$("insp-body").addEventListener("click", ev => { if (ev.target.closest("summary")) showInspectHl(itemAt(ev)); });

// the clicked point: a pin-like ring plus the radius that was searched
function markInspectPoint(ll, radius) {
  inspectMark.clearLayers();
  if (radius) L.circle(ll, { radius, color: "#e11d48", weight: 1, dashArray: "4 4", fillOpacity: .06, interactive: false }).addTo(inspectMark);
  L.circleMarker(ll, { radius: 9, color: "#fff", weight: 5, fill: false, interactive: false }).addTo(inspectMark);
  L.circleMarker(ll, { radius: 9, color: "#e11d48", weight: 3, fill: false, interactive: false }).addTo(inspectMark);
  L.circleMarker(ll, { radius: 2.5, color: "#e11d48", weight: 1, fillColor: "#e11d48", fillOpacity: 1, interactive: false }).addTo(inspectMark);
}

map.on("click", async e => {
  if (!inspecting || drawing) return;
  const seq = ++inspectSeq, { lat, lng } = e.latlng;
  inspectHl.clearLayers(); inspectData = null;
  markInspectPoint(e.latlng);
  $("insp-sub").textContent = `${lat.toFixed(5)}, ${lng.toFixed(5)}`;
  $("insp-body").innerHTML = '<div class="empty">Looking up…</div>';
  try {
    const r = await fetch(`${API}/inspect?` + new URLSearchParams({ lat, lon: lng, zoom: map.getZoom(), source }));
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `HTTP ${r.status}`);
    const d = await r.json();
    if (seq !== inspectSeq) return;   // a newer click
    inspectData = d;
    markInspectPoint(e.latlng, d.radius_m);
    if (d.population?.geometry) L.geoJSON(d.population.geometry, { interactive: false, style: { color: "#e11d48", weight: 1.5, fillOpacity: .08 } }).addTo(inspectMark);
    $("insp-sub").textContent = `${lat.toFixed(5)}, ${lng.toFixed(5)} · ${d.items.length} object${d.items.length === 1 ? "" : "s"} · ${d.source === "online" ? "online" : "local data"}`;
    $("insp-body").innerHTML = inspectHtml(d);
    $("insp-body").scrollTop = 0;
  } catch (err) {
    if (seq === inspectSeq) $("insp-body").innerHTML = `<div class="empty error">${esc(err.message)}</div>`;
  }
});
