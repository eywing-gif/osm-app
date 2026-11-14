/* Basics: API address, settings storage, sidebar, the map with its base maps. Plain script: the files share one scope and load in this order (see index.html). */
const API = location.protocol.startsWith("http") ? "" : "http://localhost:8000";
const $ = id => document.getElementById(id);
const esc = s => String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

// settings remembered between starts (may be unavailable, so always guarded)
const store = {
  get(k, d) { try { const v = localStorage.getItem("osmts." + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem("osmts." + k, JSON.stringify(v)); } catch {} },
};

// what the settings made active: labels, start view, which grid / timetable the layers show
let dataInfo = { population_id: "" };
async function loadDataInfo(first) {
  try { dataInfo = await (await fetch(`${API}/datasets/active`)).json(); } catch { return; }
  const names = dataInfo.osm;
  $("local-label").textContent = (names.length ? (names.length > 2 ? `${names.length} regions` : names.join(" & ")) : "no data") + " · fast";
  if (first && !store.get("view", null) && dataInfo.bbox) map.fitBounds([[dataInfo.bbox[1], dataInfo.bbox[0]], [dataInfo.bbox[3], dataInfo.bbox[2]]]);
  if (first && !names.length && !dataInfo.population_id) $("settings-btn").click();     // nothing installed yet
  updateLegend(); checkLayerData();
  fillPopSizes();
  overlays[POPULATION].redraw();      // grid or timetables may have changed: draw the layers again
  for (const n of ["Built-up area (GHS)", "Non-residential built-up (GHS)", PER_PERSON, ROADS]) overlays[n].redraw();
  if (!first && typeof trRefresh === "function") trRefresh();
}
window.dataChanged = () => loadDataInfo(false);

// ---------- sidebar width / visibility ----------
const SB_MIN = 280, SB_DEFAULT = 380;
const sbMax = () => Math.max(SB_MIN, Math.min(720, innerWidth * 0.6));
const sb = { w: SB_DEFAULT, hidden: false, ...store.get("sidebar", {}) };
let sbFrame = 0, mapReady = false;   // mapReady: the map below exists (this runs before it is created)
function applySidebar(save = true) {
  sb.w = Math.round(Math.min(sbMax(), Math.max(SB_MIN, Number(sb.w) || SB_DEFAULT)));
  $("app").style.setProperty("--sw", sb.w + "px");
  $("app").classList.toggle("collapsed", !!sb.hidden);
  $("resizer").setAttribute("aria-valuenow", sb.w);
  if (save) store.set("sidebar", { w: sb.w, hidden: !!sb.hidden });
  if (mapReady) {   // let the map use its new size (once per frame while dragging)
    cancelAnimationFrame(sbFrame);
    sbFrame = requestAnimationFrame(() => map.invalidateSize({ animate: false }));
  }
}
applySidebar(false);

// ---------- map ----------
const view = store.get("view", { center: [52.52, 13.405], zoom: 13 });
const map = L.map("map", { preferCanvas: true }).setView(view.center, view.zoom);
mapReady = true;
map.on("moveend", () => store.set("view", { center: map.getCenter(), zoom: map.getZoom() }));

const OSM_ATTR = '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors';
const OFM_ATTR = '<a href="https://openfreemap.org">OpenFreeMap</a> © <a href="https://www.openmaptiles.org/">OpenMapTiles</a> ' + OSM_ATTR;
const hasWebGL2 = (() => { try { return !!document.createElement("canvas").getContext("webgl2"); } catch { return false; } })();
const themes = {
  "Standard": L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png",
    { maxZoom: 19, attribution: OSM_ATTR }),
  "Terrain (topo)": topoMap(),
  "Satellite": L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
    { maxZoom: 19, maxNativeZoom: 18,
      attribution: 'Imagery © <a href="https://www.esri.com">Esri</a>, Maxar, Earthstar Geographics, and the GIS User Community' }),
  "Black & white": minimalMap("positron", "theme-bw"),
  "Dark": minimalMap("dark", "theme-dark"),
};

// Minimal maps: OpenFreeMap vector styles (free, no API key) drawn with MapLibre.
// MapLibre needs WebGL2; without it, fall back to a recoloured standard map.
function minimalMap(style, fallbackClass) {
  if (hasWebGL2 && L.maplibreGL) {
    return L.maplibreGL({ style: `https://tiles.openfreemap.org/styles/${style}`, attribution: OFM_ATTR });
  }
  return L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png",
    { maxZoom: 19, className: fallbackClass, attribution: OSM_ATTR });
}

// ---------- topo map ----------
// Our own topographic style: the OpenFreeMap vector map recoloured in natural tones, plus shaded relief,
// contour lines (computed in the browser from free elevation tiles) and peaks with their height.
// (Tracestrack Topo, the look on openstreetmap.org, needs a personal API key, so it is not used here.)
const DEM_URL = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png";
let demSource = null;

function topoMap() {
  if (!(hasWebGL2 && L.maplibreGL && window.mlcontour)) {   // no WebGL2: plain raster topo map
    return L.tileLayer("https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png", { maxZoom: 19, maxNativeZoom: 17, subdomains: "abc",
      attribution: OSM_ATTR + ', SRTM | style © <a href="https://opentopomap.org">OpenTopoMap</a> (CC-BY-SA)' });
  }
  const topo = L.maplibreGL({
    style: "https://tiles.openfreemap.org/styles/liberty",
    attribution: OFM_ATTR + ', elevation: <a href="https://github.com/tilezen/joerd/blob/master/docs/attribution.md">Mapzen Terrain Tiles</a>',
  });
  topo.on("add", () => {   // the MapLibre map exists from here on; style it as soon as its base style is loaded
    const gl = topo.getMaplibreMap();
    gl.isStyleLoaded() ? styleTopo(gl) : gl.once("style.load", () => styleTopo(gl));
  });
  return topo;
}

function styleTopo(gl) {
  if (gl.getSource("topo-dem")) return;
  if (!demSource) {
    demSource = new mlcontour.DemSource({ url: DEM_URL, encoding: "terrarium", maxzoom: 13, worker: true });
    demSource.setupMaplibre(maplibregl);
  }
  gl.addSource("topo-dem", { type: "raster-dem", encoding: "terrarium", tiles: [demSource.sharedDemProtocolUrl],
                             maxzoom: 13, tileSize: 256 });
  gl.addSource("topo-contours", { type: "vector", maxzoom: 15, tiles: [demSource.contourProtocolUrl({
    // zoom level -> [thin line every …m, thick labelled line every …m]
    thresholds: { 9: [200, 1000], 10: [100, 500], 11: [50, 250], 12: [20, 100], 13: [10, 50] },
    contourLayer: "contours", elevationKey: "ele", levelKey: "level", extent: 4096, buffer: 1 })] });

  // natural colours (every layer is optional: skip it if the base style ever changes)
  const set = (id, prop, value) => gl.getLayer(id) && gl.setPaintProperty(id, prop, value);
  set("background", "background-color", "#f2efe3");
  set("park", "fill-color", "#d3e3bd");            set("park", "fill-opacity", 0.65);
  set("landcover_wood", "fill-color", "#bcd6a3");  set("landcover_wood", "fill-opacity", 0.55);
  set("landcover_grass", "fill-color", "#dfe9c4"); set("landcover_grass", "fill-opacity", 0.6);
  set("landuse_residential", "fill-color", "#e9e3d6");
  set("water", "fill-color", "#a9d1e6");
  set("waterway_river", "line-color", "#8fc0dd");  set("waterway_other", "line-color", "#8fc0dd");
  set("building", "fill-color", "#e0d8c9");
  set("road_path_pedestrian", "line-color", "#a8532f");   // footpaths: dashed brown-red, as on hiking maps

  // above the land cover, below water, roads and labels
  const under = "waterway_tunnel";
  gl.addLayer({ id: "topo-hillshade", type: "hillshade", source: "topo-dem", paint: {
    "hillshade-exaggeration": 0.5, "hillshade-illumination-direction": 335,
    "hillshade-shadow-color": "#5e5340", "hillshade-highlight-color": "#ffffff", "hillshade-accent-color": "#7c705a" } }, under);
  gl.addLayer({ id: "topo-contour-lines", type: "line", source: "topo-contours", "source-layer": "contours",
    layout: { "line-join": "round" }, paint: {
      "line-color": ["match", ["get", "level"], 1, "#a9824f", "#c2a272"], "line-opacity": 0.85,
      "line-width": ["match", ["get", "level"], 1, 1.0, 0.5] } }, under);

  // labels (above the road names, so they stay readable)
  const labelsFont = ["Noto Sans Regular"];
  gl.addLayer({ id: "topo-contour-labels", type: "symbol", source: "topo-contours", "source-layer": "contours",
    minzoom: 11, filter: [">", ["get", "level"], 0],
    layout: { "symbol-placement": "line", "text-field": ["to-string", ["get", "ele"]], "text-font": labelsFont, "text-size": 10 },
    paint: { "text-color": "#8b6a3a", "text-halo-color": "rgba(242,239,227,0.9)", "text-halo-width": 1.5 } });
  gl.addLayer({ id: "topo-peaks", type: "symbol", source: "openmaptiles", "source-layer": "mountain_peak", minzoom: 11,
    layout: { "icon-image": "mountain_11", "icon-optional": false, "symbol-sort-key": ["coalesce", ["get", "rank"], 99],
      "text-field": ["format", ["coalesce", ["get", "name"], ""], {},
                     ["case", ["has", "ele"], ["concat", "\n", ["get", "ele"], " m"], ""], { "font-scale": 0.85 }],
      "text-font": labelsFont, "text-size": 11, "text-anchor": "top", "text-offset": [0, 0.7], "text-optional": true },
    paint: { "text-color": "#6b4423", "text-halo-color": "rgba(255,255,255,0.85)", "text-halo-width": 1.3 } });
}
