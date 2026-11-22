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
  setStatus(`in ${areaName()}`);
  const row = (label, value, hint = "") => `<div class="row"${hint ? ` title="${esc(hint)}"` : ""}><span>${label}</span><b>${value}</b></div>`;
  $("res-list").innerHTML = `<div class="pop-stats">
    <h4>Ratio</h4>
    ${row("Per km²", num(d.area_km2 ? lane / d.area_km2 : null, 1, " lane-km"), `Area ${fmt(d.area_km2, 2)} km²`)}
    ${row("Per person", d.population ? num(lane * 1000 / d.population, 1, " m") : dash, d.population ? `${d.population.toLocaleString(NUM)} inhabitants` : "No population data here")}
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
  const a = e.target.closest("a[data-rd]");
  if (!a || !roadData) return;
  roadSel.clear();
  for (const [id] of ROAD_TYPES) if (a.dataset.rd === "all" || a.dataset.rd === "cars" && !ROAD_OFF.includes(id)) roadSel.add(id);
  roadsChanged();
});
function roadsChanged() {
  store.set("roadTypes3", [...roadSel]);
  showRoads();
  splitRedraw();      // the map views use the same types
}

$("road-btn").addEventListener("click", () => {       // Roads tool = the roads statistics in the sidebar
  closeOtherTools();
  $("q").value = "roads"; search();
});
