"use strict";

// Diagnostics view: OPC UA clients, PLC polling and server health. Refreshes every 2 s
// while open; reachable directly at #diagnostics.

let diagTimer = null;

function duration(seconds) {
  if (seconds == null) return "—";
  const s = Math.max(0, Math.round(seconds));
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m ${s % 60}s`;
  return `${s}s`;
}

function tile(label, value, sub) {
  const t = el("div", "tile");
  t.append(el("div", "t-label", label), el("div", "t-value", value));
  if (sub) t.append(el("div", "t-sub", sub));
  return t;
}

function kv(rows) {
  const dl = el("dl");
  for (const [k, v] of rows) {
    if (v === undefined) continue;
    dl.append(el("dt", null, k));
    const dd = el("dd");
    if (v instanceof Node) dd.append(v); else dd.append(el("span", null, v ?? "—"));
    dl.append(dd);
  }
  return dl;
}

function showDiagnostics(open) {
  $("diag").hidden = !open;
  document.body.classList.toggle("diag-open", open);
  $("table").hidden = open || state.rows.length === 0;
  $("empty").hidden = open || state.rows.length > 0;
  if (open && state.detail) closeDetail();
  clearInterval(diagTimer);
  if (open) {
    refreshDiagnostics();
    diagTimer = setInterval(refreshDiagnostics, 2000);
    if (location.hash !== "#diagnostics") history.replaceState(null, "", "#diagnostics");
  } else if (location.hash === "#diagnostics") {
    history.replaceState(null, "", location.pathname);
  }
}

async function refreshDiagnostics() {
  let d;
  try { d = await api("/api/diagnostics"); } catch (e) { $("diagUpdated").textContent = `update failed: ${e.message}`; return; }
  const plc = d.plc, ua = d.opcua, srv = d.server;
  $("diagUpdated").textContent = `updated ${clockTime(d.now)}`;

  // headline tiles
  const tiles = [
    tile("OPC UA clients", String(ua.clients.length), `${ua.subscriptions} subscription${ua.subscriptions === 1 ? "" : "s"}`),
    tile("Subscribed tags", ua.subscribed_tags.toLocaleString(), `${ua.monitored_items.toLocaleString()} monitored items`),
  ];
  if (plc) {
    tiles.push(
      tile("PLC update cycle", plc.state === "connected" ? `${Math.max(plc.cycle, plc.update_period).toFixed(1)} s` : "—",
        `target ${plc.update_period} s`),
      tile("Watched tags", plc.watched.toLocaleString(), `${plc.watched_requests} request${plc.watched_requests === 1 ? "" : "s"} per cycle`),
      tile("UMAS requests", plc.requests_per_s != null ? `${plc.requests_per_s}/s` : "—",
        plc.avg_request_ms != null ? `avg ${plc.avg_request_ms} ms each` : ""),
      tile("Full refresh", `${plc.full_refresh} s`, "every other tag"),
    );
  }
  $("diagTiles").replaceChildren(...tiles);

  // PLC polling
  $("diagPlcCard").hidden = !plc;
  if (plc) {
    const box = $("diagPlc");
    box.replaceChildren();
    const stateEl = el("span", plc.state === "connected" ? "state-ok" : "state-bad", plc.state);
    if (plc.cycles.length > 1) {
      const host = el("div", "d-chart");
      box.append(el("p", "muted small", "Update cycle, seconds (one point per cycle)"), host);
      const pts = plc.cycles.map(([t, c, w, b]) => [t, c, `${c.toFixed(2)} s`,
        `${clockTime(t)} · ${w} watched + ${b} background requests`]);
      const top = Math.max(plc.update_period * 1.5, ...plc.cycles.map((c) => c[1]));
      lineChart(host, pts, { from: pts[0][0], to: pts[pts.length - 1][0], height: 130,
        yTicks: niceTicks(0, top, 3), yFormat: (v) => `${+v.toFixed(2)}`,
        ariaLabel: "PLC update cycle time; use the arrow keys to step through cycles" });
    }
    const wb = plc.watched_by || {};
    box.append(kv([
      ["PLC", `${plc.host}:${plc.port} (unit ${plc.unit})`],
      ["State", stateEl],
      ["Error", plc.error || undefined],
      ["Connected for", plc.connected_since ? duration(d.now - plc.connected_since) : "—"],
      ["Reconnects", String(plc.reconnects)],
      ["PLC host name", plc.plc_name || "—"],
      ["Project", plc.dictionary && plc.dictionary.project ? `${plc.dictionary.project} (CRC ${plc.crc})` : plc.crc],
      ["MAST period", plc.mast_ms != null ? `${plc.mast_ms} ms — about one UMAS request per period` : "unknown"],
      ["Max frame", plc.max_frame ? `${plc.max_frame} bytes` : "—"],
      ["Dictionary", plc.dictionary && plc.dictionary.source
        ? `${plc.dictionary.variables} variables, ${plc.dictionary.types} types → ${plc.dictionary.tags} tags; ` +
          (plc.dictionary.source === "cache" ? "from cache" : `read from PLC in ${plc.dictionary.seconds} s`)
        : "—"],
      ["Watched by", `${(wb.subscribed || 0).toLocaleString()} subscribed, ${(wb.read || 0).toLocaleString()} read, ${(wb.web_ui || 0).toLocaleString()} on screen`],
      ["Per cycle", `${plc.watched_requests} watched + ${plc.background_requests} background requests`],
      ["Requests", `${plc.requests.toLocaleString()} total, ${plc.errors.toLocaleString()} errors`],
    ]));
  }

  // OPC UA clients
  const cl = $("diagClients");
  if (!ua.clients.length) {
    cl.replaceChildren(el("p", "muted small", `No clients connected. Endpoint port ${ua.port}.`));
  } else {
    const t = el("table");
    const hr = el("tr");
    for (const h of ["Client", "Address", "Connected", "Subscribed", "Publishing"]) hr.append(el("th", null, h));
    const thead = el("thead"); thead.append(hr);
    const tb = el("tbody");
    for (const c of ua.clients) {
      const tr = el("tr");
      const who = el("td");
      who.append(el("div", null, c.application || "(no name)"));
      if (c.session) who.append(el("div", "muted small", c.session));
      tr.append(who, el("td", "mono", c.address),
        el("td", null, c.since ? duration(d.now - c.since) : c.state),
        el("td", null, `${c.monitored_items.toLocaleString()} in ${c.subscriptions}`),
        el("td", null, c.publishing_ms.length ? c.publishing_ms.map((m) => `${m} ms`).join(", ") : "—"));
      tb.append(tr);
    }
    t.append(thead, tb);
    cl.replaceChildren(t);
  }
  cl.append(el("p", "muted small", `Tags read by clients in the last minute: ${ua.recently_read_tags.toLocaleString()}`));

  // server
  $("diagServer").replaceChildren(kv([
    ["Mode", srv.mode === "plc" ? "PLC (live)" : "Export file"],
    ["Program", srv.program || "—"],
    ["File", srv.file || "—"],
    ["Tags", srv.tags.toLocaleString()],
    ["Uptime", duration(d.now - srv.started)],
    ["Memory", srv.memory_mb != null ? `${srv.memory_mb} MB` : "—"],
    ["Web UI connections", String(srv.web_clients)],
    ["Change history", `${srv.history_entries.toLocaleString()} entries`],
    ["Snapshots", String(srv.snapshots)],
    ["Versions", `Python ${srv.versions.python}, asyncua ${srv.versions.asyncua}`],
  ]));
}

$("diagBtn").addEventListener("click", () => showDiagnostics($("diag").hidden));
$("diagBack").addEventListener("click", () => showDiagnostics(false));
window.addEventListener("hashchange", () => showDiagnostics(location.hash === "#diagnostics"));
if (location.hash === "#diagnostics") showDiagnostics(true);
