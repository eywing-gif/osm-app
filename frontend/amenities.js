/* Amenities: what a place offers (10 groups, backend/amenity_catalog.py) and how it scores (backend/amenities.py).
   Search "amenities" (button in the toolbar): the places of the area (Search in …) and the rating of its inhabitants;
   click the map: the full rating of that place. Map: Search in → split by (score per square from 100 m, or places).
   Plain script: the files share one scope and load in this order (see index.html). */
const isAmenityQuery = q => /^(amenit(y|ies)|services?|angebot|versorgung|einrichtungen|dienstleistungen)$/i.test(q.trim());
let amenCat = null, amenArea = null, amenPoint = null, amenPointData = null;
const amenSel = new Set(store.get("amenGroups", null) || ["daily", "periodic", "aperiodic"]);   // start: shopping
const amenMark = L.layerGroup().addTo(map), amenDots = L.layerGroup().addTo(map);
const AMEN_DOTS_ZOOM = 15;
const BANDS = [[90, "Excellent"], [70, "Very good"], [50, "Good"], [25, "Limited"], [0, "Poor"]];   // Walk Score bands
const bandOf = s => BANDS.find(([b]) => s >= b)[1];
const gradeColor = g => rampColor(SCORE_SCALE, Math.min(1, Math.max(0, (6 - g) / 5)));

async function amenCatalog() {
  if (!amenCat) amenCat = await (await fetch(`${API}/amenities/catalog`)).json();
  return amenCat;
}
const amenActive = () => isAmenityQuery($("q").value) && !/\b(trn|insp|lib|lu)-open\b/.test($("sidebar").className);

async function amenitySearch() {
  const ctl = controller = new AbortController();
  setStatus("Calculating…");
  $("go").textContent = "Cancel";
  try {
    await amenCatalog();
    if (!amenCat.prepared) throw new Error("Amenities not prepared – Settings → Map → Prepare analyses");
    const res = amenPoint
      ? await fetch(`${API}/amenities/at?${new URLSearchParams({ lon: amenPoint.lng, lat: amenPoint.lat })}`, { signal: ctl.signal })
      : await fetch(`${API}/amenities/stats`, { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ geometry: populationGeometry() }), signal: ctl.signal });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || res.statusText);
    if (amenPoint) { amenPointData = data; showAmenityPoint(); } else { amenArea = data; showAmenityArea(); }
    amenLoadDots();
  } catch (e) {
    if (e.name === "AbortError") { if (ctl === controller) setStatus("Cancelled."); return; }
    setStatus("Error: " + e.message, true);
  } finally {
    if (ctl === controller) { controller = null; $("go").textContent = "Search"; }
  }
}

const row = (label, value, hint = "") => `<div class="row"${hint ? ` title="${esc(hint)}"` : ""}><span>${label}</span><b>${value}</b></div>`;
const gradeBadge = g => `<span class="am-grade" style="background:${gradeColor(g)}">${g.toFixed(1)}</span>`;
const METHODS = `<details class="am-methods"><summary>Methods</summary>
  <p><b>Amenity score</b> (0–100) – method of Walk Score: per group the nearest places count, fully within 400 m (5 min),
  less with distance, nothing beyond 2.4 km (30 min). Weights: daily needs 3, food & drink 3, shopping 2, errands 1, parks 1,
  education 2 (schools + libraries), culture 1 (Walk Score); health 3 (BBSR local supply), sport 1, large purchases 0.5.
  <b>Grade</b> 1–6 from the Walk Score bands: 90+ → 1, 70 → 2, 50 → 3, 25 → 4, 10 → 5, 0 → 6.</p>
  <p><b>15-min groups</b> – how many of the 10 groups have a place within 15 minutes on foot (15-minute city, Moreno et al. 2021).</p>
  <p><b>Proximity time</b> – mean over the groups of the walking time to the 5 nearest places, at most 60 min (Bruno et al. 2024: 20 nearest, routed).</p>
  <p><b>Diversity</b> – evenness of the groups within 10 minutes (Shannon / Pielou, 0–1).</p>
  <p><b>Local supply</b> – BBSR: supermarket, pharmacy, doctor, school, transit stop within 1 km (straight line).</p>
  <p>Walking: 4.5 km/h, ways 1.2 × the straight line. Places: OpenStreetMap.</p></details>`;

// ---- the methods: five ways to measure the same – how much a place offers on foot. One card each, with its map.
const mapBtn = m => `<button type="button" class="am-map" data-map="${m}" title="Show this method on the map (grid)">Map</button>`;
const card = (m, title, source, body) => `<section class="am-card"><header><b>${title}</b>${mapBtn(m)}</header>
  <div class="am-src">${source}</div>${body}</section>`;
function amenMap(m) {           // the score map of a method: grid view, that method
  if (area) clearArea(false);
  if (splitView !== "grid") setSplitView("grid");
  setSplitMode(m);
}
const INTRO = `<div class="note" style="margin:8px 0 4px">Five methods, one question: how much can be reached on foot? They weigh it differently.</div>`;

// ---- an area: places per group (the chosen groups count for the map) and the rating of its inhabitants
function showAmenityArea() {
  const d = amenArea, km2 = d.area_km2;
  layer.clearLayers(); found = []; updateMarkerHint();
  const sel = d.groups.filter(g => amenSel.has(g.id)).reduce((a, g) => a + g.n, 0);
  $("res-count").textContent = sel.toLocaleString();
  $("res-label").textContent = "places";
  setStatus(`in ${areaName()} · click the map for the rating of a place`);
  const p = d.people;
  $("res-list").innerHTML = `<div class="pop-stats">
    ${p ? `<h4>Rating of the inhabitants <small>(${d.population.toLocaleString()}, mean over where they live)</small></h4>${INTRO}
      ${card("score", "Walk Score method", "Walk Score · weighted groups, distance decay",
        `<div class="am-head">${gradeBadge(p.grade)}<div><b>${bandOf(p.score)}</b><small>score ${p.score} of 100</small></div></div>
         ${row("Score 70 or more", p.good + " % of the people", "very good or excellent")}`)}
      ${card("within15", "15-minute city", "Moreno et al. · groups within 15 min on foot", row("Groups reached", fmt(p.within15, 1) + " of 10"))}
      ${card("proximity", "Proximity time", "Bruno et al. 2024 · minutes to the nearest places", row("Mean time", fmt(p.proximity_min, 1) + " min"))}
      ${card("local", "Local supply", "BBSR · within 1 km (straight line)",
        Object.entries(p.local_pct).map(([k, v]) => row(k, v + " % of the people")).join(""))}` : ""}
    <h4>Places <span class="rd-all"><a data-am="shop">shopping</a> <a data-am="all">all</a> <a data-am="none">none</a></span></h4>
    ${d.groups.map(g => `<details class="am-group"><summary><label class="row rd-type${amenSel.has(g.id) ? "" : " off"}">
        <span><input type="checkbox" data-am="${g.id}"${amenSel.has(g.id) ? " checked" : ""}> <i class="am-dot" style="background:${amenCat.groups.find(c => c.id === g.id).color}"></i>${g.name}</span>
        <b>${g.n.toLocaleString()}<small> · ${km2 ? fmt(g.n / km2, 1) : dash}/km²${d.population ? " · " + fmt(g.n * 1000 / d.population, 1) + "/1,000" : ""}</small></b></label></summary>
        ${g.subs.map(s => row(s.name, s.n.toLocaleString())).join("") || '<div class="note">none</div>'}</details>`).join("")}
    <div class="note">Places: OpenStreetMap. The ticked groups count for the map (Search in → split by …).
      Rating: score of every ${p ? sizeName(p.square_m) : ""} square with inhabitants, weighted by the people.</div>
    ${METHODS}</div>`;
}

// ---- one place: the full rating
function showAmenityPoint() {
  const d = amenPointData;
  layer.clearLayers(); found = []; updateMarkerHint();
  $("res-count").textContent = d.grade.toFixed(1);
  $("res-label").textContent = "grade";
  setStatus('Rating of the clicked place · <a href="#" id="am-back">back to the area</a>', false, true);
  const maxW = Math.max(...d.groups.map(g => g.weight));
  $("res-list").innerHTML = `<div class="pop-stats">
    ${INTRO}
    ${card("score", "Walk Score method", "Walk Score · weighted groups, full points within 5 min, none after 30 min",
      `<div class="am-head">${gradeBadge(d.grade)}<div><b>${bandOf(d.score)}</b><small>score ${d.score} of 100</small></div></div>
       ${d.groups.map(g => `<div class="am-bar" title="${esc(g.name)}: ${fmt(g.points, 2)} of ${g.weight} points">
         <span>${g.name}</span><i><b style="width:${g.points / maxW * 100}%;background:${amenCat.groups.find(c => c.id === g.id).color}"></b>
         <em style="width:${g.weight / maxW * 100}%"></em></i><small>${fmt(g.points, 1)}/${g.weight}</small></div>`).join("")}`)}
    ${card("within15", "15-minute city", "Moreno et al. · which groups are within 15 min on foot",
      `<div class="am-big">${d.within15} <small>of ${d.groups_total} groups</small></div>
       <div class="am-chips">${d.groups.map(g => `<span class="${g.within15 ? "ok" : "no"}">${g.within15 ? "✓" : "✗"} ${g.name}</span>`).join("")}</div>`)}
    ${card("proximity", "Proximity time", "Bruno et al. 2024 · walking time to the 5 nearest places, mean of the groups",
      `<div class="am-big">${fmt(d.proximity_min, 1)} <small>min</small></div>`)}
    ${card("diversity", "Variety", "Shannon / Pielou · how evenly the groups are mixed within 10 min",
      `<div class="am-big">${Math.round(d.diversity * 100)} <small>% (100 = all groups equally)</small></div>`)}
    ${card("local", "Local supply", "BBSR · within 1 km (straight line)",
      d.local.map(l => row(`${l.ok ? "✓" : "✗"} ${l.name}`, l.m == null ? "far" : l.m.toLocaleString() + " m")).join(""))}
    <h4>Places by group <small>(within 5 · 10 · 15 min)</small></h4>
    ${d.groups.map(g => `<details class="am-group"><summary><div class="row"><span><i class="am-dot" style="background:${amenCat.groups.find(c => c.id === g.id).color}"></i>${g.name}</span>
        <b>${g.min5} · ${g.min10} · ${g.min15}</b></div></summary>
        ${g.nearest.map(n => row(esc(n.name || n.type), `${fmt(n.walk_min, 1)} min`, n.type)).join("") || '<div class="note">nothing within 60 min</div>'}
        ${g.subs.length ? `<div class="note">within 10 min: ${g.subs.map(s => `${esc(s.name)} ${s.n}`).join(" · ")}</div>` : ""}</details>`).join("")}
    ${METHODS}</div>`;
  $("am-back").addEventListener("click", e => { e.preventDefault(); amenSetPoint(null); });
}

function amenSetPoint(ll, run = true) {
  amenPoint = ll;
  amenMark.clearLayers();
  if (ll) {
    L.circle(ll, { radius: 15 * 75 / 1.2, color: "#2563eb", weight: 1, dashArray: "4 4", fillOpacity: .04, interactive: false }).addTo(amenMark);
    L.circleMarker(ll, { radius: 8, color: "#fff", weight: 5, fill: false, interactive: false }).addTo(amenMark);
    L.circleMarker(ll, { radius: 8, color: "#2563eb", weight: 3, fill: false, interactive: false }).addTo(amenMark);
  }
  if (run) search();
}
// click the map (amenities shown, no administrative split): the rating of that place
map.on("click", e => {
  if (!amenActive() || SPLIT_LEVELS[splitView]) return;
  amenSetPoint(e.latlng);
});

// groups: checkboxes in the area list (they count for the map)
$("res-list").addEventListener("change", e => {
  const id = e.target.dataset?.am;
  if (!id || !amenArea) return;
  e.target.checked ? amenSel.add(id) : amenSel.delete(id);
  amenGroupsChanged();
});
$("res-list").addEventListener("click", e => {
  const mb = e.target.closest("[data-map]");
  if (mb) return amenMap(mb.dataset.map);
  const a = e.target.closest("a[data-am]");
  if (!a || !amenArea) return;
  e.preventDefault();
  amenSel.clear();
  for (const g of amenCat.groups) if (a.dataset.am === "all" || a.dataset.am === "shop" && ["daily", "periodic", "aperiodic"].includes(g.id)) amenSel.add(g.id);
  amenGroupsChanged();
});
function amenGroupsChanged() {
  store.set("amenGroups", [...amenSel]);
  showAmenityArea();
  amenLoadDots();
  if (splitSubject === "amenities") splitRedraw(true);
}

// the places of the chosen groups as dots (zoomed in)
let dotsCtl = null;
async function amenLoadDots() {
  amenDots.clearLayers();
  if (!amenActive() || map.getZoom() < AMEN_DOTS_ZOOM || !amenCat) return;
  dotsCtl?.abort();
  const ctl = dotsCtl = new AbortController(), b = map.getBounds();
  const sw = L.CRS.EPSG3857.project(b.getSouthWest()), ne = L.CRS.EPSG3857.project(b.getNorthEast());
  try {
    const pts = await (await fetch(`${API}/amenities/points?${new URLSearchParams({ minx: sw.x, miny: sw.y, maxx: ne.x, maxy: ne.y, groups: [...amenSel].join(",") })}`, { signal: ctl.signal })).json();
    amenDots.clearLayers();
    for (const [lon, lat, g, type, name] of pts)
      L.circleMarker([lat, lon], { radius: 4, weight: 1, color: "#fff", fillColor: amenCat.groups[g].color, fillOpacity: .95 })
        .bindTooltip(`${esc(name || type)}<br><small>${esc(type)}</small>`).addTo(amenDots);
  } catch { /* aborted or offline */ }
}
map.on("moveend", () => { if (amenActive()) amenLoadDots(); else amenDots.clearLayers(); });

$("amen-btn").addEventListener("click", () => {       // Amenities tool = the amenities statistics in the sidebar
  closeOtherTools();
  amenSetPoint(null, false);
  $("q").value = "amenities"; search();
});
