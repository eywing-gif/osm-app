/* Two tools on the same places (10 groups, backend/amenity_catalog.py; scores: backend/amenities.py):
   Amenities ("amenities")      what is right at a place: attractiveness per field (daily needs, health, dining …) within
                                400 m, and the places per group. Map: an attractiveness field, or the places counted.
   Walkability ("walkability")  how well a place is supplied on foot: Walk Score method, 15-minute city, proximity time,
                                variety, BBSR local supply. Map: one method per square.
   Both: the area (Search in …) as the mean of its inhabitants; click the map for one place.
   Plain script: the files share one scope and load in this order (see index.html). */
const isAmenityQuery = q => /^(amenit(y|ies)|services?|angebot|versorgung|einrichtungen|dienstleistungen|attraktivität)$/i.test(q.trim());
const isWalkQuery = q => /^(walkability|walk ?score|versorgungslage)$/i.test(q.trim());
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
const toolOpen = () => !/\b(trn|insp|lib|lu)-open\b/.test($("sidebar").className);
const walkActive = () => isWalkQuery($("q").value) && toolOpen();
const amenActive = () => (isAmenityQuery($("q").value) || isWalkQuery($("q").value)) && toolOpen();   // either tool

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
    if (amenPoint) amenPointData = data; else amenArea = data;
    amenShow();
    amenLoadDots();
  } catch (e) {
    if (e.name === "AbortError") { if (ctl === controller) setStatus("Cancelled."); return; }
    setStatus("Error: " + e.message, true);
  } finally {
    if (ctl === controller) { controller = null; $("go").textContent = "Search"; }
  }
}
function amenShow() {
  layer.clearLayers(); found = []; updateMarkerHint();
  const walk = isWalkQuery($("q").value);
  if (amenPoint) walk ? showWalkPoint() : showAmenityPoint();
  else walk ? showWalkArea() : showAmenityArea();
  $("am-back")?.addEventListener("click", e => { e.preventDefault(); amenSetPoint(null); });
}

const row = (label, value, hint = "") => `<div class="row"${hint ? ` title="${esc(hint)}"` : ""}><span>${label}</span><b>${value}</b></div>`;
const gradeBadge = g => `<span class="am-grade" style="background:${gradeColor(g)}">${g.toLocaleString(NUM, { minimumFractionDigits: 1, maximumFractionDigits: 1 })}</span>`;
const WALK_METHODS = `<details class="am-methods"><summary>Methods</summary>
  <p><b>Walk Score method</b> (0–100) – per group the nearest places count, fully within 400 m (5 min),
  less with distance, nothing beyond 2.4 km (30 min). Weights: daily needs 3, food & drink 3, shopping 2, errands 1, parks 1,
  education 2 (schools + libraries), culture 1 (Walk Score); health 3 (BBSR local supply), sport 1, large purchases 0.5.
  <b>Grade</b> 1–6 from the Walk Score bands: 90+ → 1, 70 → 2, 50 → 3, 25 → 4, 10 → 5, 0 → 6.</p>
  <p><b>15-minute city</b> – how many of the 10 groups have a place within 15 minutes on foot (Moreno et al. 2021).</p>
  <p><b>Proximity time</b> – mean over the groups of the walking time to the 5 nearest places, at most 60 min (Bruno et al. 2024: 20 nearest, routed).</p>
  <p><b>Variety</b> – evenness of the groups within 10 minutes (Shannon / Pielou, 0–1).</p>
  <p><b>Local supply</b> – BBSR: supermarket, pharmacy, doctor, school, transit stop within 1 km (straight line).</p>
  <p>Walking: 4.5 km/h, ways 1.2 × the straight line. Places: OpenStreetMap.</p></details>`;
const ATTRACT_METHODS = `<details class="am-methods"><summary>Methods</summary>
  <p><b>Attractiveness</b> – what is right at the place: the places within 400 m (straight line), no distance decay.
  Fields after the 15-minute city (Moreno et al. 2021); third places after Oldenburg (cafés, pubs, libraries, community
  centres …). Fields overlap: a café is dining and a third place.</p>
  <p><b>Weight</b> – how often people go there, visits per month (supermarket 10, bakery 6, clothes 1.5, electronics 0.5,
  furniture 0.3; German retail: daily / periodic / episodic needs).</p>
  <p><b>Diminishing returns</b> – the 2nd place of a type adds r × the weight, the 3rd r² … (geometric, like Walk Score's
  falling shares for the 1st, 2nd … restaurant). r is low where one is enough (supermarket 0.5, pharmacy 0.3, school 0.4)
  and high where choice is the value (restaurants 0.92, clothes 0.9: "love of variety", Schiff 2015). Green spaces count
  once per type, when their edge is within 400 m.</p>
  <p><b>Index</b> 0–100 – the points as a share of the most the field can give (every type saturated). Hover a field for its weights.</p>
  <p>Places: OpenStreetMap.</p></details>`;

// ---- one card per method (or field list), each with its map
const mapBtn = m => `<button type="button" class="am-map" data-map="${m}" title="Show on the map (grid)">Map</button>`;
const card = (m, title, source, body) => `<section class="am-card"><header><b>${title}</b>${m ? mapBtn(m) : ""}</header>
  <div class="am-src">${source}</div>${body}</section>`;
function amenMap(m) {           // the map of a method / field: grid view
  if (area) clearArea(false);
  if (splitView !== "grid") setSplitView("grid");
  setSplitMode(m);
}
// the weights of an attractiveness field, for tooltips: "Supermarket 10 (next × 0.5) · …"
function attractHint(id) {
  const fields = amenCat.attract.filter(a => id === "a_supply" ? ["a_daily", "a_periodic", "a_episodic"].includes(a.id) : a.id === id);
  return "Weights: " + fields.flatMap(a => Object.entries(a.parts).flatMap(([gid, subs]) => {
    const g = amenCat.groups.find(c => c.id === gid);
    return Object.entries(subs).map(([sid, [w, r]]) => `${g.subs.find(x => x.id === sid).name} ${w}` + (gid === "green" ? "" : ` (next × ${r})`));
  })).join(" · ");
}
// attractiveness fields: bar 0–100 + map button
const attractList = list => list.map(a => `<div class="am-att" title="${esc(attractHint(a.id))}">
    <span>${a.id === "a_supply" ? "<b>" + a.name + "</b>" : a.name}</span>
    <i><b style="width:${a.value}%;background:${rampColor(ATTRACT_SCALE, Math.max(0.25, a.value / 100))}"></b></i><small>${a.value}</small>${mapBtn(a.id)}</div>`).join("");
const groupDot = id => `<i class="am-dot" style="background:${amenCat.groups.find(c => c.id === id).color}"></i>`;

// ---- Amenities, an area: attractiveness (mean of the inhabitants) and places per group (the ticked ones count for the map)
function showAmenityArea() {
  const d = amenArea, km2 = d.area_km2;
  const sel = d.groups.filter(g => amenSel.has(g.id)).reduce((a, g) => a + g.n, 0);
  $("res-count").textContent = sel.toLocaleString(NUM);
  $("res-label").textContent = "places";
  setStatus(`in ${areaName()} · click the map for one place`);
  $("res-list").innerHTML = `<div class="pop-stats">
    ${d.attract ? card(null, "Attractiveness", `what is within 400 m · mean of the ${d.population.toLocaleString(NUM)} inhabitants · 0–100`, attractList(d.attract)) : ""}
    <h4>Places <span class="rd-all"><a data-am="shop">shopping</a> <a data-am="all">all</a> <a data-am="none">none</a></span></h4>
    ${d.groups.map(g => `<details class="am-group"><summary><label class="row rd-type${amenSel.has(g.id) ? "" : " off"}">
        <span><input type="checkbox" data-am="${g.id}"${amenSel.has(g.id) ? " checked" : ""}> ${groupDot(g.id)}${g.name}</span>
        <b>${g.n.toLocaleString(NUM)}<small> · ${km2 ? fmt(g.n / km2, 1) : dash}/km²${d.population ? " · " + fmt(g.n * 1000 / d.population, 1) + "/1.000" : ""}</small></b></label></summary>
        ${g.subs.map(s => row(s.name, s.n.toLocaleString(NUM))).join("") || '<div class="note">none</div>'}</details>`).join("")}
    <div class="note">Places: OpenStreetMap. The ticked groups count for the map (Map → Count places).</div>
    ${ATTRACT_METHODS}</div>`;
}

// ---- Amenities, one place: attractiveness and what is around it
function showAmenityPoint() {
  const d = amenPointData;
  $("res-count").textContent = d.attract[0].value;
  $("res-label").textContent = "retail index";
  setStatus('The clicked place · <a href="#" id="am-back">back to the area</a>', false, true);
  $("res-list").innerHTML = `<div class="pop-stats">
    ${card(null, "Attractiveness", `what is within ${d.attract_m} m · 0–100`, attractList(d.attract))}
    <h4>Places by group <small>(within 5 · 10 · 15 min)</small></h4>
    ${d.groups.map(g => `<details class="am-group"><summary><div class="row"><span>${groupDot(g.id)}${g.name}</span>
        <b>${g.min5} · ${g.min10} · ${g.min15}</b></div></summary>
        ${g.nearest.map(n => row(esc(n.name || n.type), `${fmt(n.walk_min, 1)} min`, n.type)).join("") || '<div class="note">nothing within 60 min</div>'}
        ${g.subs.length ? `<div class="note">within 10 min: ${g.subs.map(s => `${esc(s.name)} ${s.n}`).join(" · ")}</div>` : ""}</details>`).join("")}
    ${ATTRACT_METHODS}</div>`;
}

// ---- Walkability: five ways to measure how well a place is supplied on foot. One card each, with its map.
const INTRO = `<div class="note" style="margin:8px 0 4px">Five methods, one question: how much can be reached on foot? They weigh it differently.</div>`;
function showWalkArea() {
  const d = amenArea, p = d.people;
  $("res-count").textContent = p ? p.grade.toLocaleString(NUM, { minimumFractionDigits: 1, maximumFractionDigits: 1 }) : dash;
  $("res-label").textContent = "grade";
  setStatus(`in ${areaName()} · click the map for one place`);
  $("res-list").innerHTML = `<div class="pop-stats">
    ${p ? `<h4>Rating of the inhabitants <small>(${d.population.toLocaleString(NUM)}, mean over where they live)</small></h4>${INTRO}
      ${card("score", "Walk Score method", "Walk Score · weighted groups, distance decay",
        `<div class="am-head">${gradeBadge(p.grade)}<div><b>${bandOf(p.score)}</b><small>score ${p.score} of 100</small></div></div>
         ${row("Score 70 or more", p.good + " % of the people", "very good or excellent")}`)}
      ${card("within15", "15-minute city", "Moreno et al. · groups within 15 min on foot", row("Groups reached", fmt(p.within15, 1) + " of 10"))}
      ${card("proximity", "Proximity time", "Bruno et al. 2024 · minutes to the nearest places", row("Mean time", fmt(p.proximity_min, 1) + " min"))}
      ${card("local", "Local supply", "BBSR · within 1 km (straight line)",
        Object.entries(p.local_pct).map(([k, v]) => row(k, v + " % of the people")).join(""))}
      <div class="note">Score of every ${sizeName(p.square_m)} square with inhabitants, weighted by the people.</div>`
      : '<div class="note">No population data: the rating is the mean over where people live.</div>'}
    ${WALK_METHODS}</div>`;
}
function showWalkPoint() {
  const d = amenPointData;
  $("res-count").textContent = d.grade.toLocaleString(NUM, { minimumFractionDigits: 1, maximumFractionDigits: 1 });
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
      d.local.map(l => row(`${l.ok ? "✓" : "✗"} ${l.name}`, l.m == null ? "far" : l.m.toLocaleString(NUM) + " m")).join(""))}
    ${WALK_METHODS}</div>`;
}

function amenSetPoint(ll, run = true) {
  amenPoint = ll;
  amenMark.clearLayers();
  if (ll) {
    L.circle(ll, { radius: isWalkQuery($("q").value) ? 15 * 75 / 1.2 : 400, color: "#2563eb", weight: 1, dashArray: "4 4", fillOpacity: .04, interactive: false }).addTo(amenMark);
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
  amenShow();
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

for (const [btn, q] of [["amen-btn", "amenities"], ["walk-btn", "walkability"]])   // the two tools = their statistics in the sidebar
  $(btn).addEventListener("click", () => {
    closeOtherTools();
    amenSetPoint(null, false);
    $("q").value = q; search();
  });
