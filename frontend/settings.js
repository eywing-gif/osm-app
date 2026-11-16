/* Settings: download / switch the data sets (map regions, population grids, timetables). Talks to /datasets. */
(() => {
  const $ = id => document.getElementById(id);
  const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const API = location.protocol.startsWith("http") ? "" : "http://localhost:8000";
  const mb = b => b >= 1e9 ? (b / 1e9).toFixed(1) + " GB" : Math.max(1, Math.round(b / 1e6)) + " MB";
  const TABS = { osm: "Map", population: "Population", built: "Built-up", transit: "Transit", app: "App" };
  let app = null, latest = null, data = null, tab = "osm", poll = 0, sel = new Set(), open = new Set(), filter = "", wasRunning = false;

  async function call(method, url, body) {
    const r = await fetch(API + (url.startsWith("/../") ? url.slice(3) : "/datasets" + url), { method, headers: { "Content-Type": "application/json" }, body: body && JSON.stringify(body) });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) { alert(j.detail || r.statusText); throw new Error(j.detail); }
    return j;
  }

  async function load(resetSel) {
    data = await (await fetch(API + "/datasets")).json();
    if (resetSel) sel = new Set(data.osm.built);
    render();
    if (data.job?.state === "running") { wasRunning = true; clearTimeout(poll); poll = setTimeout(() => isOpen() && load(false), 1500); }
    else if (wasRunning) { wasRunning = false; sel = new Set(data.osm.built); render(); }
  }

  const busy = () => data.job?.state === "running";

  function renderJob() {
    const j = data.job, el = $("set-job");
    el.className = "job" + (j ? " show" : "") + (j?.state === "failed" ? " failed" : "");
    if (!j) return;
    el.innerHTML = `<div class="top"><b>${esc(j.title)}</b><span>${j.state === "running" ? j.pct + " %" : j.state}</span></div>
      <div class="bar"><i style="width:${j.pct}%"></i></div>
      <div class="top"><small>${esc(j.text)}</small>${j.state === "running" ? '<button class="btn" data-a="cancel">Cancel</button>' : ""}</div>`;
  }

  // ---- map regions: a tree (continent → country → state), several can be used together
  function osmRows() {
    const kids = {};
    for (const r of data.osm.regions) (kids[r.parent ?? ""] ||= []).push(r);
    const f = filter.trim().toLowerCase();
    const row = (r, depth) => {
      const n = kids[r.id]?.length;
      return `<div class="row" style="margin-left:${depth * 18}px">
        <span class="caret" data-a="toggle" data-id="${esc(r.id)}">${n ? (open.has(r.id) ? "▾" : "▸") : ""}</span>
        <input type="checkbox" data-a="use" data-id="${esc(r.id)}" ${sel.has(r.id) ? "checked" : ""}>
        <span class="name">${esc(r.name)}${f ? `<small>${esc(r.id)}</small>` : ""}</span>
        <span class="size">${r.bytes ? mb(r.bytes) : r.est ? mb(r.est) : ""}</span>
        ${r.bytes ? `<button class="btn" data-a="del" data-id="${esc(r.id)}" ${busy() ? "disabled" : ""}>Delete</button>`
                  : `<button class="btn" data-a="get" data-id="${esc(r.id)}" ${busy() ? "disabled" : ""}>Download</button>`}
      </div>`;
    };
    if (f) return data.osm.regions.filter(r => r.name.toLowerCase().includes(f) || r.id.includes(f)).slice(0, 80).map(r => row(r, 0)).join("") || '<div class="empty">–</div>';
    const walk = (list, depth) => list.map(r => row(r, depth) + (open.has(r.id) && kids[r.id] ? walk(kids[r.id], depth + 1) : "")).join("");
    return walk(kids[""] || [], 0);
  }

  // sizes of the regions on screen that are not known yet: asked from the server, then the list is drawn again
  let sizeAsked = new Set();
  async function loadSizes() {
    const ids = [...document.querySelectorAll('#set-body [data-a="get"]')].map(b => b.dataset.id)
      .filter(i => !sizeAsked.has(i) && !data.osm.regions.find(r => r.id === i).est);
    if (!ids.length) return;
    ids.forEach(i => sizeAsked.add(i));
    const got = await (await fetch(API + "/datasets/osm/sizes?ids=" + ids.join(","))).json();
    for (const r of data.osm.regions) if (got[r.id]) r.est = got[r.id];
    if (tab === "osm") render();
  }

  function renderOsm() {
    const same = sel.size === data.osm.built.length && data.osm.built.every(i => sel.has(i));
    const miss = [...sel].map(i => data.osm.regions.find(r => r.id === i)).filter(r => r && !r.bytes);
    const missing = miss.length, missSize = miss.reduce((a, r) => a + (r.est || 0), 0);
    $("set-body").innerHTML = `<div class="empty" style="padding:0 0 6px">Map data for search, point info, roads. Download adds a region to the map; or tick several, then Apply.</div><input class="search" placeholder="Search region" value="${esc(filter)}" data-a="filter">${osmRows()}`;
    $("set-apply").innerHTML = `<span>${data.osm.built.length ? "Active: " + data.osm.built.map(i => esc(data.osm.regions.find(r => r.id === i)?.name ?? i)).join(", ") : "No map data"}</span>
      ${data.osm.built.length && !data.osm.roads ? `<button class="btn" data-a="roads" title="Needed for Roads and Amenities" ${busy() ? "disabled" : ""}>Prepare analyses</button>` : ""}
      ${data.osm.built.length ? `<button class="btn" data-a="osmupd" title="Newest map data for the regions in use" ${busy() ? "disabled" : ""}>Update</button>` : ""}
      <button class="btn primary" data-a="apply" ${same || !sel.size || busy() ? "disabled" : ""}>Apply${missing ? ` · ${missing} to download${missSize ? " (" + mb(missSize) + ")" : ""}` : ""}</button>`;
  }

  // ---- population grids, timetable feeds: one is active
  function renderList(kind) {
    $("set-body").innerHTML = data[kind].map(d => `<div class="row">
        <span class="name">${esc(d.name)}<small>${esc(d.why)}</small><small>${esc([d.source, d.detail].filter(Boolean).join(" · "))}</small></span>
        <span class="size">${d.mb} MB</span>
        ${d.outdated ? '<span class="tag warn" title="Made by an older version: Reach needs an update">Old</span>' : ""}
        ${d.active ? '<span class="tag">Active</span>' : ""}
        ${d.installed ? (d.active ? "" : `<button class="btn" data-a="use1" data-id="${d.id}">Use</button>`)
                         + `<button class="btn${d.outdated ? " primary" : ""}" data-a="inst" data-id="${d.id}" title="Download the newest version and import it again" ${busy() ? "disabled" : ""}>Update</button>`
                         + `<button class="btn" data-a="rm" data-id="${d.id}" ${busy() ? "disabled" : ""}>Delete</button>`
                      : `<button class="btn primary" data-a="inst" data-id="${d.id}" ${busy() ? "disabled" : ""}>Download</button>`}
      </div>`).join("");
    $("set-apply").innerHTML = "<span>" + ({ transit: "Needs the map data of the same area for real tracks.",
      built: "Shown in the layer menu (top right). CORINE land cover is online there too, no download.", population: "" }[kind] || "") + "</span>";
  }

  // ---- the app: version, system checks, update
  async function loadApp() {
    app = await (await fetch(API + "/app")).json();
    render();
  }

  function renderApp() {
    if (!app) { loadApp(); return; }
    const j = data.job, updated = j?.title.startsWith("App ") && j.state === "done";
    const broken = app.checks.some(c => !c.ok);
    $("set-body").innerHTML = (broken ? `<div class="repair"><b>Something is missing</b>
        <span>Repair installs and starts it. It asks for your password.</span>
        <button class="btn primary" data-a="repair" ${busy() ? "disabled" : ""}>Repair</button></div>` : "")
      + `<div class="row"><span class="name">Version<small>${esc(app.mode === "git" ? "git checkout" : "zip install")} · ${esc(app.repo)}</small></span>
        <b>${esc(app.version)}</b>
        ${updated ? '<button class="btn primary" data-a="restart">Restart</button>'
          : latest?.newer ? `<button class="btn primary" data-a="update" data-id="${esc(latest.latest)}" ${busy() ? "disabled" : ""}>Update to ${esc(latest.latest)}</button>`
          : `<button class="btn" data-a="check">Check</button>`}
      </div>
      ${latest ? `<div class="row"><span class="name">${esc(latest.error || (latest.newer ? "New version " + latest.latest : "Up to date"))}</span></div>` : ""}
      ${app.checks.map(c => `<div class="row"><span class="tag" style="${c.ok ? "" : "background:#fee2e2;color:#b91c1c"}">${c.ok ? "ok" : "missing"}</span>
        <span class="name">${esc(c.name)}</span></div>`).join("")}`;
    $("set-apply").innerHTML = "";
  }

  async function check() {
    const r = await fetch(API + "/app/latest"), j = await r.json();
    latest = r.ok ? j : { error: j.detail };
    render();
  }

  function render() {
    if (!data) return;
    document.querySelectorAll("#settings .tabs button").forEach(b => b.classList.toggle("on", b.dataset.tab === tab));
    const keep = $("set-body").scrollTop, focus = document.activeElement?.dataset?.a === "filter";
    if (tab === "osm") setTimeout(loadSizes, 0);
    tab === "osm" ? renderOsm() : tab === "app" ? renderApp() : renderList(tab);
    $("set-body").scrollTop = keep;
    if (focus) { const s = $("set-body").querySelector(".search"); s.focus(); s.setSelectionRange(s.value.length, s.value.length); }
    renderJob();
  }

  async function act(el) {
    const a = el.dataset.a, id = el.dataset.id;
    if (a === "toggle") { open.has(id) ? open.delete(id) : open.add(id); return render(); }
    if (a === "use") { el.checked ? sel.add(id) : sel.delete(id); return render(); }
    if (a === "check") return check();
    if (a === "repair") { await call("POST", "/../app/repair"); load(false); return watchJob(); }
    if (a === "update") { await call("POST", "/../app/update", { tag: id }); return load(false); }
    if (a === "restart") {
      await fetch(API + "/app/restart", { method: "POST" });
      $("set-body").innerHTML = '<div class="empty">Restarting …</div>';
      return setTimeout(() => location.reload(), 4000);
    }
    if (a === "cancel") await call("POST", "/job/cancel");
    else if (a === "get") await call("POST", "/osm/build", { regions: [...new Set([...data.osm.built, id])] });   // download and build with the regions in use
    else if (a === "apply") await call("POST", "/osm/build", { regions: [...sel] });
    else if (a === "roads") await call("POST", "/osm/roads");
    else if (a === "osmupd") { if (!confirm("Download the newest map data and build it again?")) return; await call("POST", "/osm/update", { regions: data.osm.built }); }
    else if (a === "del") { if (!confirm("Delete the download?")) return; await call("DELETE", "/osm/" + id); }
    else if (a === "inst") await call("POST", `/${tab}/${id}/install`);
    else if (a === "use1") { await call("POST", `/${tab}/${id}/activate`); window.dataChanged?.(); }
    else if (a === "rm") { if (!confirm("Delete?")) return; await call("DELETE", `/${tab}/${id}`); window.dataChanged?.(); }
    else return;
    load(false);
    watchJob();
  }

  // the running job, also while Settings is closed: a message when it is finished
  const isOpen = () => $("settings").classList.contains("open");
  let watching = false, jobWasRunning = false;
  async function watchJob() {
    if (watching) return;
    watching = true;
    try {
      for (;;) {
        const j = await (await fetch(API + "/datasets/job")).json().catch(() => null);
        if (j?.state === "running") { jobWasRunning = true; await new Promise(r => setTimeout(r, 2000)); continue; }
        if (jobWasRunning && j) {
          jobWasRunning = false;
          toast(j.state === "done" ? `✓ ${j.title} – ready` : `${j.title} – ${j.state}${j.state === "failed" ? ": " + j.text : ""}`, j.state !== "done");
          if (j.title === "Repair") app = null;      // check again
          window.dataChanged?.();
          if (isOpen()) load(false);
        }
        break;
      }
    } finally { watching = false; }
  }
  function toast(text, bad) {
    let t = $("toast");
    if (!t) { t = document.createElement("div"); t.id = "toast"; t.setAttribute("role", "status"); document.body.append(t); }
    t.className = bad ? "bad" : "";
    t.innerHTML = `<span>${esc(text)}</span><button type="button" aria-label="Close">×</button>`;
    t.hidden = false;
    t.lastElementChild.onclick = () => { t.hidden = true; };
    clearTimeout(t.timer);
    if (!bad) t.timer = setTimeout(() => { t.hidden = true; }, 15000);
  }

  document.addEventListener("DOMContentLoaded", () => {
    $("settings").innerHTML = `<div class="panel" role="dialog" aria-label="Settings">
      <header><h2>Settings</h2><button class="btn" data-a="close" aria-label="Close">×</button></header>
      <div class="tabs">${Object.entries(TABS).map(([k, v]) => `<button data-tab="${k}">${v}</button>`).join("")}</div>
      <div class="body" id="set-body"></div><div class="apply" id="set-apply"></div><div class="job" id="set-job"></div></div>`;
    const s = $("settings");
    s.addEventListener("click", e => {
      if (e.target === s || e.target.closest('[data-a="close"]')) return close();
      const t = e.target.closest("[data-tab]");
      if (t) { tab = t.dataset.tab; return render(); }
      const b = e.target.closest("[data-a]");
      if (b && b.tagName !== "INPUT" && b.dataset.a !== "filter") act(b);
    });
    s.addEventListener("change", e => { if (e.target.dataset.a === "use") act(e.target); });
    s.addEventListener("input", e => { if (e.target.dataset.a === "filter") { filter = e.target.value; render(); } });
    document.addEventListener("keydown", e => { if (e.key === "Escape" && s.classList.contains("open")) close(); });
    $("settings-btn").addEventListener("click", () => { s.classList.add("open"); load(true); });
    watchJob();          // a job may still be running from before (e.g. the window was reloaded)
    // something missing on this computer (database not running, …): open Settings → App with the Repair button
    fetch(API + "/app").then(r => r.json()).then(info => {
      if (info.checks.some(c => !c.ok)) { app = info; tab = "app"; s.classList.add("open"); load(true); }
    }).catch(() => {});
  });
  function close() { $("settings").classList.remove("open"); }
})();
