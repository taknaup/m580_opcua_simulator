"use strict";

// Row layout from /api/tags
const P = 0, TYPE = 1, VAL = 2, WR = 3, ACC = 4, CMT = 5, CTX = 6, INIT = 7, ADDR = 8, TS = 9, SRC = 10;
const SRC_LABEL = { web: "web UI", opcua: "OPC UA client", reset: "reset", plc: "PLC" };

const $ = (id) => document.getElementById(id);
const state = {
  rows: [], byPath: new Map(), view: [], generation: -1,
  filter: { text: "", access: "all", type: "", changed: false },
  rendered: new Map(),           // entry key -> row element
  rowH: 52,
  mode: loadPref("view", "table"),  // "table" | "tree"
  folders: {},                   // structure path -> [type name, comment]
  tree: null,
  expanded: new Set(),           // tree folders opened by the user
  collapsedWhileFiltered: new Set(),
  filterSig: "",
  hay: new Map(),                // path -> lowercase search text
  sort: (() => {
    const [col, dir] = loadPref("sort", "tag:1").split(":");
    return { col, dir: dir === "-1" ? -1 : 1 };
  })(),
};

function loadPref(key, dflt) {
  try { return localStorage.getItem("m580sim." + key) || dflt; } catch { return dflt; }
}
function savePref(key, value) {
  try { localStorage.setItem("m580sim." + key, value); } catch { /* storage unavailable */ }
}

// ------------------------------------------------------------------ helpers
function toast(msg, isErr = false) {
  const el = document.createElement("div");
  el.className = "toast" + (isErr ? " err" : "");
  el.textContent = msg;
  $("toasts").appendChild(el);
  setTimeout(() => el.remove(), isErr ? 6000 : 3000);
}

// Copy text to the clipboard. navigator.clipboard only exists on secure pages (HTTPS or
// localhost); the UI is usually served over plain HTTP, so fall back to copying from a
// hidden text field, which browsers allow during a click.
async function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    try { await navigator.clipboard.writeText(text); return true; } catch { /* fall through */ }
  }
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.setAttribute("readonly", "");
  ta.style.cssText = "position:fixed;top:0;left:0;width:1px;height:1px;opacity:0;";
  // inside an open modal dialog only its own elements can take focus
  (document.querySelector("dialog[open]") || document.body).appendChild(ta);
  const prev = document.activeElement;
  ta.focus();
  ta.select();
  ta.setSelectionRange(0, text.length);
  let ok = false;
  try { ok = document.execCommand("copy"); } catch { ok = false; }
  ta.remove();
  if (prev && prev.focus) prev.focus();
  return ok;
}

async function api(url, opts = {}) {
  const res = await fetch(url, opts);
  let body = null;
  try { body = await res.json(); } catch { /* empty */ }
  if (!res.ok) throw new Error((body && body.detail) || `${res.status} ${res.statusText}`);
  return body;
}

function fmt(row, v = row[VAL]) {
  if (typeof v === "boolean") return v ? "TRUE" : "FALSE";
  if (row[TYPE] === "REAL" || row[TYPE] === "LREAL") {
    if (!Number.isFinite(v)) return String(v);
    const s = Number(v).toPrecision(row[TYPE] === "REAL" ? 7 : 15);
    return String(parseFloat(s));
  }
  if (row[TYPE] === "TIME") return `${v} ms`;
  return String(v);
}

function timeStr(ts) {
  return ts ? new Date(ts * 1000).toLocaleTimeString() : "";
}

// ------------------------------------------------------------------ status
const PLC_STATE = {
  connecting: "connecting…", "loading dictionary": "reading data dictionary…",
  connected: "connected", disconnected: "disconnected", "no dictionary": "no data dictionary",
};

async function refreshStatus() {
  const s = await api("/api/status");
  state.status = s;
  $("endpoint").textContent = s.opcua.endpoint_hint;
  setSessions(s.opcua.sessions);
  const p = s.program;
  const plcMode = s.mode === "plc";
  renderPlc(s);

  $("resetBtn").hidden = plcMode;
  $("resetBtn").disabled = !p;
  $("snapBtn").disabled = !p;
  $("loadLabel").textContent = plcMode ? (s.export ? "Replace comments file" : "Load comments file") : "Load program";
  $("loadLabel").title = plcMode
    ? "Optional .ZEF/.XEF: adds comments and applies R/O / R/W from them"
    : "Serve the HMI variables of a .ZEF/.XEF export";

  if (s.loading && !p) {
    $("busy").hidden = false;
    $("busyText").textContent = "Loading saved program…";
  } else if ($("busyText").textContent === "Loading saved program…") {
    $("busy").hidden = true;
  }
  if (plcMode) {
    const st = s.plc.state;
    $("emptyTitle").textContent = st === "disconnected" ? "PLC not connected"
      : st === "no dictionary" ? "PLC has no data dictionary" : "Connecting to the PLC…";
    $("emptyText").textContent = st === "disconnected"
      ? `${s.plc.host}:${s.plc.port} — ${s.plc.error || "retrying"}. Retrying automatically.`
      : st === "no dictionary" ? `${s.plc.host}:${s.plc.port} — ${s.plc.error}`
      : `${s.plc.host}:${s.plc.port} — ${PLC_STATE[st] || st}`;
  } else {
    $("emptyTitle").textContent = "Load a Control Expert program";
    $("emptyText").innerHTML = "Drop a <b>.ZEF</b> or <b>.XEF</b> export here, or use <b>Load program</b>.<br>" +
      "Variables with <i>HMI</i> exposure are served over OPC UA.<br>" +
      "Or use <b>PLC</b> to serve live values from a PLC simulator.";
  }
  if (!p) {
    $("project").textContent = s.loading ? "Loading saved program…" : plcMode ? "Waiting for the PLC" : "No program loaded";
    $("warnings").hidden = true;
    return s;
  }
  const c = p.access_counts;
  if (plcMode) {
    const access = s.overlay
      ? `${c["R/W"].toLocaleString()} R/W, ${c["R/O"].toLocaleString()} R/O` +
        (c.default ? `, ${c.default.toLocaleString()} without prefix = ${s.default_access}` : "")
      : "all writable, no comments file";
    $("project").textContent = `${p.name} · live from PLC ${s.plc.host} · ${p.tags.toLocaleString()} tags (${access})`;
    $("project").title = p.product;
  } else {
    $("project").textContent =
      `${p.name} v${p.version} · ${p.plc} · ${p.hmi_variables.toLocaleString()} HMI variables → ` +
      `${p.tags.toLocaleString()} tags (${c["R/W"].toLocaleString()} R/W, ${c["R/O"].toLocaleString()} R/O` +
      (c.default ? `, ${c.default.toLocaleString()} unprefixed = ${s.default_access}` : "") + `)`;
    $("project").title = `${p.file} — ${p.product}`;
  }
  const w = $("warnings");
  if (p.warning_count) {
    w.hidden = false;
    $("warnToggle").textContent = `${p.warning_count} import warning${p.warning_count > 1 ? "s" : ""}`;
    $("warnList").replaceChildren(...p.warnings.map((t) => Object.assign(document.createElement("li"), { textContent: t })));
  } else {
    w.hidden = true;
  }
  return s;
}

function renderPlc(s) {
  const plc = s.plc;
  const dot = $("plcDot");
  dot.className = "dot" + (!plc ? "" : plc.state === "connected" ? " live" : plc.state === "disconnected" ? " bad" : " warn");
  $("plcLabel").textContent = plc
    ? `PLC ${plc.host}` + (plc.state === "connected"
      ? ` · ${plc.watched.toLocaleString()} watched · ${Math.max(plc.update_period, plc.scan_time).toFixed(1)}s`
      : ` · ${PLC_STATE[plc.state] || plc.state}`)
    : "PLC: off";
  $("plcBtn").title = plc && plc.state === "connected"
    ? `Watched tags (subscribed, read by a client, on screen or just written): ${plc.watched.toLocaleString()} ` +
      `in ${plc.watched_requests} request${plc.watched_requests === 1 ? "" : "s"}, refreshed every ` +
      `${Math.max(plc.update_period, plc.scan_time).toFixed(1)} s.\n` +
      `All other tags: refreshed in the background about every ${plc.full_refresh} s ` +
      `(${plc.background_requests} request${plc.background_requests === 1 ? "" : "s"} per cycle).`
    : "Connect to a PLC or PLC simulator over UMAS";

  const b = $("plcBanner");
  if (plc && plc.state === "disconnected" && s.program) {
    b.hidden = false;
    b.className = "banner err";
    b.textContent = `PLC ${plc.host}:${plc.port} disconnected — showing last values (OPC UA status BadNotConnected). ${plc.error || ""} Retrying…`;
  } else if (plc && plc.state !== "connected" && s.program) {
    b.hidden = false;
    b.className = "banner";
    b.textContent = `PLC ${plc.host}:${plc.port}: ${PLC_STATE[plc.state] || plc.state}`;
  } else {
    b.hidden = true;
  }
  document.body.classList.toggle("stale", !!(plc && plc.state !== "connected"));

  const ob = $("overlayBanner");
  if (s.mode === "plc" && s.export) {
    const o = s.overlay;
    ob.hidden = false;
    let text = `Comments from ${s.export.file}`;
    if (o) {
      text += `: ${o.matched.toLocaleString()} tags matched`;
      if (o.plc_only) text += `, ${o.plc_only.toLocaleString()} PLC tags not in the file (writable, no comment)`;
      if (o.export_only) text += `, ${o.export_only.toLocaleString()} file tags not in the PLC`;
      if (o.name_mismatch) text += ` — file is for project ${o.name}`;
    }
    $("overlayText").textContent = text;
  } else {
    ob.hidden = true;
  }
}

function openPlcDialog() {
  const plc = state.status && state.status.plc;
  const last = (state.status && state.status.plc_last) || {};
  const src = plc || last;
  $("plcHost").value = src.host || "";
  $("plcPort").value = src.port || 502;
  $("plcUnit").value = src.unit ?? 1;
  $("plcDisconnect").hidden = !plc;
  $("plcDialog").showModal();
  $("plcHost").focus();
}

async function connectPlc(e) {
  e.preventDefault();
  try {
    await api("/api/plc", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ host: $("plcHost").value.trim(), port: Number($("plcPort").value), unit: Number($("plcUnit").value) }),
    });
    $("plcDialog").close();
    toast(`Connecting to ${$("plcHost").value.trim()}…`);
    await refreshStatus();
    await loadTags();
  } catch (err) {
    toast(err.message, true);
  }
}

function setSessions(n) {
  $("sessions").textContent = `${n} client${n === 1 ? "" : "s"}`;
  $("sessDot").classList.toggle("live", n > 0);
}

// ------------------------------------------------------------------ data
async function loadTags() {
  const data = await api("/api/tags");
  state.generation = data.generation;
  state.rows = data.rows;
  state.byPath = new Map(state.rows.map((r) => [r[P], r]));
  state.folders = data.folders || {};
  state.tree = buildTree(state.rows);
  state.hay = new Map(state.rows.map((r) => {
    const parentTypes = splitPath(r[P]).slice(0, -1).map((_, i, a) => (state.folders[joinPath(a.slice(0, i + 1))] || [""])[0]);
    return [r[P], (r[P] + " " + r[CMT] + " " + r[CTX].join(" ") + " " + r[ADDR] + " " + parentTypes.join(" ")).toLowerCase()];
  }));
  const types = [...new Set(state.rows.map((r) => r[TYPE]))].sort();
  const sel = $("typeSel");
  const cur = sel.value;
  sel.replaceChildren(new Option("All types", ""), ...types.map((t) => new Option(t, t)));
  sel.value = types.includes(cur) ? cur : "";
  const has = state.rows.length > 0;
  const diagOpen = document.body.classList.contains("diag-open");
  $("empty").hidden = has || diagOpen;
  $("table").hidden = !has || diagOpen;
  $("toolbar").hidden = !has;
  clearRendered();
  applyFilter();
}

function filterActive() {
  const f = state.filter;
  return !!(f.text.trim() || f.access !== "all" || f.type || f.changed);
}

function applyFilter() {
  const f = state.filter;
  const terms = f.text.toLowerCase().split(/\s+/).filter(Boolean);
  const matches = state.rows.filter((r) => {
    if (f.access === "rw" && !r[WR]) return false;
    if (f.access === "ro" && r[WR]) return false;
    if (f.type && r[TYPE] !== f.type) return false;
    if (f.changed && r[VAL] === r[INIT]) return false;
    if (!terms.length) return true;
    const hay = state.hay.get(r[P]);
    return terms.every((t) => hay.includes(t));
  });
  const sig = JSON.stringify(f);
  if (sig !== state.filterSig) { state.filterSig = sig; state.collapsedWhileFiltered.clear(); }
  matches.sort((a, b) => rowCompare(a, b, a[P], b[P]));
  state.matches = matches;
  $("count").textContent = `Showing ${matches.length.toLocaleString()} of ${state.rows.length.toLocaleString()}`;
  rebuildView();
}

// ------------------------------------------------------------------ sorting
const natural = new Intl.Collator(undefined, { numeric: true, sensitivity: "base" });

function valueKey(v) {          // booleans, then numbers, then text
  if (typeof v === "boolean") return [0, v ? 1 : 0];
  if (typeof v === "number") return [1, v];
  return [2, String(v)];
}

function compareValues(a, b) {
  const [ka, va] = valueKey(a), [kb, vb] = valueKey(b);
  if (ka !== kb) return ka - kb;
  return ka === 2 ? natural.compare(va, vb) : va - vb;
}

// compare two rows (table) or two tree nodes (tree) on the current sort column
function rowCompare(a, b, nameA, nameB) {
  const { col, dir } = state.sort;
  let c = 0;
  if (col === "type") c = natural.compare(a[TYPE], b[TYPE]);
  else if (col === "value") c = compareValues(a[VAL], b[VAL]);
  else if (col === "access") c = (a[WR] ? 1 : 0) - (b[WR] ? 1 : 0);   // R/O before R/W, like the labels
  else if (col === "comment") c = emptyLast(a[CMT], b[CMT]);
  return dir * (c || natural.compare(nameA, nameB));
}

function emptyLast(a, b) {
  if (!a !== !b) return a ? -1 : 1;
  return natural.compare(a || "", b || "");
}

function nodeCompare(a, b) {
  if (!a.row !== !b.row) return a.row ? 1 : -1;            // folders before tags
  if (a.row) return rowCompare(a.row, b.row, a.name, b.name);
  const { col, dir } = state.sort;
  const [ta, ca] = state.folders[a.key] || ["", ""];
  const [tb, cb] = state.folders[b.key] || ["", ""];
  let c = 0;
  if (col === "type") c = natural.compare(ta, tb);
  else if (col === "comment") c = emptyLast(ca, cb);
  return dir * (c || natural.compare(a.name, b.name));
}

function setSort(col) {
  state.sort = state.sort.col === col ? { col, dir: -state.sort.dir } : { col, dir: 1 };
  savePref("sort", `${state.sort.col}:${state.sort.dir}`);
  renderSortHeader();
  applyFilter();
}

function renderSortHeader() {
  for (const th of $("thead").children) {
    if (th.dataset.col === state.sort.col) th.setAttribute("aria-sort", state.sort.dir > 0 ? "ascending" : "descending");
    else th.removeAttribute("aria-sort");
  }
}

function rebuildView() {
  state.view = state.mode === "tree" && state.tree
    ? flattenTree(new Set(state.matches.map((r) => r[P])))
    : state.matches.map((row) => ({ k: row[P], row, depth: -1 }));
  $("spacer").style.height = `${state.view.length * state.rowH}px`;
  $("thName").textContent = state.mode === "tree" ? "Name" : "Tag";
  $("treeTools").hidden = state.mode !== "tree";
  clearRendered();
  render();
}

// ------------------------------------------------------------------ tree
function splitPath(path) {           // 'A.B[1,2].C' -> ['A', 'B', '[1,2]', 'C']
  const out = [];
  for (const seg of path.split(".")) {
    const i = seg.indexOf("[");
    if (i < 0) { out.push(seg); continue; }
    if (i > 0) out.push(seg.slice(0, i));
    out.push(...seg.slice(i).match(/\[[^\]]*\]/g));
  }
  return out;
}

function joinPath(parts) {
  return parts.reduce((acc, p) => (acc && !p.startsWith("[") ? acc + "." + p : acc + p), "");
}

function buildTree(rows) {
  const root = { key: "", children: [], map: new Map(), leaves: 0 };
  for (const row of rows) {
    let node = root;
    const parts = splitPath(row[P]);
    parts.forEach((name, i) => {
      let child = node.map.get(name);
      if (!child) {
        child = { key: joinPath(parts.slice(0, i + 1)), name, children: [], map: new Map(), leaves: 0, row: null };
        node.map.set(name, child);
        node.children.push(child);
      }
      node.leaves++;
      node = child;
    });
    node.row = row;
  }
  return root;
}

function flattenTree(matchSet) {
  const filtering = filterActive();
  const count = (node) => {
    if (node.row) return (node.hits = matchSet.has(node.row[P]) ? 1 : 0);
    let c = 0;
    for (const ch of node.children) c += count(ch);
    return (node.hits = c);
  };
  count(state.tree);
  const out = [];
  const walk = (node, depth) => {
    const children = node.children.filter((ch) => ch.hits).sort(nodeCompare);
    for (const ch of children) {
      if (ch.row) { out.push({ k: ch.row[P], row: ch.row, node: ch, depth }); continue; }
      const open = filtering ? !state.collapsedWhileFiltered.has(ch.key) : state.expanded.has(ch.key);
      out.push({ k: "f:" + ch.key, node: ch, depth, open });
      if (open) walk(ch, depth + 1);
    }
  };
  walk(state.tree, 0);
  return out;
}

function toggleFolder(key, open) {
  if (filterActive()) {
    if (open) state.collapsedWhileFiltered.delete(key); else state.collapsedWhileFiltered.add(key);
  } else if (open) {
    state.expanded.add(key);
  } else {
    state.expanded.delete(key);
  }
  rebuildView();
  const el = state.rendered.get("f:" + key);
  if (el) el.querySelector(".twisty").focus();
}

function setAllFolders(open) {
  const keys = [];
  const walk = (n) => { for (const ch of n.children) if (!ch.row) { keys.push(ch.key); walk(ch); } };
  walk(state.tree);
  if (filterActive()) {
    state.collapsedWhileFiltered = open ? new Set() : new Set(keys);
  } else {
    state.expanded = open ? new Set(keys) : new Set();
  }
  rebuildView();
}

function setMode(mode) {
  state.mode = mode;
  savePref("view", mode);
  for (const b of $("viewSeg").children) b.setAttribute("aria-checked", String(b.dataset.v === mode));
  $("scroller").scrollTop = 0;
  rebuildView();
}

function buildFolder(e) {
  const node = e.node;
  const [typeName, comment] = state.folders[node.key] || ["", ""];
  const el = document.createElement("div");
  el.className = "row folder";
  el.dataset.key = node.key;

  const name = document.createElement("div");
  name.className = "cell c-path";
  name.style.paddingLeft = `${e.depth * 18}px`;
  const tw = document.createElement("button");
  tw.className = "twisty";
  tw.setAttribute("aria-expanded", String(e.open));
  tw.setAttribute("aria-label", `${e.open ? "Collapse" : "Expand"} ${node.key}`);
  tw.innerHTML = '<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true"><path d="M6 4l4 4-4 4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  const label = Object.assign(document.createElement("span"), { className: "fname", textContent: node.name });
  tw.appendChild(label);
  tw.addEventListener("click", (ev) => { ev.stopPropagation(); toggleFolder(node.key, !e.open); });
  tw.addEventListener("keydown", (ev) => {
    if (ev.key === "ArrowRight" && !e.open) { ev.preventDefault(); toggleFolder(node.key, true); }
    if (ev.key === "ArrowLeft" && e.open) { ev.preventDefault(); toggleFolder(node.key, false); }
  });
  const n = filterActive() ? node.hits : node.leaves;
  const meta = Object.assign(document.createElement("div"), {
    className: "fmeta", textContent: `${n.toLocaleString()} tag${n === 1 ? "" : "s"}`,
  });
  name.title = node.key;
  name.append(tw, meta);

  const type = Object.assign(document.createElement("div"), { className: "cell c-type type", textContent: typeName });
  type.title = typeName;
  const value = Object.assign(document.createElement("div"), { className: "cell c-value" });
  const access = Object.assign(document.createElement("div"), { className: "cell c-access" });
  const cmt = document.createElement("div");
  cmt.className = "cell c-comment";
  cmt.appendChild(Object.assign(document.createElement("div"), { className: "comment folder-comment", textContent: comment, title: comment }));
  el.append(name, type, value, access, cmt);
  el.addEventListener("click", (ev) => { if (!ev.target.closest("button, input")) toggleFolder(node.key, !e.open); });
  return el;
}

// ------------------------------------------------------------------ virtual table
function clearRendered() {
  for (const el of state.rendered.values()) el.remove();
  state.rendered.clear();
}

function render() {
  const sc = $("scroller");
  const first = Math.max(0, Math.floor(sc.scrollTop / state.rowH) - 8);
  const last = Math.min(state.view.length, Math.ceil((sc.scrollTop + sc.clientHeight) / state.rowH) + 8);
  const keep = new Set();
  for (let i = first; i < last; i++) {
    const e = state.view[i];
    keep.add(e.k);
    let el = state.rendered.get(e.k);
    if (!el) {
      el = e.row ? buildRow(e.row, e.depth) : buildFolder(e);
      state.rendered.set(e.k, el);
      $("spacer").appendChild(el);
    }
    el.style.top = `${i * state.rowH}px`;
  }
  for (const [path, el] of state.rendered) {
    if (!keep.has(path) && !el.contains(document.activeElement)) {
      el.remove();
      state.rendered.delete(path);
    }
  }
  scheduleWatch();
}

// tell the server which tags are on screen; in PLC mode those are refreshed every period
let watchTimer = null, lastWatch = "";
function scheduleWatch() {
  clearTimeout(watchTimer);
  watchTimer = setTimeout(sendWatch, 250);
}
function sendWatch(force = false) {
  if (!ws || ws.readyState !== 1) return;
  const paths = [...state.rendered.keys()].filter((k) => !k.startsWith("f:"));
  if (state.detail) paths.push(state.detail.path);
  const sig = paths.join("\n");
  if (!force && sig === lastWatch) return;
  lastWatch = sig;
  ws.send(JSON.stringify({ type: "watch", paths }));
}

function buildRow(row, depth = -1) {
  const el = document.createElement("div");
  el.className = "row" + (state.detail && state.detail.path === row[P] ? " selected" : "");
  el.dataset.path = row[P];

  const path = document.createElement("div");
  path.className = "cell c-path";
  path.title = `${row[P]} — click for details`;
  path.tabIndex = 0;
  path.setAttribute("role", "button");
  path.setAttribute("aria-label", `Details for ${row[P]}`);
  const dot = row[P].search(/[.[]/);
  if (depth >= 0) {
    // tree leaf: just its own name, indented under its folder
    path.classList.add("leaf");
    path.style.paddingLeft = `${depth * 18 + 22}px`;
    const parts = splitPath(row[P]);
    path.appendChild(Object.assign(document.createElement("div"), { className: "path", textContent: parts[parts.length - 1] }));
  } else if (dot > 0) {
    // structured tags: top-level variable on line 1, member path on line 2
    path.append(
      Object.assign(document.createElement("div"), { className: "path-root", textContent: row[P].slice(0, dot) }),
      Object.assign(document.createElement("div"), { className: "path", textContent: row[P].slice(dot) }));
  } else {
    path.appendChild(Object.assign(document.createElement("div"), { className: "path", textContent: row[P] }));
  }
  if (row[ADDR]) path.appendChild(Object.assign(document.createElement("div"), { className: "addr", textContent: row[ADDR] }));

  const type = Object.assign(document.createElement("div"), { className: "cell c-type type", textContent: row[TYPE] });

  const value = document.createElement("div");
  value.className = "cell c-value";

  const access = document.createElement("div");
  access.className = "cell c-access";
  const badge = document.createElement("span");
  badge.className = "badge " + (row[WR] ? "rw" : "ro") + (row[ACC] === "default" ? " def" : "");
  badge.textContent = row[WR] ? "R/W" : "R/O";
  badge.title = row[ACC] === "default"
    ? "No R/O or R/W prefix in the comment — using the default access"
    : `From comment prefix ${row[ACC]}`;
  access.appendChild(badge);

  const comment = document.createElement("div");
  comment.className = "cell c-comment";
  const c1 = Object.assign(document.createElement("div"), { className: "comment", textContent: row[CMT] || "—" });
  const ctx = row[CTX].join(" › ");
  c1.title = ctx ? `${row[CMT]}\n${ctx}` : row[CMT];
  comment.appendChild(c1);
  if (ctx) comment.appendChild(Object.assign(document.createElement("div"), { className: "context", textContent: ctx }));

  el.append(path, type, value, access, comment);
  fillValue(el, row);
  return el;
}

function sourceLine(row) {
  if (!row[TS]) return null;
  const s = document.createElement("span");
  s.className = "src";
  s.textContent = `${SRC_LABEL[row[SRC]] || row[SRC]} · ${timeStr(row[TS])}`;
  return s;
}

function fillValue(el, row) {
  const cell = el.querySelector(".c-value");
  const isBool = typeof row[VAL] === "boolean";
  const src = sourceLine(row);

  if (!row[WR]) {
    const v = document.createElement("div");
    v.className = "val-ro" + (isBool ? (row[VAL] ? " bool-true" : " bool-false") : "");
    v.textContent = fmt(row);
    cell.replaceChildren(v, ...(src ? [src] : []));
    return;
  }

  if (isBool) {
    let t = cell.querySelector(".toggle");
    if (!t) {
      t = document.createElement("button");
      t.className = "toggle";
      t.setAttribute("role", "switch");
      t.innerHTML = '<span class="track"></span><span class="lbl"></span>';
      t.addEventListener("click", () => write(row[P], !state.byPath.get(row[P])[VAL]));
    }
    t.setAttribute("aria-checked", String(row[VAL]));
    t.querySelector(".lbl").textContent = row[VAL] ? "TRUE" : "FALSE";
    t.setAttribute("aria-label", `${row[P]} ${row[VAL] ? "TRUE" : "FALSE"}`);
    cell.replaceChildren(t, ...(src ? [src] : []));
    return;
  }

  let inp = cell.querySelector("input");
  if (!inp) {
    inp = document.createElement("input");
    inp.className = "num";
    inp.spellcheck = false;
    inp.autocomplete = "off";
    inp.setAttribute("aria-label", row[P]);
    const numeric = !row[TYPE].startsWith("STRING");
    if (numeric) inp.inputMode = /REAL/.test(row[TYPE]) ? "decimal" : "numeric";
    const commit = () => {
      const cur = state.byPath.get(row[P]);
      const raw = inp.value.replace(/\s*ms$/i, "");
      if (raw === fmt(cur).replace(/\s*ms$/i, "")) { inp.classList.remove("err"); return; }
      write(row[P], numeric ? raw.trim() : raw, inp);
    };
    inp.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); commit(); }
      if (e.key === "Escape") { inp.value = fmt(state.byPath.get(row[P])); inp.classList.remove("err"); inp.blur(); }
    });
    inp.addEventListener("blur", commit);
  }
  if (document.activeElement !== inp) inp.value = fmt(row);
  cell.replaceChildren(inp, ...(src ? [src] : []));
}

async function write(path, value, input) {
  try {
    await api("/api/tags/write", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path, value }),
    });
    if (input) input.classList.remove("err");
  } catch (e) {
    if (input) input.classList.add("err");
    toast(`${path}: ${e.message}`, true);
  }
}

function applyChange([path, value, source, ts]) {
  const row = state.byPath.get(path);
  if (!row) return;
  row[VAL] = value; row[SRC] = source; row[TS] = ts;
  if (typeof detailOnChange === "function") detailOnChange(path, value, source, ts);
  const el = state.rendered.get(path);
  if (el) {
    fillValue(el, row);
    el.classList.add("flash");
    requestAnimationFrame(() => requestAnimationFrame(() => el.classList.remove("flash")));
    const inp = el.querySelector("input");
    if (inp && document.activeElement !== inp) inp.classList.remove("err");
  }
}

// ------------------------------------------------------------------ websocket
let ws, wsRetry = 500, changedTimer = null;
function connect() {
  ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  ws.onopen = () => {
    wsRetry = 500;
    $("wsState").textContent = "live";
    $("wsState").className = "ws-on";
    // re-sync in case anything changed while disconnected
    refreshStatus().then(loadTags).then(() => sendWatch(true)).catch((e) => toast(e.message, true));
  };
  ws.onmessage = (m) => {
    const msg = JSON.parse(m.data);
    if (msg.type === "values") {
      msg.changes.forEach(applyChange);
      if (state.filter.changed) { clearTimeout(changedTimer); changedTimer = setTimeout(applyFilter, 400); }
    } else if (msg.type === "reload") {
      refreshStatus().then(loadTags);
    } else if (msg.type === "status") {
      refreshStatus();
    } else if (msg.type === "sessions") {
      setSessions(msg.count);
    }
  };
  ws.onclose = () => {
    $("wsState").textContent = "offline — reconnecting";
    $("wsState").className = "ws-off";
    setTimeout(connect, wsRetry);
    wsRetry = Math.min(wsRetry * 2, 8000);
  };
}
setInterval(() => { if (ws && ws.readyState === 1) ws.send("ping"); }, 20000);
setInterval(() => { if (state.status && state.status.mode === "plc") refreshStatus().catch(() => {}); }, 5000);

// ------------------------------------------------------------------ upload
async function upload(file) {
  if (!/\.(zef|xef)$/i.test(file.name)) { toast("Choose a .ZEF or .XEF file", true); return; }
  $("busy").hidden = false;
  $("busyText").textContent = `Loading ${file.name}…`;
  try {
    const fd = new FormData();
    fd.append("file", file);
    const s = await api("/api/program", { method: "POST", body: fd });
    toast(s.mode === "plc" ? `Comments loaded from ${file.name}` : `Loaded ${s.program.name}: ${s.program.tags.toLocaleString()} tags`);
    await refreshStatus();
    await loadTags();
  } catch (e) {
    toast(e.message, true);
  } finally {
    $("busy").hidden = true;
  }
}

// ------------------------------------------------------------------ wiring
function measureRow() {
  const h = parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--row-h"));
  if (h && h !== state.rowH) { state.rowH = h; applyFilter(); }
}

let searchTimer;
$("search").addEventListener("input", (e) => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => { state.filter.text = e.target.value; applyFilter(); }, 120);
});
$("accessSeg").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  for (const x of $("accessSeg").children) x.setAttribute("aria-checked", String(x === b));
  state.filter.access = b.dataset.v;
  applyFilter();
});
$("viewSeg").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (b && b.dataset.v !== state.mode) setMode(b.dataset.v);
});
$("expandAll").addEventListener("click", () => setAllFolders(true));
$("thead").addEventListener("click", (e) => {
  const th = e.target.closest("[data-col]");
  if (th) setSort(th.dataset.col);
});
$("collapseAll").addEventListener("click", () => setAllFolders(false));
$("typeSel").addEventListener("change", (e) => { state.filter.type = e.target.value; applyFilter(); });
$("changedOnly").addEventListener("change", (e) => { state.filter.changed = e.target.checked; applyFilter(); });
$("scroller").addEventListener("scroll", () => requestAnimationFrame(render), { passive: true });
window.addEventListener("resize", () => { measureRow(); render(); });
$("fileInput").addEventListener("change", (e) => { if (e.target.files[0]) upload(e.target.files[0]); e.target.value = ""; });
$("resetBtn").addEventListener("click", async () => {
  if (!confirm("Reset every tag to its initial value from the program?")) return;
  try { await api("/api/reset", { method: "POST" }); toast("Values reset to program initial values"); }
  catch (e) { toast(e.message, true); }
});
$("copyEp").addEventListener("click", async () => {
  const text = $("endpoint").textContent;
  if (await copyText(text)) toast("Endpoint copied");
  else toast(`Copy failed — select the text instead: ${text}`, true);
});
$("plcBtn").addEventListener("click", openPlcDialog);
$("plcForm").addEventListener("submit", connectPlc);
$("plcCancel").addEventListener("click", () => $("plcDialog").close());
$("plcDisconnect").addEventListener("click", async () => {
  try {
    await api("/api/plc", { method: "DELETE" });
    $("plcDialog").close();
    toast("Disconnected from the PLC");
    await refreshStatus();
    await loadTags();
  } catch (err) { toast(err.message, true); }
});
$("removeExport").addEventListener("click", async () => {
  if (!confirm("Remove the comments file? All PLC tags become writable and lose their comments.")) return;
  try { await api("/api/program", { method: "DELETE" }); toast("Comments file removed"); }
  catch (err) { toast(err.message, true); }
});
$("warnToggle").addEventListener("click", () => { $("warnList").hidden = !$("warnList").hidden; });

let dragDepth = 0;
window.addEventListener("dragenter", (e) => { if (e.dataTransfer.types.includes("Files")) { dragDepth++; $("dropzone").hidden = false; } });
window.addEventListener("dragleave", () => { if (--dragDepth <= 0) { dragDepth = 0; $("dropzone").hidden = true; } });
window.addEventListener("dragover", (e) => e.preventDefault());
window.addEventListener("drop", (e) => {
  e.preventDefault();
  dragDepth = 0;
  $("dropzone").hidden = true;
  const f = e.dataTransfer.files[0];
  if (f) upload(f);
});

for (const b of $("viewSeg").children) b.setAttribute("aria-checked", String(b.dataset.v === state.mode));
renderSortHeader();
measureRow();
connect();
