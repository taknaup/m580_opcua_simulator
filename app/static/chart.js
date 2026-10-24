"use strict";

// Small single-series time chart (SVG) with a crosshair tooltip.
// points: [[time (s), y, label?, sub?], ...] sorted by time. Single series, so no legend.
// opts: { step: bool, from, to (s), yFormat(y), yTicks: [y...], height, ariaLabel }
const SVGNS = "http://www.w3.org/2000/svg";

function svgEl(name, attrs = {}) {
  const el = document.createElementNS(SVGNS, name);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  return el;
}

function niceTicks(lo, hi, count = 3) {
  if (lo === hi) { lo -= 1; hi += 1; }
  const span = hi - lo;
  const step0 = span / count;
  const mag = 10 ** Math.floor(Math.log10(step0));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= step0) || step0;
  const first = Math.floor(lo / step) * step;
  const ticks = [];
  for (let v = first; v <= hi + step * 0.5; v += step) ticks.push(+v.toPrecision(12));
  return ticks;
}

function compactNumber(v) {
  const a = Math.abs(v);
  if (a >= 1e6) return `${+(v / 1e6).toPrecision(3)}M`;
  if (a >= 1e4) return `${+(v / 1e3).toPrecision(3)}K`;
  return String(+v.toPrecision(6));
}

function clockTime(t) {
  return new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function lineChart(host, points, opts = {}) {
  host.replaceChildren();
  host.classList.add("chart");
  const W = Math.max(240, host.clientWidth || 360);
  const H = opts.height || 140;
  const pad = { l: 46, r: 14, t: 10, b: 22 };
  const from = opts.from ?? (points.length ? points[0][0] : Date.now() / 1000 - 60);
  const to = Math.max(opts.to ?? Date.now() / 1000, from + 1);
  const ys = points.map((p) => p[1]);
  let ticks = opts.yTicks || niceTicks(Math.min(...ys), Math.max(...ys));
  const lo = Math.min(...ticks), hi = Math.max(...ticks);
  const x = (t) => pad.l + ((t - from) / (to - from)) * (W - pad.l - pad.r);
  const y = (v) => pad.t + (1 - (v - lo) / (hi - lo || 1)) * (H - pad.t - pad.b);
  // compact (30K) only when that still tells the ticks apart, else full numbers (30,050)
  const tickStep = ticks.length > 1 ? Math.abs(ticks[1] - ticks[0]) : 0;
  const yFmt = opts.yFormat || ((v) => (tickStep >= 1000 || Math.abs(v) < 1e4
    ? compactNumber(v) : v.toLocaleString(undefined, { maximumFractionDigits: 6 })));

  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H, tabindex: "0", role: "img",
    "aria-label": opts.ariaLabel || "Trend" });
  // recessive hairline grid + tick labels (text tokens, never the series color)
  for (const v of ticks) {
    svg.append(svgEl("line", { x1: pad.l, x2: W - pad.r, y1: y(v), y2: y(v), class: "grid" }));
    const lbl = svgEl("text", { x: pad.l - 6, y: y(v) + 4, class: "tick", "text-anchor": "end" });
    lbl.textContent = yFmt(v);
    svg.append(lbl);
  }
  for (const [t, anchor] of [[from, "start"], [to, "end"]]) {
    const lbl = svgEl("text", { x: x(t), y: H - 6, class: "tick", "text-anchor": anchor });
    lbl.textContent = clockTime(t);
    svg.append(lbl);
  }

  if (points.length) {
    // step-after: a value holds until the next change, and up to "now"
    let d = "";
    points.forEach(([t, v], i) => {
      const px = x(Math.max(t, from)), py = y(v);
      if (i === 0) d += `M${px},${py}`;
      else d += opts.step ? `H${px}V${py}` : `L${px},${py}`;
    });
    const last = points[points.length - 1];
    d += opts.step ? `H${x(to)}` : "";
    const base = y(lo);
    svg.append(svgEl("path", { d: `${d}V${base}H${x(Math.max(points[0][0], from))}Z`, class: "area" }));
    svg.append(svgEl("path", { d, class: "line" }));
    const endX = opts.step ? x(to) : x(last[0]);
    svg.append(svgEl("circle", { cx: endX, cy: y(last[1]), r: 4, class: "dot" }));
  }

  // crosshair + tooltip (hover, touch and keyboard)
  const cross = svgEl("line", { y1: pad.t, y2: H - pad.b, class: "cross", visibility: "hidden" });
  const marker = svgEl("circle", { r: 4, class: "dot", visibility: "hidden" });
  svg.append(cross, marker);
  const tip = document.createElement("div");
  tip.className = "chart-tip";
  tip.hidden = true;
  host.append(svg, tip);

  let index = points.length - 1;
  const show = (i) => {
    if (!points.length) return;
    index = Math.max(0, Math.min(points.length - 1, i));
    const [t, v, label, sub] = points[index];
    const px = x(Math.max(t, from));
    cross.setAttribute("x1", px); cross.setAttribute("x2", px); cross.setAttribute("visibility", "visible");
    marker.setAttribute("cx", px); marker.setAttribute("cy", y(v)); marker.setAttribute("visibility", "visible");
    const strong = document.createElement("strong");
    strong.textContent = label ?? yFmt(v);
    const small = document.createElement("span");
    small.textContent = sub ?? clockTime(t);
    tip.replaceChildren(strong, small);
    tip.hidden = false;
    const left = Math.min(Math.max(px - tip.offsetWidth / 2, 0), W - tip.offsetWidth);
    tip.style.left = `${left}px`;
  };
  const hide = () => {
    cross.setAttribute("visibility", "hidden"); marker.setAttribute("visibility", "hidden"); tip.hidden = true;
  };
  const nearest = (clientX) => {
    const r = svg.getBoundingClientRect();
    const t = from + ((clientX - r.left) * (W / r.width) - pad.l) / (W - pad.l - pad.r) * (to - from);
    // step chart: the value in force at t is the last change at or before t
    let i = 0;
    while (i + 1 < points.length && points[i + 1][0] <= t) i++;
    return i;
  };
  svg.addEventListener("pointermove", (e) => show(nearest(e.clientX)));
  svg.addEventListener("pointerleave", hide);
  svg.addEventListener("focus", () => show(index));
  svg.addEventListener("blur", hide);
  svg.addEventListener("keydown", (e) => {
    if (e.key === "ArrowLeft") { e.preventDefault(); show(index - 1); }
    if (e.key === "ArrowRight") { e.preventDefault(); show(index + 1); }
  });
  return svg;
}
