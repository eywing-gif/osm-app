/* Overlays: slope, population / built-up grids, CORINE, legend, density filter. Plain script: the files share one scope and load in this order. */
// ---------- slope overlay ----------
// Elevation comes from the free "Terrarium" tiles (each pixel's colour encodes its height);
// the steepness is computed here in the browser and coloured by degrees.
// Shading in dark gray: invisible on flat ground (the map stays white/as it is), darker and more opaque
// the steeper it gets, up to 45° and more.
const SLOPE_FULL = 45;       // degrees at which the shading is darkest
const SLOPE_SHADE = 30;      // gray level of the shading (0 = black)
const SLOPE_OPACITY = 0.85;  // default strength of the overlay (adjustable in its legend)
const slopeAlpha = deg => Math.min(1, deg / SLOPE_FULL) ** 0.8;
const slopeLUT = (() => {   // RGBA per whole degree 0..90
  const lut = new Uint8ClampedArray(91 * 4);
  for (let deg = 1; deg <= 90; deg++) {
    lut.set([SLOPE_SHADE, SLOPE_SHADE, SLOPE_SHADE, Math.round(255 * slopeAlpha(deg))], deg * 4);
  }
  return lut;
})();

const SlopeLayer = L.GridLayer.extend({
  createTile(coords, done) {
    const tile = document.createElement("canvas");
    tile.width = tile.height = 256;
    const img = new Image();
    img.crossOrigin = "anonymous";
    img.onload = () => { try { drawSlope(tile, img, coords); } catch (e) {} done(null, tile); };
    img.onerror = () => done(null, tile);   // sea or no data: leave empty
    img.src = `https://s3.amazonaws.com/elevation-tiles-prod/terrarium/${coords.z}/${coords.x}/${coords.y}.png`;
    return tile;
  },
});

function drawSlope(tile, img, { x, y, z }) {
  const ctx = tile.getContext("2d", { willReadFrequently: true });
  ctx.drawImage(img, 0, 0);
  const px = ctx.getImageData(0, 0, 256, 256), d = px.data;
  const h = new Float32Array(256 * 256);
  for (let i = 0; i < h.length; i++) h[i] = d[i * 4] * 256 + d[i * 4 + 1] + d[i * 4 + 2] / 256 - 32768;
  // ground size of one pixel in metres at this tile's latitude
  const lat = Math.atan(Math.sinh(Math.PI - 2 * Math.PI * (y + 0.5) / 2 ** z));
  const m = 40075016.686 * Math.cos(lat) / (256 * 2 ** z);
  for (let r = 0; r < 256; r++) {
    const up = Math.max(r - 1, 0) * 256, dn = Math.min(r + 1, 255) * 256, row = r * 256;
    for (let c = 0; c < 256; c++) {
      const l = Math.max(c - 1, 0), rt = Math.min(c + 1, 255);
      const dx = (h[row + rt] - h[row + l]) / ((rt - l) * m);
      const dy = (h[dn + c] - h[up + c]) / (((dn - up) / 256) * m);
      const deg = Math.min(90, Math.round(Math.atan(Math.hypot(dx, dy)) * 57.2958));
      d.set(slopeLUT.subarray(deg * 4, deg * 4 + 4), (row + c) * 4);
    }
  }
  ctx.putImageData(px, 0, 0);
}

// ---------- population density overlay ----------
// The server sends one number per 100 m cell (summed to bigger squares when zoomed out); the colours are chosen here.
// Colour scale: densities (inhabitants per km²) and their colours. The steps are spaced evenly on screen, so the
// range from 1,000 to 40,000 – where cities are – gets about 5/8 of the scale. Colours are blended in between.
// [density, colour, position on the scale 0..1]: 0 – 1,000 is only a short part (few cells), the rest is spread out
const POP_SCALE = [[10, "#ffffd4", 0], [100, "#fee391", .02], [500, "#fec44f", .05], [1000, "#fe9929", .08], [2500, "#ec7014", .26],
                   [5000, "#d7301f", .43], [10000, "#a50f15", .61], [20000, "#7a0177", .8], [40000, "#2b0a57", 1]];
const POP_OPACITY = 0.6;
const POP_MIN_ZOOM = 6;
const popFull = d => {   // position 0..1 on the whole scale (logarithmic between neighbouring steps)
  const n = POP_SCALE.length - 1;
  if (d <= POP_SCALE[0][0]) return 0;
  if (d >= POP_SCALE[n][0]) return 1;
  let i = 0; while (d >= POP_SCALE[i + 1][0]) i++;
  const [d0, , t0] = POP_SCALE[i], [d1, , t1] = POP_SCALE[i + 1];
  return t0 + (t1 - t0) * Math.log(d / d0) / Math.log(d1 / d0);
};
const popInv = t => {    // density at a position on the whole scale
  let i = 0; while (i < POP_SCALE.length - 2 && t > POP_SCALE[i + 1][2]) i++;
  const [d0, , t0] = POP_SCALE[i], [d1, , t1] = POP_SCALE[i + 1];
  return d0 * (d1 / d0) ** ((t - t0) / (t1 - t0));
};
// The filter (from / to, inhabitants per km²): cells outside are not drawn and the colours are spread over what is left,
// so the same palette shows finer steps.
const popRange = { min: null, max: null, ...store.get("popRange", {}) };
let popLo = 0, popSpan = 1;
const popSetRange = () => {
  popLo = popRange.min > 0 ? popFull(popRange.min) : 0;
  popSpan = Math.max((popRange.max > 0 ? popFull(popRange.max) : 1) - popLo, 1e-6);
};
popSetRange();
const popT = d => Math.min(1, Math.max(0, (popFull(d) - popLo) / popSpan));
let popSize = store.get("popSize", 0);               // edge of the squares in m, 0 = as fine as the data
let popLabels = store.get("popLabels", false);   // density as a number in each square
const popShown = d => !(popRange.min > 0 && d < popRange.min) && !(popRange.max > 0 && d > popRange.max);
const popRgb = POP_SCALE.map(([, h]) => [1, 3, 5].map(i => parseInt(h.slice(i, i + 2), 16)));
const popColorAt = t => {   // t: position 0..1 on the (filtered) scale; the whole palette is spread over it
  const tt = t;
  let i = 0; while (i < POP_SCALE.length - 2 && tt > POP_SCALE[i + 1][2]) i++;
  const u = Math.min(1, Math.max(0, (tt - POP_SCALE[i][2]) / (POP_SCALE[i + 1][2] - POP_SCALE[i][2])));
  return `rgb(${popRgb[i].map((c, j) => Math.round(c + (popRgb[i + 1][j] - c) * u)).join(",")})`;
};
const popColor = d => popColorAt(popT(d));

// A layer of grid cells: the server sends [x, y, value] per cell, `spec` says where to get it and how it looks:
//   url(z, x, y), t(value) 0..1 on the colour scale, color(value), show(value), text(value, cellPixels) or null
const GridCellLayer = L.GridLayer.extend({
  createTile({ x, y, z }, done) {
    const tile = document.createElement("canvas"), spec = this.options.spec;
    tile.width = tile.height = 256;
    fetch(spec.url(z, x, y)).then(r => r.ok ? r.json() : null).then(data => {
      if (!data) return done(null, tile);
      const ctx = tile.getContext("2d"), [ax, ay] = data.ex, [bx, by] = data.ey;
      // each cell is a parallelogram: the grid is slightly rotated against the map.
      // Drawn a touch larger (grow) so no hairlines show between neighbours.
      const len = Math.hypot(ax, ay), grow = 0.35 / Math.max(len, 0.5), g = 0.5 + grow;
      let last = -1, text = false;
      for (const [cx, cy, d] of data.cells) {
        if (!spec.show(d)) continue;
        const c = Math.round(spec.t(d) * 80);
        if (c !== last) { ctx.fillStyle = spec.color(d); last = c; }
        ctx.beginPath();
        ctx.moveTo(cx - g * ax - g * bx, cy - g * ay - g * by);
        ctx.lineTo(cx + g * ax - g * bx, cy + g * ay - g * by);
        ctx.lineTo(cx + g * ax + g * bx, cy + g * ay + g * by);
        ctx.lineTo(cx - g * ax + g * bx, cy - g * ay + g * by);
        ctx.closePath();
        ctx.fill();
        const label = len >= 22 && spec.text(d, len);        // numbers only where a cell is big enough
        if (label) {
          if (!text) { ctx.font = `${Math.min(12, Math.round(len * 0.27))}px system-ui, sans-serif`; ctx.textAlign = "center"; ctx.textBaseline = "middle"; text = true; }
          ctx.fillStyle = spec.t(d) > 0.5 ? "#fff" : "#222";
          ctx.fillText(label, cx, cy);
          last = -1;
        }
      }
      done(null, tile);
    }).catch(() => done(null, tile));   // offline or no data: leave empty
    return tile;
  },
});
const PopDensityLayer = opts => new GridCellLayer({ ...opts, spec: {
  url: (z, x, y) => `${API}/density/${z}/${x}/${y}?v=${dataInfo.population_id}&size=${popSize}`,
  t: popT, color: popColor, show: popShown,
  text: (d, len) => popLabels && (len >= 44 ? d.toLocaleString() : popNice(d)),
} });

// built-up share of a cell (GHS-BUILT-S), 0 – 100 %. Most cells are only a few per cent: the scale follows the square root,
// so countryside shows a tint and towns do not drown it.
const BUILT_SCALE = ["#fff1c9", "#fdd28a", "#f4a259", "#dc6b3f", "#b03a4a", "#702a55", "#2b1b3d"];
const builtT = v => Math.sqrt(Math.min(1, Math.max(0, v / 100)));
const builtColorAt = t => rampColor(BUILT_SCALE, t);
const BuiltLayer = (col, opts) => new GridCellLayer({ ...opts, spec: {
  url: (z, x, y) => `${API}/built/${z}/${x}/${y}?v=${dataInfo.built_id}&col=${col}`,
  t: builtT, color: v => builtColorAt(builtT(v)), show: v => v >= 0.3, text: () => null,
} });

// built-up area per inhabitant (m²) for 1 km squares: log scale 20 m² (dense) … 500 m² (sprawl); grey: nobody lives there
const PP_SCALE = ["#f7fcb9", "#addd8e", "#41ab5d", "#1a9a7c", "#1d6f9c", "#2f3c8f", "#3b1f5e"], PP_MIN = 20, PP_MAX = 500, PP_NOBODY = 2000;
const ppT = v => Math.min(1, Math.max(0, Math.log(v / PP_MIN) / Math.log(PP_MAX / PP_MIN)));
const rampColor = (scale, t) => {
  const rgb = scale.map(h => [1, 3, 5].map(i => parseInt(h.slice(i, i + 2), 16))), f = t * (rgb.length - 1), i = Math.min(rgb.length - 2, Math.floor(f)), u = f - i;
  return `rgb(${rgb[i].map((c, j) => Math.round(c + (rgb[i + 1][j] - c) * u)).join(",")})`;
};
const PerPersonLayer = opts => new GridCellLayer({ ...opts, spec: {
  url: (z, x, y) => `${API}/perperson/${z}/${x}/${y}?v=${dataInfo.population_id}${dataInfo.built_id}`,
  t: ppT, color: v => v >= PP_NOBODY ? "#9aa0a6" : rampColor(PP_SCALE, ppT(v)), show: () => true, text: v => v >= PP_NOBODY ? "–" : String(v),
} });

// road lanes per 1 km square or per administrative area (roads.js): lane-km in total, per km² or per inhabitant (m);
// the road types are chosen in the Roads list
const ROAD_TYPES = [["motorway", "Motorway"], ["trunk", "Trunk"], ["primary", "Primary"], ["secondary", "Secondary"], ["tertiary", "Tertiary"],
  ["unclassified", "Unclassified"], ["residential", "Residential"], ["living_street", "Living street"], ["service", "Service"],
  ["driveway", "Driveway"], ["parking_aisle", "Parking aisle"], ["track", "Track"], ["pedestrian", "Pedestrian"], ["cycleway", "Cycleway"], ["footway", "Footway, path"]];
const roadName = Object.fromEntries(ROAD_TYPES);
const ROAD_HINT = {     // tooltip in the Roads list (OSM highway=*)
  motorway: "Autobahn, incl. ramps", trunk: "Expressway below motorway: Kraftfahrstraße, dual-carriageway Bundesstraße",
  primary: "Main road between towns, usually Bundesstraße", secondary: "Regional road, usually Landesstraße",
  tertiary: "Local connector, usually Kreisstraße / main street in town", unclassified: "Minor public road, e.g. between villages",
  residential: "Street in a residential area", living_street: "Verkehrsberuhigter Bereich (walking pace)",
  service: "Access road to a site: company yard, petrol station, alley", driveway: "Driveway to a house or garage",
  parking_aisle: "Lane inside a car park", track: "Farm or forest track", pedestrian: "Pedestrian zone",
  cycleway: "Separate cycle path", footway: "Footway, path, steps",
};
// default: roads for cars only (no service roads, driveways, parking aisles, tracks, foot- and cycleways)
const ROAD_OFF = ["service", "driveway", "parking_aisle", "track", "pedestrian", "cycleway", "footway"];
const roadSel = new Set(store.get("roadTypes3", ROAD_TYPES.map(t => t[0]).filter(t => !ROAD_OFF.includes(t))));
let roadPer = store.get("roadPer", "area");          // total: lane-km · area: lane-km per km² · person: lane-m per inhabitant
const ROAD_SCALE = ["#fff7ec", "#fdd49e", "#fc8d59", "#d7301f", "#7f0000", "#2b0000"];
const ROAD_RANGE = { total: [2, 500], area: [2, 500], person: [1, 300] };     // log scale; grid "total": per square
const ROAD_UNIT = { total: "lane-km", area: "lane-km per km²", person: "lane-m per inhabitant" };
const ROAD_LEVELS = { 4: ["State", "admin_level 4: Bundesland"], 6: ["County", "admin_level 6: Landkreis, kreisfreie Stadt"],
  8: ["Town", "admin_level 8: Gemeinde, Stadt"], 9: ["Borough", "admin_level 9: Stadtbezirk"], 10: ["Quarter", "admin_level 10: Stadtteil, Ortsteil"] };
let roadView = "off";                                 // off · grid · an admin level (roads.js)
let roadAdminRange = null;                            // admin areas in total: the scale follows the areas shown
const roadAdmin = L.layerGroup();                     // the areas, drawn by roads.js
const roadRange = () => roadPer === "total" && roadView !== "grid" && roadAdminRange ? roadAdminRange : ROAD_RANGE[roadPer];
const roadT = v => { const [lo, hi] = roadRange(); return Math.min(1, Math.max(0, Math.log(v / lo) / Math.log(hi / lo))); };
const RoadLayer = opts => new GridCellLayer({ ...opts, spec: {
  url: (z, x, y) => `${API}/roads/grid/${z}/${x}/${y}?${new URLSearchParams({ types: [...roadSel].join(","), per: roadPer, v: dataInfo.population_id })}`,
  t: roadT, color: v => rampColor(ROAD_SCALE, roadT(v)), show: v => v > 0, text: v => String(Math.round(v)),
} });

// own pane, so the slope always sits above any base map (incl. vector maps) and below markers
map.createPane("popdensity").style.zIndex = 240;   // above the base map, below the slope shading
map.createPane("slope").style.zIndex = 250;
map.createPane("labels").style.zIndex = 255;   // place names over a satellite image
map.getPane("labels").style.pointerEvents = "none";

map.createPane("clc").style.zIndex = 232;       // CORINE land cover, below the OSM landuse
// the 44 classes of CORINE with the colours of the map (checked against the tiles: every colour on the map is here)
const CLC_CLASSES = [["230,0,77", "Continuous urban fabric"], ["255,0,0", "Discontinuous urban fabric"], ["204,77,242", "Industrial or commercial units"],
  ["204,0,0", "Road and rail networks"], ["230,204,204", "Port areas"], ["230,204,230", "Airports"], ["166,0,204", "Mineral extraction sites"],
  ["166,77,0", "Dump sites"], ["255,77,255", "Construction sites"], ["255,166,255", "Green urban areas"], ["255,230,255", "Sport and leisure"],
  ["255,255,168", "Non-irrigated arable land"], ["255,255,0", "Permanently irrigated land"], ["230,230,0", "Rice fields"], ["230,128,0", "Vineyards"],
  ["242,166,77", "Fruit trees, berries"], ["230,166,0", "Olive groves"], ["230,230,77", "Pastures"], ["255,230,166", "Annual + permanent crops"],
  ["255,230,77", "Complex cultivation"], ["230,204,77", "Agriculture + natural vegetation"], ["242,204,166", "Agro-forestry"],
  ["128,255,0", "Broad-leaved forest"], ["0,166,0", "Coniferous forest"], ["77,255,0", "Mixed forest"], ["204,242,77", "Natural grasslands"],
  ["166,255,128", "Moors and heathland"], ["166,230,77", "Sclerophyllous vegetation"], ["166,242,0", "Transitional woodland-shrub"],
  ["230,230,230", "Beaches, dunes, sands"], ["204,204,204", "Bare rocks"], ["204,255,204", "Sparsely vegetated"], ["0,0,0", "Burnt areas"],
  ["166,230,204", "Glaciers, snow"], ["166,166,255", "Inland marshes"], ["77,77,255", "Peat bogs"], ["204,204,255", "Salt marshes"],
  ["230,230,255", "Salines"], ["166,166,230", "Intertidal flats"], ["0,204,242", "Water courses"], ["128,242,230", "Water bodies"],
  ["0,255,166", "Coastal lagoons"], ["166,255,230", "Estuaries"], ["230,242,255", "Sea and ocean"]];
const CLC_OPACITY = 0.6;
const layer = L.layerGroup();                   // the search results (markers on the map)
const MARKERS = "Search results (markers)";
const POPULATION = "Population density";
const PER_PERSON = "Built-up per person";
const ROADS = "Road lanes (1 km)";
const overlays = {
  [MARKERS]: layer,
  [POPULATION]: PopDensityLayer({
    minZoom: POP_MIN_ZOOM, maxNativeZoom: 16, maxZoom: 19, opacity: store.get("popOpacity", POP_OPACITY), pane: "popdensity",
    attribution: 'Population: <a href="https://www.zensus2022.de">Zensus 2022</a> (Statistische Ämter des Bundes und der Länder), <a href="https://ghsl.jrc.ec.europa.eu">GHS-POP</a> (EC JRC)',
  }),
  "Built-up area (GHS)": BuiltLayer("built", { minZoom: 5, maxNativeZoom: 16, maxZoom: 19, opacity: store.get("builtOpacity", 0.7), pane: "popdensity",
    attribution: 'Built-up: <a href="https://ghsl.jrc.ec.europa.eu">GHS-BUILT-S</a> 2020 (EC JRC)' }),
  "Non-residential built-up (GHS)": BuiltLayer("nres", { minZoom: 5, maxNativeZoom: 16, maxZoom: 19, opacity: store.get("builtOpacity", 0.7), pane: "popdensity",
    attribution: 'Built-up: <a href="https://ghsl.jrc.ec.europa.eu">GHS-BUILT-S</a> 2020 (EC JRC)' }),
  [PER_PERSON]: PerPersonLayer({ minZoom: 6, maxNativeZoom: 16, maxZoom: 19, opacity: store.get("ppOpacity", 0.7), pane: "popdensity",
    attribution: 'Built-up: <a href="https://ghsl.jrc.ec.europa.eu">GHS-BUILT-S</a> 2020 ÷ population (EC JRC, Zensus 2022 / GHS-POP)' }),
  [ROADS]: RoadLayer({ minZoom: 7, maxNativeZoom: 16, maxZoom: 19, opacity: store.get("roadOpacity", 0.7), pane: "popdensity",
    attribution: 'Roads: © OpenStreetMap contributors' }),
  "CORINE land cover (2018)": L.tileLayer.wms("https://image.discomap.eea.europa.eu/arcgis/services/Corine/CLC2018_WM/MapServer/WMSServer", {
    layers: "12,13", version: "1.3.0", format: "image/png", transparent: true, pane: "clc", minZoom: 4, maxNativeZoom: 18, maxZoom: 19,
    opacity: store.get("clcOpacity", CLC_OPACITY),
    attribution: '© <a href="https://land.copernicus.eu/pan-european/corine-land-cover">CORINE Land Cover 2018</a>, Copernicus / EEA',
  }),
  "Slope (steepness)": new SlopeLayer({
    minZoom: 8, maxNativeZoom: 13, maxZoom: 19, opacity: store.get("slopeOpacity", SLOPE_OPACITY), pane: "slope",
    attribution: 'Elevation: <a href="https://github.com/tilezen/joerd/blob/master/docs/attribution.md">Mapzen Terrain Tiles</a>',
  }),
};

// Place names belong to the satellite image: they are offered in the menu (and shown) only while the
// Satellite base map is active. Whether the user wants them is remembered separately.
const PLACE_NAMES = "Place names (for satellite)";
const placeNames = L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}",
  { maxZoom: 19, maxNativeZoom: 17, pane: "labels", attribution: 'Labels © <a href="https://www.esri.com">Esri</a>' });
let placeNamesWanted = store.get("placeNames", store.get("overlays", []).includes(PLACE_NAMES));   // old saves kept the choice in "overlays"
let placeNamesInMenu = false, syncingPlaceNames = false;
function syncPlaceNames(base) {
  syncingPlaceNames = true;             // our own add/remove below is not a choice by the user
  try {
    if (base === "Satellite") {
      // switch the layer on first, so the menu is drawn with the right tick
      if (placeNamesWanted) placeNames.addTo(map);
      if (!placeNamesInMenu) { layerControl.addOverlay(placeNames, PLACE_NAMES); placeNamesInMenu = true; }
    } else {
      map.removeLayer(placeNames);
      if (placeNamesInMenu) { layerControl.removeLayer(placeNames); placeNamesInMenu = false; }
    }
  } finally { syncingPlaceNames = false; }
}

const startTheme = themes[store.get("theme")] ? store.get("theme") : "Standard";
themes[startTheme].addTo(map);
map.getContainer().dataset.theme = startTheme;
map.on("baselayerchange", e => map.getContainer().dataset.theme = e.name);
if (store.get("markers", true)) layer.addTo(map);   // markers are on unless the user switched them off
for (const name of store.get("overlays", [])) if (name !== MARKERS) overlays[name]?.addTo(map);
const layerControl = L.control.layers(themes, overlays, { position: "topright" }).addTo(map);
syncPlaceNames(startTheme);
map.on("baselayerchange", e => syncPlaceNames(e.name));
L.control.scale({ imperial: false, position: "bottomleft" }).addTo(map);

const legend = $("legend");
const gradient = Array.from({ length: 10 }, (_, i) => {
  const g = Math.round(255 - (255 - SLOPE_SHADE) * slopeAlpha(i / 9 * SLOPE_FULL));   // shade over a white map
  return `rgb(${g},${g},${g}) ${Math.round(i / 9 * 100)}%`;
}).join(", ");
legend.innerHTML = `<div id="leg-pop"><b>Inhabitants per km²</b>
    <div class="grad" id="pop-grad"></div>
    <div class="ticks" id="pop-ticks"></div>
    <label class="op">Strength <input type="range" id="pop-op" min="20" max="100" step="5"
      value="${Math.round(store.get("popOpacity", POP_OPACITY) * 100)}" aria-label="Population layer strength"></label>
    <div class="note" id="pop-note"></div></div>
  <div id="leg-built"><b id="built-title">Built-up share</b>
    <div class="grad" style="width:270px;background: linear-gradient(to right, ${BUILT_SCALE.map((c, i) => `${c} ${Math.round(i / (BUILT_SCALE.length - 1) * 100)}%`).join(", ")})"></div>
    <div class="ticks" style="position:relative;display:block;height:13px;width:270px">${[1, 5, 25, 50, 100].map(p => `<span style="position:absolute;${p === 100 ? "right:0" : `left:${Math.round(Math.sqrt(p / 100) * 100)}%;transform:translateX(-50%)`}">${p} %</span>`).join("")}</div>
    <label class="op">Strength <input type="range" id="built-op" min="20" max="100" step="5"
      value="${Math.round(store.get("builtOpacity", 0.7) * 100)}" aria-label="Built-up strength"></label>
    <div class="note" id="built-note"></div></div>
  <div id="leg-pp"><b>Built-up m² per person</b>
    <div class="grad" style="width:270px;background: linear-gradient(to right, ${PP_SCALE.map((c, i) => `${c} ${Math.round(i / (PP_SCALE.length - 1) * 100)}%`).join(", ")})"></div>
    <div class="ticks" style="position:relative;display:block;height:13px;width:270px">${[20, 50, 100, 200, PP_MAX].map(v => `<span style="position:absolute;${v === PP_MAX ? "right:0" : `left:${Math.round(ppT(v) * 100)}%;transform:translateX(-50%)`}">${v === PP_MAX ? "500+" : v}</span>`).join("")}</div>
    <label class="op">Strength <input type="range" id="pp-op" min="20" max="100" step="5"
      value="${Math.round(store.get("ppOpacity", 0.7) * 100)}" aria-label="Built-up per person strength"></label>
    <div class="note" id="pp-note"></div></div>
  <div id="leg-roads"><b id="road-title">Road lanes</b>
    <div class="seg" id="road-per" style="margin:4px 0;width:270px"><button type="button" data-per="total">Total</button><button type="button" data-per="area">per km²</button><button type="button" data-per="person">per person</button></div>
    <div class="grad" style="width:270px;background: linear-gradient(to right, ${ROAD_SCALE.map((c, i) => `${c} ${Math.round(i / (ROAD_SCALE.length - 1) * 100)}%`).join(", ")})"></div>
    <div class="ticks" id="road-ticks" style="position:relative;display:block;height:13px;width:270px"></div>
    <label class="op">Strength <input type="range" id="road-op" min="20" max="100" step="5"
      value="${Math.round(store.get("roadOpacity", 0.7) * 100)}" aria-label="Road layer strength"></label>
    <div class="note" id="road-note"></div></div>
  <div id="leg-clc"><b>CORINE land cover</b>
    <div class="clc-list">${CLC_CLASSES.map(([c, n]) => `<div class="row"><i class="sw" style="background:rgb(${c})"></i>${n}</div>`).join("")}</div>
    <label class="op">Strength <input type="range" id="clc-op" min="20" max="100" step="5"
      value="${Math.round(store.get("clcOpacity", CLC_OPACITY) * 100)}" aria-label="CORINE strength"></label></div>
  <div id="leg-slope"><b>Slope</b>
    <div class="grad" style="background: linear-gradient(to right, ${gradient})"></div>
    <div class="ticks"><span>0°</span><span>15°</span><span>30°</span><span>${SLOPE_FULL}°+</span></div>
    <label class="op">Strength <input type="range" id="slope-op" min="20" max="100" step="5"
      value="${Math.round(store.get("slopeOpacity", SLOPE_OPACITY) * 100)}" aria-label="Slope overlay strength"></label>
    <div class="note" id="legend-note"></div></div>`;
const slope = overlays["Slope (steepness)"], popLayer = overlays[POPULATION];
// scale: the palette over the chosen range, with the densities at fixed positions below
const popNice = d => {
  const v = d >= 1000 ? d / 1000 : d, r = v >= 100 ? Math.round(v) : Number(v.toPrecision(2));
  return r.toLocaleString(undefined, { maximumFractionDigits: 1 }) + (d >= 1000 ? "k" : "");
};
function renderPopLegend() {
  const stops = Array.from({ length: 13 }, (_, i) => `${popColorAt(i / 12)} ${Math.round(i / 12 * 100)}%`);
  $("pop-grad").style.background = `linear-gradient(to right, ${stops.join(", ")})`;
  const filtered = popRange.min > 0 || popRange.max > 0;
  const ticks = filtered
    ? Array.from({ length: 6 }, (_, i) => [i / 5, popNice(popInv(popLo + popSpan * i / 5))])      // evenly spread over the range
    : [[popT(1000), "1k"], [popT(2500), "2.5k"], [popT(5000), "5k"], [popT(10000), "10k"], [popT(20000), "20k"], [1, "40k+"]];
  if (filtered) {                       // the ends show the chosen limits
    ticks[0][1] = popRange.min > 0 ? popNice(popRange.min) : "0";
    ticks[5][1] = popRange.max > 0 ? popNice(popRange.max) : "40k+";
  }
  $("pop-ticks").innerHTML = ticks.map(([t, label], i) => `<span style="position:absolute;${i === ticks.length - 1 ? "right:0" : t < .02 ? "left:0" : `left:${Math.round(t * 100)}%;transform:translateX(-50%)`}">${label}</span>`).join("");
}
renderPopLegend();
$("slope-op").addEventListener("input", e => {
  const v = e.target.value / 100;
  slope.setOpacity(v);
  store.set("slopeOpacity", v);
});
const BUILT_NAMES = ["Built-up area (GHS)", "Non-residential built-up (GHS)"];
$("built-op").addEventListener("input", e => {
  for (const n of BUILT_NAMES) overlays[n].setOpacity(e.target.value / 100);
  store.set("builtOpacity", e.target.value / 100);
});
$("pp-op").addEventListener("input", e => {
  overlays[PER_PERSON].setOpacity(e.target.value / 100);
  store.set("ppOpacity", e.target.value / 100);
});
$("road-op").addEventListener("input", e => {
  overlays[ROADS].setOpacity(e.target.value / 100);
  store.set("roadOpacity", e.target.value / 100);
  if (typeof roadAdminRender === "function") roadAdminRender();
});
$("road-per").addEventListener("click", e => {
  const b = e.target.closest("button"); if (!b) return;
  roadPer = b.dataset.per; store.set("roadPer", roadPer);
  overlays[ROADS].redraw();
  if (typeof roadAdminRender === "function") roadAdminRender();
  updateLegend(); checkLayerData();
});
function renderRoadLegend() {
  document.querySelectorAll("#road-per button").forEach(b => b.classList.toggle("on", b.dataset.per === roadPer));
  const [lo, hi] = roadRange(), nice = v => Number(v.toPrecision(v < 10 ? 1 : 2));
  const ticks = roadRange() === roadAdminRange ? [0, 1, 2, 3, 4].map(i => nice(lo * (hi / lo) ** (i / 4)))
    : roadPer === "person" ? [1, 3, 10, 30, 100, 300] : [2, 10, 50, 100, 500], last = ticks.at(-1);
  $("road-ticks").innerHTML = ticks.map(v => `<span style="position:absolute;${v === last ? "right:0" : v === ticks[0] ? "left:0" : `left:${Math.round(roadT(v) * 100)}%;transform:translateX(-50%)`}">${v.toLocaleString()}${v === last && roadRange() !== roadAdminRange ? "+" : ""}</span>`).join("");
  const admin = ROAD_LEVELS[roadView];
  $("road-title").textContent = "Road lanes · " + (admin ? admin[0] : "1 km grid");
  $("road-note").textContent = ROAD_UNIT[roadPer] + (roadPer === "total" && !admin ? " per square" : "") + ` · ${roadSel.size} of ${ROAD_TYPES.length} types (Roads)`;
}
$("clc-op").addEventListener("input", e => {
  overlays["CORINE land cover (2018)"].setOpacity(e.target.value / 100);
  store.set("clcOpacity", e.target.value / 100);
});
$("pop-op").addEventListener("input", e => {
  const v = e.target.value / 100;
  popLayer.setOpacity(v);
  store.set("popOpacity", v);
});
const updateLegend = () => {
  const sl = map.hasLayer(slope), pp = map.hasLayer(popLayer), cc = map.hasLayer(overlays["CORINE land cover (2018)"]);
  const bt = BUILT_NAMES.filter(n => map.hasLayer(overlays[n]));
  const pe = map.hasLayer(overlays[PER_PERSON]), rd = map.hasLayer(overlays[ROADS]) || map.hasLayer(roadAdmin);
  legend.classList.toggle("show", sl || pp || cc || pe || rd || bt.length > 0);
  $("leg-roads").hidden = !rd;
  if (rd) renderRoadLegend();
  $("leg-pp").hidden = !pe;
  $("pp-note").textContent = dataInfo.built_id && dataInfo.population_id ? "1 km squares · grey: nobody" : "no data – see Settings";
  $("leg-slope").hidden = !sl; $("leg-pop").hidden = !pp; $("leg-clc").hidden = !cc; $("leg-built").hidden = !bt.length;
  $("built-title").textContent = bt.length === 1 ? (bt[0].startsWith("Non") ? "Non-residential built-up" : "Built-up") + " share of area" : "Built-up share of area";
  $("built-note").textContent = dataInfo.built_id ? "GHS-BUILT-S 2020 · " + dataInfo.built_cell_m + " m cells" : "no data – see Settings";
  $("pop-filter-wrap").hidden = !pp;
  if (!pp) showPopFilter(false);
  $("legend-note").textContent = map.getZoom() < 8 ? "Zoom in to see slope" : "";
  $("pop-note").textContent = map.getZoom() < POP_MIN_ZOOM ? "Zoom in to see the layer"
    : (dataInfo.population || "no data – see Settings") + (popSize ? ` · ${popSize >= 1000 ? popSize / 1000 + " km" : popSize + " m"} squares` : "");
};
// filter button in the footer + small card with from / to
function applyPopRange() {
  const val = id => { const v = parseFloat($(id).value); return v > 0 ? v : null; };
  const min = val("pop-min"), max = val("pop-max"), bad = min != null && max != null && min >= max;
  $("pop-min").classList.toggle("bad", bad); $("pop-max").classList.toggle("bad", bad);
  if (bad) return;
  popRange.min = min; popRange.max = max;
  store.set("popRange", popRange);
  popSetRange(); renderPopLegend(); popLayer.redraw();
  $("pop-filter-btn").classList.toggle("on", min != null || max != null);
}
// grid size: sizes that are a multiple of the data's cell (100 m census: 200 m … 10 km; 1 km grid: 2 km …)
function fillPopSizes() {
  const cell = dataInfo.cell_m || 100;
  const sizes = [0, ...[100, 200, 500, 1000, 2000, 5000, 10000].filter(v => v % cell === 0)];
  $("pop-size").innerHTML = sizes.map(v => `<option value="${v}">${v ? (v >= 1000 ? v / 1000 + " km" : v + " m") : "Auto"}</option>`).join("");
  if (!sizes.includes(popSize)) popSize = 0;
  $("pop-size").value = popSize;
}
fillPopSizes();
$("pop-size").addEventListener("change", e => { popSize = +e.target.value; store.set("popSize", popSize); popLayer.redraw(); updateLegend(); });
$("pop-labels").checked = popLabels;
$("pop-labels").addEventListener("change", e => { popLabels = e.target.checked; store.set("popLabels", popLabels); popLayer.redraw(); });
$("pop-min").value = popRange.min ?? ""; $("pop-max").value = popRange.max ?? "";
$("pop-filter-btn").classList.toggle("on", popRange.min > 0 || popRange.max > 0);
const showPopFilter = on => { $("pop-filter").hidden = !on; };
$("pop-filter-btn").addEventListener("click", () => showPopFilter($("pop-filter").hidden));
let popTimer;
for (const id of ["pop-min", "pop-max"]) $(id).addEventListener("input", () => { clearTimeout(popTimer); popTimer = setTimeout(applyPopRange, 350); });
$("pop-reset").addEventListener("click", () => { $("pop-min").value = $("pop-max").value = ""; applyPopRange(); });
document.addEventListener("click", e => { if (!e.target.closest("#pop-filter-wrap")) showPopFilter(false); });
// a layer that needs a data set which is not installed: say so (instead of an empty map)
const needsData = { "Built-up area (GHS)": () => dataInfo.built_id, "Non-residential built-up (GHS)": () => dataInfo.built_id, [POPULATION]: () => dataInfo.population_id,
  [PER_PERSON]: () => dataInfo.built_id && dataInfo.population_id,
  [ROADS]: () => dataInfo.roads && (roadPer === "area" || dataInfo.population_id) };
function checkLayerData() {
  const missing = Object.keys(needsData).filter(n => map.hasLayer(overlays[n]) && !needsData[n]());
  $("notice").hidden = !missing.length;
  if (missing.length) $("notice").firstElementChild.textContent = `No data for "${missing[0]}" – `;
}
$("notice").lastElementChild.addEventListener("click", () => { $("settings-btn").click(); });
const saveLayers = () => store.set("overlays", Object.keys(overlays).filter(n => n !== MARKERS && map.hasLayer(overlays[n])));
map.on("baselayerchange", e => store.set("theme", e.name));
map.on("overlayadd overlayremove", e => {
  if (e.layer === layer) { store.set("markers", map.hasLayer(layer)); updateMarkerHint(); }
  if (e.layer === placeNames && !syncingPlaceNames) {   // the user ticked / unticked it
    placeNamesWanted = map.hasLayer(placeNames);
    store.set("placeNames", placeNamesWanted);
  }
  saveLayers(); updateLegend(); checkLayerData();
});
map.on("zoomend", updateLegend);
updateLegend();

const color = cat => {   // stable colour per category
  let h = 0; for (const ch of String(cat)) h = (h * 31 + ch.charCodeAt(0)) % 360;
  return `hsl(${h},70%,45%)`;
};

function popup(p) {
  const rows = Object.entries(p.tags || {}).sort().map(([k, v]) => {
    const val = /^https?:\/\//.test(v) ? `<a href="${esc(v)}" target="_blank">${esc(v)}</a>` : esc(v);
    return `<tr><td>${esc(k)}</td><td>${val}</td></tr>`;
  }).join("");
  return `<div class="popup"><b>${esc(p.name ?? "(no name)")}</b><br>
    <small>${esc(p.category ?? "")}</small>
    <div class="scroll"><table>${rows}</table></div>
    <a href="https://www.openstreetmap.org/${p.osm_type}/${p.osm_id}" target="_blank">View on OpenStreetMap</a></div>`;
}
