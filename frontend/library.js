/* Sidebar resize, tag library, tool switching, coordinates bar. Plain script: the files share one scope and load in this order (see index.html). */
// ---------- sidebar: drag to resize, hide / show ----------
const handle = $("resizer");
handle.addEventListener("pointerdown", e => {
  e.preventDefault();
  handle.setPointerCapture(e.pointerId);
  handle.classList.add("drag");
  document.body.classList.add("resizing");
});
handle.addEventListener("pointermove", e => {
  if (!handle.hasPointerCapture(e.pointerId)) return;
  sb.w = e.clientX;                // the sidebar starts at the left window edge
  applySidebar(false);
});
const endDrag = e => {
  if (!handle.hasPointerCapture(e.pointerId)) return;
  handle.releasePointerCapture(e.pointerId);
  handle.classList.remove("drag");
  document.body.classList.remove("resizing");
  applySidebar();                  // save
};
handle.addEventListener("pointerup", endDrag);
handle.addEventListener("pointercancel", endDrag);
handle.addEventListener("dblclick", () => { sb.w = SB_DEFAULT; applySidebar(); });
handle.addEventListener("keydown", e => {   // keyboard: ← / → (Shift = bigger steps), Home = default
  const step = e.shiftKey ? 96 : 24;
  if (e.key === "ArrowLeft") sb.w -= step;
  else if (e.key === "ArrowRight") sb.w += step;
  else if (e.key === "Home") sb.w = SB_DEFAULT;
  else return;
  e.preventDefault();
  applySidebar();
});
const setSidebarHidden = hidden => { sb.hidden = hidden; applySidebar(); };
$("sb-close").addEventListener("click", () => setSidebarHidden(true));
$("sb-open").addEventListener("click", () => setSidebarHidden(false));
document.addEventListener("keydown", e => {
  if ((e.ctrlKey || e.metaKey) && !e.shiftKey && !e.altKey && e.key.toLowerCase() === "b") {
    e.preventDefault();
    setSidebarHidden(!sb.hidden);
  }
});
addEventListener("resize", () => applySidebar(false));   // keep the width within the new window size

// ---------- tag library ----------
const lib = { open: false, key: null, q: "", page: 1, hasMore: false, loading: false, token: 0, ctl: null,
              sort: store.get("libSort", "name"), documented: store.get("libDoc", true) };
const fmtCount = n => new Intl.NumberFormat(NUM, { notation: "compact", maximumFractionDigits: 1 }).format(n);

// one tool open at a time (Point info, Transit, Landuse, Tag library): opening one closes the others
const TOOL_CLOSERS = { inspect: () => setInspect(false), transit: () => setTransit(false), landuse: () => setLanduse(false), library: () => closeLibrary() };
function closeOtherTools(keep) {
  for (const [name, close] of Object.entries(TOOL_CLOSERS)) if (name !== keep) close();
  closeSuggest();
  if (drawing) map.pm.disableDraw();
  if ($("app").classList.contains("collapsed")) setSidebarHidden(false);
}
document.addEventListener("keydown", e => {      // Escape closes the open tool
  if (e.key !== "Escape" || drawing || /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName)) return;
  if (inspecting) setInspect(false); else if (tr.on) setTransit(false); else if (lu.on) setLanduse(false); else if (lib.open) closeLibrary();
});

function openLibrary() {
  closeOtherTools("library");
  lib.open = true;
  $("sidebar").classList.add("lib-open");
  $("lib-btn").classList.add("on"); $("lib-btn").setAttribute("aria-expanded", "true");
  $("lib-sort").value = lib.sort; $("lib-doc").checked = lib.documented;
  loadLibrary(true);
  $("lib-q").focus();
}
function closeLibrary() {
  if (!lib.open) return;
  lib.open = false;
  lib.ctl?.abort();
  $("sidebar").classList.remove("lib-open");
  $("lib-btn").classList.remove("on"); $("lib-btn").setAttribute("aria-expanded", "false");
}
$("lib-btn").addEventListener("click", () => lib.open ? closeLibrary() : openLibrary());
$("lib-close").addEventListener("click", closeLibrary);
$("lib-back").addEventListener("click", () => showKeys());

function showKeys() { lib.key = null; lib.q = ""; $("lib-q").value = ""; loadLibrary(true); }
function showValues(key) { lib.key = key; lib.q = ""; $("lib-q").value = ""; loadLibrary(true); $("lib-q").focus(); }

function libHeader(total) {
  const online = source === "online";
  $("lib-back").hidden = !lib.key;
  $("lib-title").textContent = lib.key ? lib.key : "Tag library";
  const what = lib.key ? (total === 1 ? "value" : "values") : (total === 1 ? "key" : "keys");
  $("lib-sub").textContent = (total != null ? `${total.toLocaleString(NUM)} ${what} · ` : "") + (online ? "worldwide (taginfo)" : "your local data");
  $("lib-q").placeholder = lib.key ? `Filter values of ${lib.key}…` : "Filter keys…";
  $("lib-doc-row").hidden = !online;
}

async function loadLibrary(reset) {
  if (reset) { lib.page = 1; lib.hasMore = true; $("lib-list").scrollTop = 0; $("lib-list").innerHTML = ""; libHeader(null); }
  else if (lib.loading || !lib.hasMore) return;
  const token = ++lib.token;
  lib.ctl?.abort();
  const ctl = lib.ctl = new AbortController();
  lib.loading = true;
  const list = $("lib-list");
  list.querySelector(".lib-msg")?.remove();
  list.insertAdjacentHTML("beforeend", `<div class="lib-msg">Loading…</div>`);
  const params = new URLSearchParams({ source, q: lib.q, sort: lib.sort, documented: lib.documented, page: lib.page, per: 100 });
  if (lib.key) params.set("key", lib.key);
  try {
    const res = await fetch(`${API}/taglib/${lib.key ? "values" : "keys"}?${params}`, { signal: ctl.signal });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || res.statusText);
    if (token !== lib.token) return;
    list.querySelector(".lib-msg")?.remove();
    const rows = [];
    if (lib.key && lib.page === 1 && !lib.q) {
      rows.push(`<div class="lr any" data-any="1"><div class="lmain"><div class="ln">${esc(lib.key)}=*</div>
        <div class="ld">any value – everything that has this key</div></div></div>`);
    }
    for (const it of data.items) {
      rows.push(lib.key
        ? `<div class="lr" data-name="${esc(it.name)}"><div class="lmain">
             <div class="ln"><span class="k">${esc(lib.key)}=</span>${esc(it.name)}</div>
             ${it.description ? `<div class="ld">${esc(it.description)}</div>` : ""}</div>
             <span class="lc">${fmtCount(it.count)}</span></div>`
        : `<div class="lr" data-name="${esc(it.name)}"><div class="lmain">
             <div class="ln">${esc(it.name)}</div>
             ${it.values ? `<div class="ld">${it.values.toLocaleString(NUM)} ${it.values === 1 ? "value" : "values"}</div>` : ""}</div>
             <span class="lc">${fmtCount(it.count)}</span><span class="chev">›</span></div>`);
    }
    list.insertAdjacentHTML("beforeend", rows.join(""));
    lib.hasMore = data.has_more;
    lib.page = data.next_page;
    libHeader(data.total);
    if (lib.page === 2 && !data.items.length) {
      list.insertAdjacentHTML("beforeend", `<div class="lib-msg">${lib.q ? `Nothing matches “${esc(lib.q)}”.` : "Nothing here."}</div>`);
    }
  } catch (e) {
    if (e.name === "AbortError") return;
    list.querySelector(".lib-msg")?.remove();
    list.insertAdjacentHTML("beforeend", `<div class="lib-msg error">${esc(e.message)}<button type="button" data-retry="1">Retry</button></div>`);
  } finally {
    if (token === lib.token) {
      lib.loading = false;
      // a short list (e.g. after a filter) may not fill the panel, so there is nothing to scroll: keep loading
      if (lib.hasMore && list.scrollHeight <= list.clientHeight + 40 && !list.querySelector(".error")) loadLibrary(false);
    }
  }
}

$("lib-list").addEventListener("scroll", e => {   // endless list: fetch the next page near the bottom
  const el = e.target;
  if (el.scrollTop + el.clientHeight > el.scrollHeight - 300) loadLibrary(false);
});
$("lib-list").addEventListener("click", e => {
  if (e.target.closest("[data-retry]")) { lib.page = Math.max(1, lib.page); loadLibrary(lib.page === 1); return; }
  const row = e.target.closest(".lr");
  if (!row) return;
  if (!lib.key) return showValues(row.dataset.name);
  $("q").value = `${lib.key}=${row.dataset.any ? "*" : row.dataset.name}`;
  updateClear();
  closeLibrary();
  search();
});
let libTimer;
$("lib-q").addEventListener("input", e => {
  clearTimeout(libTimer);
  libTimer = setTimeout(() => { lib.q = e.target.value.trim(); loadLibrary(true); }, 250);
});
$("lib-sort").addEventListener("change", e => { lib.sort = e.target.value; store.set("libSort", lib.sort); loadLibrary(true); });
$("lib-doc").addEventListener("change", e => { lib.documented = e.target.checked; store.set("libDoc", lib.documented); loadLibrary(true); });
document.querySelectorAll("#lib-source button").forEach(b => b.addEventListener("click", () => {
  if (b.dataset.src === source) return;
  setSource(b.dataset.src);
  lib.key = null; lib.q = ""; $("lib-q").value = "";
  loadLibrary(true);
}));

// ---------- bottom bar: cursor coordinates ----------
const showCoords = ll => $("coords").textContent = `${ll.lat.toFixed(5)}, ${ll.lng.toFixed(5)}`;
showCoords(map.getCenter());                       // until the cursor enters the map
map.on("mousemove", e => showCoords(e.latlng));    // keeps the last position when the cursor leaves
const showZoom = () => $("zoom-info").textContent = `Zoom ${map.getZoom()}`;
showZoom(); map.on("zoomend", showZoom);
$("coords").addEventListener("click", async () => {
  const text = $("coords").textContent;
  try { await navigator.clipboard.writeText(text); } catch { return; }
  $("coords").textContent = "Copied!";
  setTimeout(() => $("coords").textContent = text, 900);
});
