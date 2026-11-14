/* Landuse tool (OSM landuse=*). Plain script: the files share one scope and load in this order (see index.html). */
// ---------- Landuse tool: OSM landuse=* values of the view, drawn on the map and listed with their area ----------
const LU_COLORS = { residential: "#e8b4a0", commercial: "#e05a8a", retail: "#d9902f", industrial: "#9a8aa8", farmland: "#d8e8a8",
  farmyard: "#c8a878", meadow: "#a8d878", grass: "#88c860", forest: "#2e7d32", orchard: "#8cc068", vineyard: "#b8a050", allotments: "#d0b878",
  cemetery: "#7a9a8a", construction: "#b8a090", brownfield: "#a08068", greenfield: "#b8d898", recreation_ground: "#68c8a8", garages: "#b0b0b8",
  railway: "#808090", military: "#c05050", quarry: "#888070", landfill: "#9a7a6a", basin: "#78b8e8", religious: "#c8b8d8", education: "#e8d878",
  depot: "#a8a8b8", village_green: "#98d868", flowerbed: "#e888c8", plant_nursery: "#78a860", greenhouse_horticulture: "#a0d0b0" };
const luColor = v => LU_COLORS[v] || `hsl(${[...v].reduce((h, c) => (h * 31 + c.charCodeAt(0)) % 360, 7)}, 45%, 62%)`;
map.createPane("landuse").style.zIndex = 235;
const lu = { on: false, off: new Set(store.get("luOff", [])), layer: null, data: null, seq: 0, timer: 0, opacity: store.get("luOpacity", 0.6) };

function luDraw() {
  if (lu.layer) map.removeLayer(lu.layer);
  lu.layer = null;
  if (!lu.data) return;
  lu.layer = L.geoJSON(lu.data, {
    pane: "landuse", interactive: false,
    filter: f => !lu.off.has(f.properties.v),
    style: f => ({ color: luColor(f.properties.v), weight: .6, fillColor: luColor(f.properties.v), fillOpacity: lu.opacity, opacity: Math.min(1, lu.opacity + .2) }),
  }).addTo(map);
}

async function luLoad() {
  if (!lu.on) return;
  const seq = ++lu.seq, z = map.getZoom(), b = map.getBounds();
  if (z < 9) { lu.data = null; luDraw(); $("lu-body").innerHTML = '<div class="empty">Zoom in</div>'; return; }
  const q = new URLSearchParams({ minx: b.getWest(), miny: b.getSouth(), maxx: b.getEast(), maxy: b.getNorth(), zoom: z });
  try {
    const [st, ft] = await Promise.all([fetch(`${API}/landuse/stats?${q}`), fetch(`${API}/landuse/features?${q}`)]);
    if (!st.ok || !ft.ok) throw new Error((await (st.ok ? ft : st).json().catch(() => ({}))).detail || "Error");
    const stats = await st.json(), feats = await ft.json();
    if (seq !== lu.seq) return;
    lu.data = feats; luDraw(); luList(stats.values, feats.truncated);
  } catch (e) { if (seq === lu.seq) $("lu-body").innerHTML = `<div class="empty" style="color:#b91c1c">${esc(e.message)}</div>`; }
}

function luList(values, truncated) {
  const total = values.reduce((a, v) => a + v.km2, 0) || 1, top = values[0]?.km2 || 1;
  $("lu-body").innerHTML = `<div class="head"><span>In view</span><span><a data-a="all">all</a><a data-a="none">none</a></span></div>`
    + (values.map(v => `<div class="lu-row${lu.off.has(v.value) ? " off" : ""}" data-v="${esc(v.value)}">
        <input type="checkbox" ${lu.off.has(v.value) ? "" : "checked"} tabindex="-1"><i class="sw" style="background:${luColor(v.value)}"></i>
        <span>${esc(v.value)}</span><span class="a">${v.km2.toLocaleString(undefined, { maximumFractionDigits: v.km2 < 10 ? 2 : 1 })} km² · ${Math.round(v.km2 / total * 100)} %</span>
        <div class="bar"><i style="width:${v.km2 / top * 100}%;background:${luColor(v.value)}"></i></div></div>`).join("") || '<div class="empty">No landuse here</div>')
    + (truncated ? '<div class="empty">Only the biggest areas are drawn – zoom in.</div>' : "")
    + `<label class="op">Strength <input type="range" id="lu-op" min="20" max="100" step="5" value="${Math.round(lu.opacity * 100)}"></label>`;
}

$("lu-body").addEventListener("click", e => {
  const a = e.target.closest("[data-a]"), row = e.target.closest(".lu-row");
  if (a) {
    const all = [...document.querySelectorAll(".lu-row")].map(r => r.dataset.v);
    lu.off = a.dataset.a === "none" ? new Set(all) : new Set();
  } else if (row) lu.off.has(row.dataset.v) ? lu.off.delete(row.dataset.v) : lu.off.add(row.dataset.v);
  else return;
  store.set("luOff", [...lu.off]);
  document.querySelectorAll(".lu-row").forEach(r => { const off = lu.off.has(r.dataset.v); r.classList.toggle("off", off); r.querySelector("input").checked = !off; });
  luDraw();
});
$("lu-body").addEventListener("input", e => {
  if (e.target.id !== "lu-op") return;
  lu.opacity = e.target.value / 100; store.set("luOpacity", lu.opacity); luDraw();
});

function setLanduse(on) {
  if (on === lu.on) return;
  if (on) closeOtherTools("landuse");
  lu.on = on;
  $("sidebar").classList.toggle("lu-open", on);
  $("lu-btn").classList.toggle("on", on);
  $("lu-btn").setAttribute("aria-expanded", String(on));
  if (on) luLoad();
  else { lu.seq++; lu.data = null; luDraw(); }
}
$("lu-btn").addEventListener("click", () => setLanduse(!lu.on));
$("lu-close").addEventListener("click", () => setLanduse(false));
map.on("moveend", () => { if (lu.on) { clearTimeout(lu.timer); lu.timer = setTimeout(luLoad, 400); } });
map.on("pm:drawstart", () => setLanduse(false));
