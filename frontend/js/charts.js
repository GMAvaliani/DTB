/* SVG chart + graph renderers — dependency-free. */
"use strict";

const SVG_NS = "http://www.w3.org/2000/svg";

function svgEl(tag, attrs, ...children) {
  const node = document.createElementNS(SVG_NS, tag);
  if (attrs) for (const [k, v] of Object.entries(attrs)) {
    if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  children.flat().forEach(c => c && node.append(c));
  return node;
}

const TYPE_COLORS = {
  enterprise: "#f5b83d", goal: "#f5b83d", sed: "#4f8ff7", utility_type: "#3ecf8e",
  resource: "#fb7185", org_unit: "#7c5cff", employee: "#b78cff", equipment: "#38bdf8",
  erp_object: "#8b98b8", event_kind: "#f06a6a", kpi: "#a3e635", custom: "#e879f9",
};
const TYPE_LABELS = {
  enterprise: "Предприятие", goal: "Цель", sed: "СЭД", utility_type: "Полезность",
  resource: "Ресурс", org_unit: "Орг-единица", employee: "Сотрудник",
  equipment: "Оборудование", erp_object: "ERP-объект", event_kind: "Событие", kpi: "KPI",
  custom: "Пользовательский",
};

/* ------------------------------- line chart ------------------------------- */

function lineChart(opts) {
  const W = opts.width || 640, H = opts.height || 260;
  const padL = 56, padR = 12, padT = 12, padB = 26;
  const series = (opts.series || []).filter(s => s.points && s.points.length);
  const bands = (opts.bands || []).filter(b => b.points && b.points.length);
  let xs = [], ys = [];
  series.forEach(s => s.points.forEach(([x, y]) => { xs.push(x); ys.push(y); }));
  bands.forEach(b => b.points.forEach(([x, lo, hi]) => { xs.push(x); ys.push(lo, hi); }));
  if (!xs.length) return svgEl("svg", { width: W, height: H },
    svgEl("text", { x: W / 2, y: H / 2, fill: "#8b98b8", "text-anchor": "middle" }, "нет данных"));
  let xMin = Math.min(...xs), xMax = Math.max(...xs);
  let yMin = Math.min(0, ...ys), yMax = Math.max(...ys);
  if (opts.yMin !== undefined) yMin = opts.yMin;
  if (yMax === yMin) yMax = yMin + 1;
  if (xMax === xMin) xMax = xMin + 1;
  const X = x => padL + (x - xMin) / (xMax - xMin) * (W - padL - padR);
  const Y = y => H - padB - (y - yMin) / (yMax - yMin) * (H - padT - padB);
  const svg = svgEl("svg", { width: W, height: H, viewBox: `0 0 ${W} ${H}` });
  // grid + axes
  const nTicks = 4;
  for (let i = 0; i <= nTicks; i++) {
    const yv = yMin + (yMax - yMin) * i / nTicks;
    svg.append(svgEl("line", { x1: padL, x2: W - padR, y1: Y(yv), y2: Y(yv), stroke: "#2c3a58", "stroke-width": .6 }));
    svg.append(svgEl("text", { x: padL - 6, y: Y(yv) + 4, fill: "#8b98b8", "font-size": 10, "text-anchor": "end" },
      Math.abs(yv) >= 1e6 ? (yv / 1e6).toFixed(1) + "M" : Math.abs(yv) >= 1e3 ? (yv / 1e3).toFixed(0) + "k" : yv.toFixed(Math.abs(yv) < 10 ? 1 : 0)));
  }
  for (let i = 0; i <= nTicks; i++) {
    const xv = xMin + (xMax - xMin) * i / nTicks;
    svg.append(svgEl("text", { x: X(xv), y: H - 8, fill: "#8b98b8", "font-size": 10, "text-anchor": "middle" },
      (xv / 24).toFixed(0) + "д"));
  }
  // bands
  bands.forEach(b => {
    const up = b.points.map(([x, lo]) => `${X(x)},${Y(lo)}`);
    const down = [...b.points].reverse().map(([x, , hi]) => `${X(x)},${Y(hi)}`);
    svg.append(svgEl("polygon", { points: [...up, ...down].join(" "), fill: b.color || "#4f8ff7", opacity: b.opacity ?? .15 }));
  });
  // series
  series.forEach(s => {
    const pts = s.points.map(([x, y]) => `${X(x)},${Y(y)}`).join(" ");
    svg.append(svgEl("polyline", {
      points: pts, fill: "none", stroke: s.color || "#4f8ff7",
      "stroke-width": s.width || 2, "stroke-dasharray": s.dash || null, opacity: s.opacity ?? 1,
    }));
    if (s.markers) s.points.forEach(([x, y]) => svg.append(
      svgEl("circle", { cx: X(x), cy: Y(y), r: 3, fill: s.color || "#4f8ff7" },
        svgEl("title", null, `${s.name}: t=${(x / 24).toFixed(1)}д, ${y.toLocaleString("ru-RU")}`))));
  });
  return svg;
}

function chartLegend(items) {
  const box = el("div", { class: "legend" });
  items.forEach(([label, color, dash]) => box.append(
    el("span", null, el("span", { class: "sw", style: `background:${color};${dash ? "opacity:.6" : ""}` }), label)));
  return box;
}

/* ------------------------------ graph layouts ----------------------------- */

function layeredLayout(nodes, edges) {
  const incoming = {}, outgoing = {};
  nodes.forEach(n => { incoming[n.id] = 0; outgoing[n.id] = []; });
  edges.forEach(e => {
    if (incoming[e.target] === undefined || outgoing[e.source] === undefined) return;
    incoming[e.target]++; outgoing[e.source].push(e.target);
  });
  // first-visit BFS layering: robust to cycles (QC↔rework etc.)
  const layer = {};
  let frontier = nodes.filter(n => incoming[n.id] === 0).map(n => n.id);
  if (!frontier.length && nodes.length) frontier = [nodes[0].id];
  frontier.forEach(id => layer[id] = 0);
  const queue = [...frontier];
  while (queue.length) {
    const id = queue.shift();
    for (const next of outgoing[id] || []) {
      if (layer[next] === undefined) {
        layer[next] = layer[id] + 1;
        queue.push(next);
      }
    }
  }
  nodes.forEach(n => { if (layer[n.id] === undefined) layer[n.id] = 0; });
  const byLayer = {};
  nodes.forEach(n => (byLayer[layer[n.id]] = byLayer[layer[n.id]] || []).push(n));
  const pos = {};
  Object.entries(byLayer).forEach(([l, arr]) => {
    arr.sort((a, b) => (a.label || a.id).localeCompare(b.label || b.id));
    arr.forEach((n, i) => { pos[n.id] = { x: 90 + l * 210, y: 70 + i * 74 }; });
  });
  return pos;
}

function forceLayout(nodes, edges, iterations) {
  const pos = {};
  const N = nodes.length;
  const R = Math.max(200, N * 16);
  nodes.forEach((n, i) => {
    const a = 2 * Math.PI * i / N;
    pos[n.id] = { x: 400 + R * Math.cos(a) / 2, y: 300 + R * Math.sin(a) / 2 };
  });
  const adj = edges.filter(e => pos[e.source] && pos[e.target]);
  const k = 120;
  for (let it = 0; it < (iterations || 150); it++) {
    const disp = {};
    nodes.forEach(n => disp[n.id] = { x: 0, y: 0 });
    for (let i = 0; i < N; i++) for (let j = i + 1; j < N; j++) {
      const a = nodes[i].id, b = nodes[j].id;
      let dx = pos[a].x - pos[b].x, dy = pos[a].y - pos[b].y;
      let d2 = dx * dx + dy * dy || 0.01;
      if (d2 > 300000) continue;
      const f = k * k / d2;
      const d = Math.sqrt(d2);
      dx /= d; dy /= d;
      disp[a].x += dx * f; disp[a].y += dy * f;
      disp[b].x -= dx * f; disp[b].y -= dy * f;
    }
    adj.forEach(e => {
      let dx = pos[e.source].x - pos[e.target].x, dy = pos[e.source].y - pos[e.target].y;
      const d = Math.sqrt(dx * dx + dy * dy) || 0.1;
      const f = (d * d / k) / d * 0.06;
      disp[e.source].x -= dx * f; disp[e.source].y -= dy * f;
      disp[e.target].x += dx * f; disp[e.target].y += dy * f;
    });
    const t = 12 * (1 - it / (iterations || 150));
    nodes.forEach(n => {
      const d = Math.sqrt(disp[n.id].x ** 2 + disp[n.id].y ** 2) || 0.1;
      pos[n.id].x += disp[n.id].x / d * Math.min(d, t);
      pos[n.id].y += disp[n.id].y / d * Math.min(d, t);
    });
  }
  // normalize into positive space
  const xs = Object.values(pos).map(p => p.x), ys = Object.values(pos).map(p => p.y);
  const minX = Math.min(...xs) - 80, minY = Math.min(...ys) - 60;
  Object.values(pos).forEach(p => { p.x -= minX; p.y -= minY; });
  return pos;
}

/* interactive graph: pan/zoom + draggable nodes */
function graphView(opts) {
  const nodes = opts.nodes || [], edges = opts.edges || [];
  const height = opts.height || 560;
  let positions = { ...(opts.layout === "layered" ? layeredLayout(nodes, edges) : forceLayout(nodes, edges)) };
  Object.assign(positions, opts.positions || {});
  const wrap = el("div", { class: "svg-wrap", style: `height:${height}px` });
  const svg = svgEl("svg", { width: "100%", height: "100%" });
  wrap.append(svg);
  const xs = Object.values(positions).map(p => p.x), ys = Object.values(positions).map(p => p.y);
  const minX = xs.length ? Math.min(...xs) : 0, maxX = xs.length ? Math.max(...xs) : 100;
  const minY = ys.length ? Math.min(...ys) : 0, maxY = ys.length ? Math.max(...ys) : 100;
  let vb = {
    x: minX - 70, y: minY - 50,
    w: Math.max(500, maxX - minX + 160),
    h: Math.max(320, maxY - minY + 110),
  };
  const applyVB = () => svg.setAttribute("viewBox", `${vb.x} ${vb.y} ${vb.w} ${vb.h}`);
  applyVB();

  const defs = svgEl("defs", null, svgEl("marker", {
    id: "arrow", viewBox: "0 0 10 10", refX: 9, refY: 5, markerWidth: 7, markerHeight: 7, orient: "auto-start-reverse",
  }, svgEl("path", { d: "M 0 0 L 10 5 L 0 10 z", fill: "#5a6c94" })));
  svg.append(defs);
  const edgeLayer = svgEl("g"), nodeLayer = svgEl("g");
  svg.append(edgeLayer, nodeLayer);

  const edgeEls = [];
  function drawEdges() {
    edgeLayer.innerHTML = "";
    edgeEls.length = 0;
    edges.forEach(e => {
      const a = positions[e.source], b = positions[e.target];
      if (!a || !b) return;
      const highlighted = opts.highlightEdges && opts.highlightEdges.has(e.id);
      const line = svgEl("line", {
        x1: a.x, y1: a.y, x2: b.x, y2: b.y,
        stroke: highlighted ? "#f5b83d" : "#43547c", "stroke-width": highlighted ? 2.4 : 1.2,
        "marker-end": "url(#arrow)", opacity: opts.dimEdges && !highlighted ? .25 : .9,
      }, svgEl("title", null, `${e.type}${e.label ? ": " + e.label : ""}`));
      edgeLayer.append(line);
      if (opts.edgeLabels) {
        edgeLayer.append(svgEl("text", {
          x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 - 4, fill: "#5a6c94", "font-size": 9, "text-anchor": "middle",
        }, e.type));
      }
      edgeEls.push({ e, line });
    });
  }

  let selected = null;
  function drawNodes() {
    nodeLayer.innerHTML = "";
    nodes.forEach(n => {
      const p = positions[n.id];
      if (!p) return;
      const color = TYPE_COLORS[n.type] || "#8b98b8";
      const dim = opts.highlightNodes && !opts.highlightNodes.has(n.id);
      const g = svgEl("g", { class: "graph-node", transform: `translate(${p.x},${p.y})`, opacity: dim ? .3 : 1 });
      const label = (n.label || n.id).length > 22 ? (n.label || n.id).slice(0, 21) + "…" : (n.label || n.id);
      const w = Math.max(70, label.length * 6.4 + 18);
      g.append(svgEl("rect", {
        x: -w / 2, y: -15, width: w, height: 30, rx: 8,
        fill: n.id === selected ? "#24365c" : "#1b2438",
        stroke: color, "stroke-width": n.id === selected ? 2.5 : 1.5,
      }, svgEl("title", null, `${TYPE_LABELS[n.type] || n.type}: ${n.label || n.id}`)));
      g.append(svgEl("text", { y: 4, fill: "#dde5f4", "font-size": 11, "text-anchor": "middle" }, label));
      g.append(svgEl("circle", { cx: -w / 2 + 8, cy: -8, r: 3, fill: color }));
      // drag + click
      let drag = null, moved = false;
      g.addEventListener("mousedown", ev => {
        ev.stopPropagation();
        drag = { mx: ev.clientX, my: ev.clientY, px: p.x, py: p.y };
        moved = false;
        const move = e2 => {
          const scale = vb.w / svg.clientWidth;
          p.x = drag.px + (e2.clientX - drag.mx) * scale;
          p.y = drag.py + (e2.clientY - drag.my) * scale;
          moved = true;
          g.setAttribute("transform", `translate(${p.x},${p.y})`);
          drawEdges();
        };
        const up = () => {
          window.removeEventListener("mousemove", move);
          window.removeEventListener("mouseup", up);
          if (moved && opts.onMove) opts.onMove(positions);
          if (!moved) { selected = n.id; drawNodes(); if (opts.onSelect) opts.onSelect(n); }
        };
        window.addEventListener("mousemove", move);
        window.addEventListener("mouseup", up);
      });
      nodeLayer.append(g);
    });
  }

  drawEdges();
  drawNodes();

  // pan + zoom
  svg.addEventListener("mousedown", ev => {
    const start = { mx: ev.clientX, my: ev.clientY, x: vb.x, y: vb.y };
    const move = e2 => {
      const scale = vb.w / svg.clientWidth;
      vb.x = start.x - (e2.clientX - start.mx) * scale;
      vb.y = start.y - (e2.clientY - start.my) * scale;
      applyVB();
    };
    const up = () => { window.removeEventListener("mousemove", move); window.removeEventListener("mouseup", up); };
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up);
  });
  wrap.addEventListener("wheel", ev => {
    ev.preventDefault();
    const factor = ev.deltaY > 0 ? 1.15 : 0.87;
    const rect = svg.getBoundingClientRect();
    const cx = vb.x + (ev.clientX - rect.left) / rect.width * vb.w;
    const cy = vb.y + (ev.clientY - rect.top) / rect.height * vb.h;
    vb.w *= factor; vb.h *= factor;
    vb.x = cx - (cx - vb.x) * factor;
    vb.y = cy - (cy - vb.y) * factor;
    applyVB();
  }, { passive: false });

  wrap.redraw = () => { drawEdges(); drawNodes(); };
  wrap.getPositions = () => positions;
  return wrap;
}

function graphLegend(types) {
  return chartLegend(types.map(t => [TYPE_LABELS[t] || t, TYPE_COLORS[t] || "#8b98b8"]));
}
