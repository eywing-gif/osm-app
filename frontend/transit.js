/* Transit mode and its filters; starts the data info at the end. Plain script: the files share one scope and load in this order (see index.html). */
// ---------- transit mode: public transport lines, filtered by type and frequency ----------
const TR_MODES = [   // order = list order; colours must match the map
  ["highspeed", "High-speed trains", "ICE, Railjet", "#0f172a"],
  ["intercity", "Intercity trains", "IC, EC, night trains", "#db2777"],
  ["regional", "Regional trains", "RE, RB", "#ea580c"],
  ["sbahn", "S-Bahn", "", "#16a34a"],
  ["subway", "Subway", "U-Bahn", "#2563eb"],
  ["tram", "Tram", "", "#7c3aed"],
  ["bus", "Bus", "from zoom 12", "#0891b2"],
  ["coach", "Long-distance buses", "FlixBus & co.", "#84cc16"],
  ["ferry", "Ferry", "", "#64748b"],
  ["other", "Other", "cable car, funicular", "#a16207"],
];
const TR_MIN_ZOOM = { highspeed: 4, intercity: 5, regional: 7, sbahn: 8, subway: 9, tram: 10, ferry: 9, other: 11, bus: 12, coach: 6 };
const TR_FREQ = [   // [upper limit in minutes, label, line width]
  [5, "≤ 5 min", 5], [10, "≤ 10 min", 4.2], [15, "≤ 15 min", 3.5], [20, "≤ 20 min", 2.9],
  [30, "≤ 30 min", 2.3], [60, "≤ 60 min", 1.7], [999, "> 60 min", 1.2],
];
const trColor = Object.fromEntries(TR_MODES.map(m => [m[0], m[3]]));
const trName = Object.fromEntries(TR_MODES.map(m => [m[0], m[1]]));
const trSaved = store.get("transit", {});
const tr = {
  on: false, day: trSaved.day || "wd", view: trSaved.view || "lines", width: trSaved.width || "freq",
  minutes: trSaved.minutes || 30, filters: null, filterOpen: false, detailData: null, color: trSaved.color || "type", lanes: trSaved.lanes !== false, size: trSaved.size || "freq",
  modes: new Set([...(trSaved.modes || TR_MODES.map(m => m[0])), ...(trSaved.v === 2 ? [] : ["coach"])]),   // coaches were added later
  freq: new Set(trSaved.freq || TR_FREQ.map(f => f[0])),
  meta: null, seq: 0,
};
const trSave = () => store.set("transit", { v: 2, day: tr.day, view: tr.view, minutes: tr.minutes, width: tr.width, color: tr.color, lanes: tr.lanes, size: tr.size, modes: [...tr.modes], freq: [...tr.freq] });
// interval class of a segment with n departures in the 12 h window (12 h = 720 min)
const trBucket = n => { const h = 720 / n; return TR_FREQ.find(f => h <= f[0] + 1e-9)[0]; };
const trQuery = () => new URLSearchParams({ modes: [...tr.modes].join(","), buckets: [...tr.freq].join(","), day: tr.day, centre: sdParams().centre });

// map layer: segments drawn in the browser, the server only sends coordinates
map.createPane("transit").style.zIndex = 300;
const TR_SAME_WIDTH = 2.6;      // line width when "all the same" is chosen
// Width by frequency. Zoomed in (14+) the width is exactly proportional to the departures, i.e. to 1 / interval:
// every 5 min is twice as wide as every 10 min. Zoomed out the scale is flattened (exponent < 1) so busy
// lines don't swallow the map. Reference: a 5-minute service (144 departures in 12 h) gets `ref`.
const lineWidth = (n, z) => {
  const p = z >= 14 ? 1 : z === 13 ? 0.85 : z === 12 ? 0.7 : 0.55;
  const ref = z >= 14 ? 6 : z === 13 ? 5.5 : z === 12 ? 5 : z >= 10 ? 4.5 : 4;
  return Math.min(26, Math.max(0.8, ref * (n / 144) ** p));
};
const TR_GRAY = "#4b5563";
// Station dot: its AREA is proportional to the departures (07-19 h, all types together), so the radius grows with the
// square root. Very small stops keep a minimum size, huge stations a maximum.
const trRadius = (n, z) => {
  const k = z < 9 ? 0.6 : z < 12 ? 0.8 : 1;
  return tr.size === "freq" ? Math.min(40, Math.max(2.5, 0.5 * Math.sqrt(n))) * k : 4 * k;
};

// Lines of different types that run on top of each other get their own lane next to each other.
// Returns, per segment, the sideways shift in pixels (lanes are as wide as the lines themselves).
function trLaneOffsets(items) {
  const byId = new Map(items.map(it => [it.id, it]));
  const GAP = 1;
  for (const it of items) {
    it.off = 0;
    if (!it.ov) continue;
    const grp = [it, ...it.ov.map(id => byId.get(id)).filter(Boolean)].sort((a, b) => a.cat - b.cat || a.id - b.id);
    if (grp.length < 2) continue;
    const total = grp.reduce((s, g) => s + g.w, 0) + GAP * (grp.length - 1);
    let pos = -total / 2;
    for (const g of grp) { if (g === it) it.off = pos + g.w / 2; pos += g.w + GAP; }
  }
}
// shift a polyline sideways; "left" is defined by a fixed direction (west -> east), so lanes of
// segments drawn in opposite directions still agree
function trOffsetLine(pts, off) {
  if (!off || pts.length < 2) return pts;
  const seg = [];
  for (let i = 0; i < pts.length - 1; i++) {
    let dx = pts[i + 1][0] - pts[i][0], dy = pts[i + 1][1] - pts[i][1];
    if (dx < 0 || (dx === 0 && dy < 0)) { dx = -dx; dy = -dy; }
    const len = Math.hypot(dx, dy) || 1;
    seg.push([-dy / len, dx / len]);
  }
  return pts.map((p, i) => {
    const a = seg[Math.max(0, i - 1)], b = seg[Math.min(seg.length - 1, i)];
    let nx = a[0] + b[0], ny = a[1] + b[1];
    const len = Math.hypot(nx, ny);
    if (len < 0.2) { nx = b[0]; ny = b[1]; } else { nx /= len; ny /= len; }   // sharp turns: use the segment's own side
    return [p[0] + nx * off, p[1] + ny * off];
  });
}

const TransitLayer = L.GridLayer.extend({
  createTile(coords, done) {
    const tile = document.createElement("canvas");
    tile.width = tile.height = 256;
    const z = coords.z, view = tr.view;
    const url = `${API}/transit/${view === "stations" ? "stations" : "tile"}/${z}/${coords.x}/${coords.y}?${trQuery()}`;
    fetch(url).then(r => r.ok ? r.json() : null).then(data => {
      if (!data) return done(null, tile);
      const ctx = tile.getContext("2d"), ox = coords.x * 256, oy = coords.y * 256, k = z < 9 ? 0.75 : z < 12 ? 0.9 : 1;
      const pt = (lat, lon) => { const p = map.project([lat, lon], z); return [p.x - ox, p.y - oy]; };
      if (view === "stations") {
        ctx.font = "11px system-ui, sans-serif"; ctx.textBaseline = "middle"; ctx.lineJoin = "round";
        // big stations first, so the small ones next to them stay visible
        for (const [lon, lat, name, parts, total] of data.s) {
          const [x, y] = pt(lat, lon), r = trRadius(total, z);
          // pie chart: every type of line gets a slice as big as its share of the departures
          let a0 = -Math.PI / 2;
          for (const [cat, n] of parts) {
            const a1 = a0 + 2 * Math.PI * n / total;
            ctx.beginPath(); ctx.moveTo(x, y); ctx.arc(x, y, r, a0, a1); ctx.closePath();
            ctx.fillStyle = trColor[data.cats[cat]]; ctx.fill();
            a0 = a1;
          }
          ctx.beginPath(); ctx.arc(x, y, r, 0, 7);
          ctx.lineWidth = 1.5; ctx.strokeStyle = "#fff"; ctx.stroke();
          if (z >= 14) {   // names only when zoomed in
            ctx.lineWidth = 3; ctx.strokeStyle = "rgba(255,255,255,.9)"; ctx.strokeText(name, x + r + 3, y);
            ctx.fillStyle = "#1f2328"; ctx.fillText(name, x + r + 3, y);
          }
        }
        return done(null, tile);
      }
      const items = data.e.map(([cat, n, flat, id, ov]) => {
        const pts = [];
        for (let i = 0; i < flat.length; i += 2) pts.push(pt(flat[i + 1], flat[i]));   // flat = lon, lat, lon, lat …
        return { cat, n, id, ov, pts, w: tr.width === "freq" ? lineWidth(n, z) : TR_SAME_WIDTH * k };
      });
      if (tr.lanes) trLaneOffsets(items);
      // thin and rare first, so the frequent lines and the faster trains stay on top
      items.sort((a, b) => a.cat - b.cat || a.n - b.n);
      ctx.lineCap = "round"; ctx.lineJoin = "round";
      for (const casing of [true, false]) {
        for (const it of items) {
          const pts = tr.lanes && it.off ? trOffsetLine(it.pts, it.off) : it.pts;
          ctx.strokeStyle = casing ? "rgba(255,255,255,.85)" : tr.color === "gray" ? TR_GRAY : trColor[data.cats[it.cat]];
          ctx.lineWidth = casing ? it.w + 1.6 : it.w;
          ctx.beginPath(); ctx.moveTo(pts[0][0], pts[0][1]);
          for (let i = 1; i < pts.length; i++) ctx.lineTo(pts[i][0], pts[i][1]);
          ctx.stroke();
        }
      }
      done(null, tile);
    }).catch(() => done(null, tile));
    return tile;
  },
});
const transitLayer = new TransitLayer({ pane: "transit", minZoom: 3, maxZoom: 19, updateWhenZooming: false, keepBuffer: 1 });

const TR_INFO_HELP = {
  lines: '<div class="empty">Click a line to see which services use that section and how often they run.</div>',
  stations: '<div class="empty">Click a station to see which types of line stop there and how often.</div>',
  reach: '<div class="empty">Click the map: how far you get in the chosen time.</div>',
};
function trRenderControls() {
  document.querySelectorAll("#trn-day button").forEach(b => b.classList.toggle("on", b.dataset.day === tr.day));
  document.querySelectorAll("#trn-view button").forEach(b => b.classList.toggle("on", b.dataset.view === tr.view));
  document.querySelectorAll("#trn-width button").forEach(b => b.classList.toggle("on", b.dataset.width === tr.width));
  document.querySelectorAll("#trn-color button").forEach(b => b.classList.toggle("on", b.dataset.color === tr.color));
  document.querySelectorAll("#trn-size button").forEach(b => b.classList.toggle("on", b.dataset.size === tr.size));
  $("trn-lanes").checked = tr.lanes;
  $("trn-lines-opts").hidden = tr.view !== "lines";
  trRenderFilters();
  $("trn-stations-opts").hidden = tr.view !== "stations";
  $("trn-reach-opts").hidden = tr.view !== "reach";
  $("rch-min").value = tr.minutes; $("rch-val").textContent = tr.minutes;
  $("trn-modes").innerHTML = TR_MODES.map(([id, name, hint, c]) => `
    <label class="tr-mode${tr.modes.has(id) ? "" : " off"}"><input type="checkbox" data-mode="${id}"${tr.modes.has(id) ? " checked" : ""}>
      <span class="sw" style="background:${c}"></span><span class="nm">${name}</span><small>${hint}</small></label>`).join("");
  $("trn-freq").innerHTML = TR_FREQ.map(([lim, label, w]) => `
    <button type="button" class="tr-chip${tr.freq.has(lim) ? " on" : ""}" data-lim="${lim}" aria-pressed="${tr.freq.has(lim)}">
      <i style="height:${w}px"></i>${label}</button>`).join("");
  const day = tr.meta?.days?.[tr.day];
  const zmin = Math.min(...[...tr.modes].map(m => TR_MIN_ZOOM[m]));
  $("tr-note").innerHTML = (tr.modes.size && map.getZoom() < zmin ? `<b>Zoom in to see the selected lines (from zoom ${zmin}).</b><br>` : "")
    + `Interval = average time between departures from 07:00 to 19:00 in the busier direction, all lines of that type together${day ? ` (timetable of ${day})` : ""}.
       ${tr.view === "reach" ? `Reach: walk ${WALK_KMH} km/h × ${DETOUR} detour to the stops and on from the last stop; wait = ½ interval; average day, not the exact timetable.` : ""}
       ${tr.view === "stations" ? "Dot area = departures from the station (all types together); the colours show how they split between the types of line (names from zoom 14)." : tr.width === "freq" ? "Line width = frequency (from zoom 14 exactly proportional: every 5 min is twice as wide as every 10 min)." : "All lines have the same width."}
       Lines follow the real tracks and roads where OpenStreetMap has the route, else straight between stops.
       Timetables: ${esc(dataInfo.transit_name || "–")}.`;
}
const trRefresh = () => { trSave(); trRenderControls(); trUpdateCount(); if (tr.on) { transitLayer.redraw(); if (tr.lastClick) trLookup(tr.lastClick); } };

function setTransit(on) {
  if (on === tr.on) return;
  if (on) {
    closeOtherTools("transit");
    transitLayer.addTo(map);
    tr.on = true; trUpdateCount(); trShowCentres();
    if (!tr.meta) fetch(`${API}/transit/meta`).then(async r => {
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `HTTP ${r.status}`);
      tr.meta = await r.json(); trRenderControls();
    }).catch(e => { $("tr-info").innerHTML = `<div class="empty" style="color:#b91c1c">${esc(e.message)}</div>`; });
  } else {
    map.removeLayer(transitLayer); inspectMark.clearLayers(); reachMark.clearLayers(); tr.seq++; tr.lastClick = null; tr.counts = null; tr.on = false; trShowCentres();
  }
  tr.on = on;
  $("sidebar").classList.toggle("trn-open", on);
  $("trn-btn").classList.toggle("trn-on", on);
  $("trn-btn").setAttribute("aria-expanded", String(on));
  if (on) trRenderControls();
}
$("trn-btn").addEventListener("click", () => setTransit(!tr.on));
$("pop-btn").addEventListener("click", () => {       // Population tool = the population search in the sidebar
  closeOtherTools();
  $("q").value = "population"; search();
});
$("trn-close").addEventListener("click", () => setTransit(false));
$("trn-view").addEventListener("click", e => { const b = e.target.closest("button"); if (b) { tr.view = b.dataset.view; tr.lastClick = null; reachMark.clearLayers(); $("tr-info").innerHTML = TR_INFO_HELP[tr.view]; inspectMark.clearLayers(); trRefresh(); } });
$("trn-color").addEventListener("click", e => { const b = e.target.closest("button"); if (b) { tr.color = b.dataset.color; trRefresh(); } });
$("trn-size").addEventListener("click", e => { const b = e.target.closest("button"); if (b) { tr.size = b.dataset.size; trRefresh(); } });
$("trn-lanes").addEventListener("change", e => { tr.lanes = e.target.checked; trRefresh(); });
$("trn-width").addEventListener("click", e => { const b = e.target.closest("button"); if (b) { tr.width = b.dataset.width; trRefresh(); } });
$("trn-day").addEventListener("click", e => { const b = e.target.closest("button"); if (b) { tr.day = b.dataset.day; trRefresh(); } });
$("trn-modes").addEventListener("change", e => {
  const id = e.target.dataset.mode; if (!id) return;
  e.target.checked ? tr.modes.add(id) : tr.modes.delete(id); trRefresh();
});
$("trn-freq").addEventListener("click", e => {
  const b = e.target.closest(".tr-chip"); if (!b) return;
  const lim = +b.dataset.lim; tr.freq.has(lim) ? tr.freq.delete(lim) : tr.freq.add(lim); trRefresh();
});
$("trn-all").addEventListener("click", () => { tr.modes = new Set(TR_MODES.map(m => m[0])); trRefresh(); });
$("trn-none").addEventListener("click", () => { tr.modes.clear(); trRefresh(); });
$("trn-fall").addEventListener("click", () => { tr.freq = new Set(TR_FREQ.map(f => f[0])); trRefresh(); });
map.on("zoomend", () => { if (tr.on) trRenderControls(); });

// ---------- filters (the same list is used by the map and by the station window) ----------
// Every filter is one entry of this list; the filter panel, the badge on the button and the "active filters" row are
// all built from it. To add a filter: add an entry here and handle the same parameter (id) in /transit/station
// on the server. options(d) -> [[value, label, hint], …] (the first is the default "no filter" option); note(d) -> help text.
const SD_FILTERS = [
  {
    // a line "goes into" a central place if one of its stops lies in the city centre of an Oberzentrum, Mittelzentrum
    // or Grundzentrum (Saxony, Berlin: official lists). Each line counts for the highest level it reaches.
    id: "centre", title: "Lines into a central place", multi: true, def: [],
    options: d => {
      const lv = d && d.levels, sum = lv && Object.values(lv).reduce((a, l) => a + l.departures_window, 0) / 12;
      return [["all", "All", sum], ["oz", "Oberzentrum", lv && lv.oz.per_hour], ["mz", "Mittelzentrum", lv && lv.mz.per_hour],
              ["gz", "Grundzentrum", lv && lv.gz.per_hour], ["other", "Other centre", lv && lv.other.per_hour],
              ["none", "Without centre", lv && lv.none.per_hour]];
    },
    note: d => `Shows only ${tr.view === "stations" ? "the stations" : "the line sections"} of lines that stop in the <b>city centre</b> (not the whole town) of a central place.
      A line counts for the highest level it reaches. Official levels exist for Saxony (Landesentwicklungsplan 2013, Regionalpläne) and Berlin;
      <i>Other centre</i> = main stations elsewhere (no official level); <i>Without centre</i> = lines that never stop in a centre.
      ${d && d.in_centre ? `<br>This station lies in the centre of <b>${esc(d.in_centre.name)}</b> (${esc(d.in_centre.level)}).` : ""}`,
  },
  // { id: "mode", title: "Type of line", def: "all", options: d => […], note: d => "…" },   <- next filter goes here
];
const sdDefaults = () => Object.fromEntries(SD_FILTERS.map(f => [f.id, f.multi ? [] : f.def]));
const sdIsActive = f => f.multi ? tr.filters[f.id].length > 0 : tr.filters[f.id] !== f.def;
const sdParams = () => Object.fromEntries(SD_FILTERS.map(f => [f.id, f.multi ? tr.filters[f.id].join(",") : tr.filters[f.id]]));   // sent to the server
const sdOn = (f, v) => f.multi ? (v === "all" ? tr.filters[f.id].length === 0 : tr.filters[f.id].includes(v)) : tr.filters[f.id] === v;
const sdSet = (f, v) => {   // click on an option: multiple choice toggles, "all" clears
  if (!f.multi) { tr.filters[f.id] = v; return; }
  if (v === "all") tr.filters[f.id] = [];
  else tr.filters[f.id] = tr.filters[f.id].includes(v) ? tr.filters[f.id].filter(x => x !== v) : [...tr.filters[f.id], v];
};
tr.filters = sdDefaults();

const FUNNEL = '<svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><path d="M1.5 2h13l-5 6.2V13l-3 1.5V8.2z"/></svg>';
// ---------- filter of the station dots on the map (Stations view) ----------
// "Filter" button + panel (type of line, how often, and every filter of the registry above) + tags of what is active.
const trFilterDefs = () => [
  { key: "modes", label: "Type of line", active: tr.modes.size < TR_MODES.length, clear: () => { tr.modes = new Set(TR_MODES.map(m => m[0])); },
    text: () => tr.modes.size === 0 ? "none" : tr.modes.size <= 3 ? [...tr.modes].map(m => trName[m]).join(", ") : `${tr.modes.size} of ${TR_MODES.length}` },
  { key: "freq", label: "How often", active: tr.freq.size < TR_FREQ.length, clear: () => { tr.freq = new Set(TR_FREQ.map(f => f[0])); },
    text: () => tr.freq.size === 0 ? "none" : `${tr.freq.size} of ${TR_FREQ.length} classes` },
  ...SD_FILTERS.map(f => ({ key: f.id, label: f.title.replace(/\?$/, ""), active: sdIsActive(f), clear: () => { tr.filters[f.id] = sdDefaults()[f.id]; },
    text: () => f.options(null).filter(o => o[0] !== "all" && sdOn(f, o[0])).map(o => o[1]).join(", ") })),
];
function trRenderFilters() {
  const stations = tr.view === "stations";
  $("trn-filter-head").hidden = false;
  $("trn-sfilters").hidden = false;
  $("trn-filterbox").hidden = !tr.mapFilterOpen;
  const defs = trFilterDefs(), act = defs.filter(d => d.active);
  $("trn-filter-btn").className = "lib-btn" + (tr.mapFilterOpen || act.length ? " on" : "");
  $("trn-filter-btn").setAttribute("aria-expanded", String(!!tr.mapFilterOpen));
  $("trn-filter-btn").innerHTML = `${FUNNEL} Filter${act.length ? ` <span class="sd-badge-n">${act.length}</span>` : ""}`;
  // tags: only while the panel is closed (open: the panel itself shows everything)
  $("trn-filter-tags").innerHTML = tr.mapFilterOpen ? "" : act.map(d => `<span class="sd-tag">${esc(d.label)}: <b>${esc(d.text())}</b> <a data-mclear="${d.key}" role="button" title="Remove this filter">×</a></span>`).join("")
    + (act.length > 1 ? '<a class="sd-reset" data-mclear="*" style="margin-top:0">Reset all</a>' : "");
  // the filters of the registry (centre, …) as groups of buttons; the number is how many stations there are in the visible map area
  const cnt = tr.counts, num0 = n => n == null ? "" : `<small>${n.toLocaleString()}</small>`;
  $("trn-sfilters").innerHTML = SD_FILTERS.map(f => `
    <div class="sd-fgroup"><h4>${esc(f.title.replace(/\?$/, ""))}${f.multi ? " <span style='text-transform:none;letter-spacing:0;font-weight:400'>– several possible</span>" : ""}</h4>
      <div class="tr-dirs" role="${f.multi ? "group" : "radiogroup"}" aria-label="${esc(f.title)}">
        ${f.options(null).map(([v, label]) => `<button type="button" role="${f.multi ? "checkbox" : "radio"}" data-f="${f.id}" data-v="${v}" class="tr-chip${sdOn(f, v) ? " on" : ""}"><span>${esc(label)}</span>${f.id === "centre" && cnt && stations ? num0(v === "all" ? cnt.all : cnt.levels[v]) : ""}</button>`).join("")}
      </div>
      <div class="sd-near" style="margin-top:6px">${f.note(null)}</div>
    </div>`).join("") + `
    <label class="tr-check"><input type="checkbox" id="trn-showcentres"${tr.showCentres ? " checked" : ""}> Show the city-centre areas on the map</label>
    <div id="trn-centre-legend" class="sd-near" ${tr.showCentres ? "" : "hidden"}></div>`;
  trRenderCentreLegend();
  // number of stations for the current filters, next to the Filter button
  $("trn-count").innerHTML = cnt && stations ? `<b>${cnt.total.toLocaleString()}</b> station${cnt.total === 1 ? "" : "s"} in view${cnt.hidden_modes.length ? ` <span title="Zoom in to see ${cnt.hidden_modes.map(m => trName[m]).join(', ')}" style="cursor:help">ⓘ</span>` : ""}` : "";
}

// ---- number of stations in the visible map area (for the current filters)
let countTimer = null, countSeq = 0;
function trUpdateCount() {
  clearTimeout(countTimer);
  if (!tr.on || tr.view !== "stations") return;
  countTimer = setTimeout(async () => {
    const seq = ++countSeq, b = map.getBounds();
    try {
      const q = new URLSearchParams({ minx: b.getWest(), miny: b.getSouth(), maxx: b.getEast(), maxy: b.getNorth(), zoom: map.getZoom(),
        modes: [...tr.modes].join(","), buckets: [...tr.freq].join(","), day: tr.day, centre: sdParams().centre });
      const r = await fetch(`${API}/transit/stations-summary?${q}`);
      if (!r.ok || seq !== countSeq) return;
      tr.counts = await r.json();
      trRenderFilters();
    } catch {}
  }, 250);
}
map.on("moveend", trUpdateCount);

// ---- the areas that count as "city centre" (Oberzentrum / Mittelzentrum / Grundzentrum)
const CENTRE_COLORS = { 1: "#b91c1c", 2: "#ea580c", 3: "#ca8a04" };
const CENTRE_NAMES = { 1: "Oberzentrum", 2: "Mittelzentrum", 3: "Grundzentrum" };
map.createPane("centres").style.zIndex = 290;
let centreLayer = null, centreData = null;
async function trShowCentres() {
  if (centreLayer) { map.removeLayer(centreLayer); centreLayer = null; }
  if (!tr.on || !tr.showCentres) return;
  if (!centreData) {
    try { const r = await fetch(`${API}/transit/centres`); if (!r.ok) throw 0; centreData = await r.json(); } catch { return; }
    trRenderCentreLegend();
  }
  if (!tr.on || !tr.showCentres || centreLayer) return;
  centreLayer = L.geoJSON(centreData, {
    pane: "centres",
    style: f => ({ color: CENTRE_COLORS[f.properties.level], weight: 1.5, fillColor: CENTRE_COLORS[f.properties.level], fillOpacity: .22 }),
    onEachFeature: (f, l) => l.bindTooltip(`${f.properties.name} · ${CENTRE_NAMES[f.properties.level]}`, { sticky: true }),
  }).addTo(map);
}
function trRenderCentreLegend() {
  const box = $("trn-centre-legend"); if (!box) return;
  box.hidden = !tr.showCentres;
  if (!tr.showCentres) return;
  const names = centreData?.names || {};
  box.innerHTML = [1, 2, 3].map(l => {
    const list = names[{ 1: "oz", 2: "mz", 3: "gz" }[l]] || [];
    return `<div style="margin-top:4px"><i style="display:inline-block;width:12px;height:12px;border-radius:3px;background:${CENTRE_COLORS[l]};opacity:.7;margin-right:6px;vertical-align:-2px"></i><b>${CENTRE_NAMES[l]}</b>${centreData ? ` (${list.length})` : ""}
      ${l === 3 ? "" : `<div style="margin-left:18px">${esc(list.join(", "))}</div>`}</div>`;
  }).join("") + '<div style="margin-top:4px">Grundzentren: names on the map (hover).</div>';
}
$("trn-filter-btn").addEventListener("click", () => { tr.mapFilterOpen = !tr.mapFilterOpen; trRenderFilters(); });
$("trn-filter-head").addEventListener("click", e => {
  const c = e.target.closest("[data-mclear]"); if (!c) return;
  const defs = trFilterDefs();
  (c.dataset.mclear === "*" ? defs : defs.filter(d => d.key === c.dataset.mclear)).forEach(d => d.clear());
  trRefresh();
});
$("trn-sfilters").addEventListener("click", e => {
  const f = e.target.closest("[data-f]"); if (!f) return;
  sdSet(SD_FILTERS.find(x => x.id === f.dataset.f), f.dataset.v); trRefresh();
});
$("trn-sfilters").addEventListener("change", e => {
  if (e.target.id === "trn-showcentres") { tr.showCentres = e.target.checked; trRenderCentreLegend(); trShowCentres(); }
});

// click: which services use the section / station under the cursor.
// Remembered, so the answer is refreshed when the day or a filter changes.
async function trLookup(latlng) {
  const seq = ++tr.seq, { lat, lng } = latlng;
  tr.lastClick = latlng;
  inspectMark.clearLayers();
  L.circleMarker(latlng, { radius: 9, color: "#fff", weight: 5, fill: false, interactive: false }).addTo(inspectMark);
  L.circleMarker(latlng, { radius: 9, color: "#e11d48", weight: 3, fill: false, interactive: false }).addTo(inspectMark);
  $("tr-info").innerHTML = '<div class="empty">Looking up…</div>';
  const dayName = { wd: "Weekday", sa: "Saturday", su: "Sunday" }[tr.day];
  if (tr.view === "reach") return trReach(latlng, seq);
  try {
    const r = await fetch(`${API}/transit/at?${new URLSearchParams({ lat, lon: lng, zoom: map.getZoom(), view: tr.view })}&${trQuery()}`);
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `HTTP ${r.status}`);
    const d = await r.json();
    if (seq !== tr.seq) return;
    const ivl = m => m == null ? dash : (m < 10 ? fmt(m, 1) : Math.round(m)) + " min";
    const perH = v => fmt(v, v < 10 ? 1 : 0);
    const sw = c => `<span class="sw" style="display:inline-block;width:14px;height:5px;border-radius:3px;background:${trColor[c]};margin-right:6px"></span>`;
    if (tr.view === "stations") {
      tr.quick = d.stations;
      // quick info; the "Details" button opens the station window with all lines
      $("tr-info").innerHTML = d.stations.length ? `<div class="md" style="margin-bottom:2px">${dayName}, 07:00–19:00</div>` + d.stations.map((st, i) => {
        const tot = st.departures || 1;
        return `<div class="hit">
          <div class="top">${esc(st.name)}</div>
          <div class="tot"><b>${perH(st.per_hour)}</b> departures per hour · one every <b>${ivl(st.interval_min)}</b>
            <small>(${st.departures.toLocaleString()} in 12 h)</small></div>
          <div class="qmix">${st.modes.map(m => `<div style="width:${m.departures / tot * 100}%;background:${trColor[m.cat]}" title="${esc(trName[m.cat])}"></div>`).join("")}</div>
          ${st.modes.map(m => `<div class="md">${sw(m.cat)}${trName[m.cat]} · ${perH(m.per_hour)} per hour · every ${ivl(m.interval_min)}</div>`).join("")}
          <div class="more"><button type="button" class="lib-btn" data-details="${i}">Details ›</button></div>
        </div>`;
      }).join("") : '<div class="empty">No station here with the current filters.</div>';
      return;
    }
    $("tr-info").innerHTML = d.items.length ? `<div class="md" style="margin-bottom:2px">${dayName}, 07:00–19:00</div>` + d.items.map(it => `
      <div class="hit"><div class="top"><span class="sw" style="background:${trColor[it.cat]}"></span>${esc(it.from || "?")} – ${esc(it.to || "?")}
        <span class="iv">every ${ivl(it.interval_min)}</span></div>
        <div class="md">${trName[it.cat]} · ${perH(it.departures / 12)} departures per hour (${it.departures} in 12 h)</div>
        ${it.lines ? `<div class="ln">${esc(it.lines)}</div>` : ""}</div>`).join("")
      : '<div class="empty">No line here with the current filters.</div>';
  } catch (err) {
    if (seq === tr.seq) $("tr-info").innerHTML = `<div class="empty" style="color:#b91c1c">${esc(err.message)}</div>`;
  }
}
map.on("click", e => { if (tr.on) trLookup(e.latlng); });

// Reach: the area you get to from the clicked point within tr.minutes (circles around the stops, merged on the server)
const WALK_KMH = 4.5, DETOUR = 1.2;       // as on the server (backend/population.py)
const reachMark = L.layerGroup().addTo(map);
async function trReach(latlng, seq) {
  reachMark.clearLayers();
  try {
    const r = await fetch(`${API}/transit/reach?${new URLSearchParams({ lat: latlng.lat, lon: latlng.lng, minutes: tr.minutes, day: tr.day, modes: [...tr.modes].join(",") })}`);
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `HTTP ${r.status}`);
    const d = await r.json();
    if (seq !== tr.seq) return;
    L.geoJSON(d.geometry, { interactive: false, style: { color: "#e11d48", weight: 2, fillColor: "#e11d48", fillOpacity: 0.22 } }).addTo(reachMark);
    $("tr-info").innerHTML = `<table class="reach">
      <tr><td>Time</td><td><b>${d.minutes}</b> min</td></tr>
      <tr><td>Area</td><td><b>${d.area_km2.toLocaleString()}</b> km²</td></tr>
      <tr><td>Population</td><td><b>${d.population == null ? dash : d.population.toLocaleString()}</b></td></tr>
      <tr><td>Stops</td><td><b>${d.stops.toLocaleString()}</b></td></tr></table>`;
  } catch (err) {
    if (seq === tr.seq) $("tr-info").innerHTML = `<div class="empty" style="color:#b91c1c">${esc(err.message)}</div>`;
  }
}
let reachTimer;
$("rch-min").addEventListener("input", e => {
  tr.minutes = +e.target.value; $("rch-val").textContent = tr.minutes; trSave();
  clearTimeout(reachTimer);
  reachTimer = setTimeout(() => { if (tr.on && tr.view === "reach" && tr.lastClick) trLookup(tr.lastClick); }, 300);
});

// "Details": the station window (a page of its own, so it can grow without crowding the sidebar)
function openStationWindow(st) {
  const url = `${API}/station?${new URLSearchParams({ lon: st.lon, lat: st.lat, day: tr.day, centre: sdParams().centre })}`;
  const w = window.open(url, `osm-station-${st.lon}-${st.lat}`, "popup=yes,width=640,height=900");
  if (!w) window.open(url, "_blank");     // pop-ups blocked: use a normal tab
  else w.focus();
}
$("tr-info").addEventListener("click", e => {
  const b = e.target.closest("[data-details]");
  if (b && tr.quick && tr.quick[+b.dataset.details]) openStationWindow(tr.quick[+b.dataset.details]);
});

let t;
map.on("moveend", () => {
  if (area || !$("live").checked || !$("q").value.trim()) return;   // an area doesn't move with the map
  if (Date.now() < suppressFollowUntil) return;
  // online: wait until the map has stopped moving for a moment, to spare the public servers
  clearTimeout(t); t = setTimeout(search, source === "online" ? 1200 : 300);
});
loadDataInfo(true);
