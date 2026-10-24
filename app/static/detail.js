"use strict";

// Tag detail panel: value + editor, recent-change trend and table, OPC UA identity,
// structure chain and PLC location. Opened by clicking a tag row.

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined && text !== null) e.textContent = text;
  return e;
}

async function openDetail(path) {
  state.detail = { path, data: null, history: [] };
  markSelected();
  $("detail").hidden = false;
  document.body.classList.add("detail-open");
  $("detailName").textContent = splitPath(path).slice(-1)[0];
  $("detailRoot").textContent = path;
  $("detailBody").replaceChildren(el("p", "muted", "Loading…"));
  sendWatch(true);
  try {
    const data = await api(`/api/tag?path=${encodeURIComponent(path)}`);
    if (!state.detail || state.detail.path !== path) return;
    state.detail.data = data;
    state.detail.history = data.history;
    renderDetail();
  } catch (e) {
    toast(e.message, true);
    closeDetail();
  }
}

function closeDetail() {
  state.detail = null;
  $("detail").hidden = true;
  document.body.classList.remove("detail-open");
  markSelected();
  sendWatch(true);
}

function markSelected() {
  for (const [k, row] of state.rendered) row.classList.toggle("selected", !!state.detail && k === state.detail.path);
}

function copyButton(text, label) {
  const b = el("button", "icon-btn");
  b.title = `Copy ${label}`;
  b.setAttribute("aria-label", `Copy ${label}`);
  b.innerHTML = '<svg viewBox="0 0 24 24" width="15" height="15"><rect x="8" y="8" width="12" height="12" rx="2" fill="none" stroke="currentColor" stroke-width="2"/><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2" fill="none" stroke="currentColor" stroke-width="2"/></svg>';
  b.addEventListener("click", async () => {
    if (await copyText(text)) toast(`${label} copied`);
    else toast(`Copy failed — select the text instead: ${text}`, true);
  });
  return b;
}

function dlRow(dl, term, value, copy) {
  dl.append(el("dt", null, term));
  const dd = el("dd");
  if (value instanceof Node) dd.append(value);
  else dd.append(el("span", "mono", value ?? "—"));
  if (copy) dd.append(copyButton(copy, term));
  dl.append(dd);
}

function renderDetail() {
  const d = state.detail.data;
  const row = state.byPath.get(d.path);
  const body = $("detailBody");
  body.replaceChildren();

  // value + editor (the same control as the table row)
  const valueCard = el("section", "d-card d-value");
  const valueWrap = el("div");
  valueWrap.append(el("div", "c-value"));
  if (row) fillValue(valueWrap, row);
  const badges = el("div", "d-badges");
  const badge = el("span", "badge " + (d.writable ? "rw" : "ro") + (d.access === "default" ? " def" : ""), d.writable ? "R/W" : "R/O");
  badges.append(badge, el("span", "type", d.type));
  valueCard.append(valueWrap, badges);
  if (d.comment) valueCard.append(el("p", "d-comment", d.comment));
  body.append(valueCard);

  // recent changes: trend (numbers and booleans) + table
  const trend = el("section", "d-card");
  trend.append(el("h3", null, "Recent changes"));
  const chartHost = el("div", "d-chart");
  const caption = el("p", "muted small");
  trend.append(chartHost, caption);
  const table = el("table", "d-history");
  trend.append(table);
  body.append(trend);
  state.detail.chartHost = chartHost;
  state.detail.caption = caption;
  state.detail.table = table;
  renderHistory();

  // OPC UA
  const ua = el("section", "d-card");
  ua.append(el("h3", null, "OPC UA"));
  const dl1 = el("dl");
  dlRow(dl1, "NodeId", d.nodeid, d.nodeid);
  dlRow(dl1, "Expanded NodeId", d.nodeid_expanded, d.nodeid_expanded);
  dlRow(dl1, "Data type", `${d.type} → ${d.ua_type}`);
  dlRow(dl1, "Access", d.writable ? "CurrentRead | CurrentWrite" : "CurrentRead");
  dlRow(dl1, "Subscriptions", String(d.subscribers));
  ua.append(dl1);
  body.append(ua);

  // program structure
  const prog = el("section", "d-card");
  prog.append(el("h3", null, "Program"));
  const dl2 = el("dl");
  if (d.parents.length) {
    const chain = el("ol", "d-chain");
    for (const p of d.parents) {
      const li = el("li");
      li.append(el("span", "mono", p.name));
      if (p.type) li.append(el("span", "type", p.type));
      if (p.comment) li.append(el("span", "muted", p.comment));
      chain.append(li);
    }
    dlRow(dl2, "Structure", chain);
  }
  dlRow(dl2, "Comment prefix", d.access === "default" ? "none (default access)" : d.access);
  if (d.address) dlRow(dl2, "Address", d.address);
  if (state.status && state.status.mode !== "plc") dlRow(dl2, "Initial value", fmt(row || [d.path, d.type], d.initial));
  if (d.plc) {
    dlRow(dl2, "PLC memory", `block ${d.plc.block}, offset ${d.plc.offset}, ${d.plc.size} byte${d.plc.size === 1 ? "" : "s"} (${d.plc.type})`);
    dlRow(dl2, "Polling", "watched — refreshed every update period while this panel is open");
  }
  prog.append(dl2);
  body.append(prog);
}

function renderHistory() {
  const det = state.detail;
  if (!det || !det.data) return;
  const d = det.data;
  const row = state.byPath.get(d.path);
  const hist = det.history;
  const kind = row ? typeof row[VAL] : "number";
  const now = Date.now() / 1000;

  if (kind === "string") {
    det.chartHost.replaceChildren();
    det.chartHost.hidden = true;
  } else {
    det.chartHost.hidden = false;
    const fmtV = (v) => (kind === "boolean" ? (v ? "TRUE" : "FALSE") : fmt(row || [d.path, d.type], v));
    const pts = hist.map(([t, v, src]) => [t, kind === "boolean" ? (v ? 1 : 0) : Number(v), fmtV(v),
      `${clockTime(t)} · ${SRC_LABEL[src] || src}`]).filter((p) => Number.isFinite(p[1]));   // NaN/Inf: table only
    if (!pts.length && row && (kind === "boolean" || Number.isFinite(Number(row[VAL])))) {
      // no change seen yet: show the current value as a flat line
      pts.push([now - 60, kind === "boolean" ? (row[VAL] ? 1 : 0) : Number(row[VAL]), fmtV(row[VAL]), "current value"]);
    }
    if (!pts.length) {
      det.chartHost.replaceChildren();
      det.chartHost.hidden = true;
    } else {
      lineChart(det.chartHost, pts, {
        step: true, from: Math.min(pts[0][0], now - 60), to: now, height: 140,
        yTicks: kind === "boolean" ? [0, 1] : undefined,
        yFormat: kind === "boolean" ? (v) => (v ? "TRUE" : "FALSE") : undefined,
        ariaLabel: `Recent values of ${d.path}; use the arrow keys to step through changes`,
      });
    }
  }
  det.caption.textContent = hist.length
    ? `${hist.length} change${hist.length === 1 ? "" : "s"} since ${clockTime(hist[0][0])} (last ${HISTORY_LIMIT} kept)`
    : "No changes seen since the tags were loaded.";

  const table = det.table;
  table.replaceChildren();
  if (!hist.length) return;
  const head = el("tr");
  head.append(el("th", null, "Time"), el("th", null, "Value"), el("th", null, "Changed by"));
  table.append(el("thead"));
  table.firstChild.append(head);
  const tb = el("tbody");
  for (const [t, v, src] of hist.slice(-50).reverse()) {
    const tr = el("tr");
    tr.append(el("td", "mono", clockTime(t)), el("td", "mono", fmt(row || [d.path, d.type], v)),
      el("td", null, SRC_LABEL[src] || src));
    tb.append(tr);
  }
  table.append(tb);
}

const HISTORY_LIMIT = 240;      // matches the server's HISTORY_PER_TAG

function detailOnChange(path, value, source, ts) {
  const det = state.detail;
  if (!det || det.path !== path || !det.data) return;
  const last = det.history[det.history.length - 1];
  if (last && last[1] === value && ts - last[0] < 3) {
    if (last[2] === "plc") last[2] = source;    // same change seen by the poll first (as the server does)
  } else {
    det.history.push([ts, value, source]);
    if (det.history.length > HISTORY_LIMIT) det.history.shift();
  }
  const row = state.byPath.get(path);
  const valueWrap = $("detailBody").querySelector(".d-value > div");
  if (row && valueWrap) fillValue(valueWrap, row);
  renderHistory();
}

// ------------------------------------------------------------------ wiring
$("detailClose").addEventListener("click", closeDetail);
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && state.detail && !document.querySelector("dialog[open]")) closeDetail();
});
$("spacer").addEventListener("click", (e) => {
  const rowEl = e.target.closest(".row:not(.folder)");
  if (!rowEl || e.target.closest("button, input, .toggle")) return;
  openDetail(rowEl.dataset.path);
});
$("spacer").addEventListener("keydown", (e) => {
  if ((e.key === "Enter" || e.key === " ") && e.target.classList.contains("c-path") && e.target.closest(".row:not(.folder)")) {
    e.preventDefault();
    openDetail(e.target.closest(".row").dataset.path);
  }
});
setInterval(() => { if (state.detail && state.detail.data) renderHistory(); }, 5000);   // keep "now" moving
