"use strict";

const COPPER = 10_000;
const REALM_ORDER = ["ravencrest", "frostmane", "darkspear", "silvermoon", "sylvanas"];
const REALM_COLOR = Object.fromEntries(REALM_ORDER.map((r, i) => [r, `var(--s${i + 1})`]));
const QUALITY_ORDER = ["POOR", "COMMON", "UNCOMMON", "RARE", "EPIC", "LEGENDARY", "ARTIFACT", "HEIRLOOM"];

const state = {
  tab: "spreads",
  spreads: null,
  timing: null,
  items: new Map(),
  generatedAt: null,
  favorites: new Set(loadJSON("cw.favorites", [])),
  filters: Object.assign({
    maxBuy: 100000, minProfit: 500, minRoi: 20, maxRoi: 400, minSellListings: 3,
    quality: "", search: "", namedOnly: false,
  }, loadJSON("cw.filters", {})),
  sort: { spreads: ["net_profit", -1], timing: ["net_profit", -1], favorites: ["name", 1] },
  detail: null,
  chart: { range: "7d", measure: "min_unit_price", hidden: new Set() },
};

// ---------- utils ----------
function loadJSON(key, fallback) {
  try { const v = localStorage.getItem(key); return v ? JSON.parse(v) : fallback; } catch { return fallback; }
}
function saveJSON(key, value) {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch {}
}
function gold(copper) {
  const g = copper / COPPER;
  if (Math.abs(g) >= 1_000_000) return (g / 1_000_000).toFixed(2) + "M";
  if (Math.abs(g) >= 10_000) return Math.round(g / 1000) + "k";
  return Math.round(g).toLocaleString("en-US");
}
function goldFull(copper) { return Math.round(copper / COPPER).toLocaleString("en-US") + "g"; }
function pct(x) { return Math.round(x * 100) + "%"; }
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined) node.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined) continue;
    node.append(c.nodeType ? c : document.createTextNode(String(c)));
  }
  return node;
}
function ago(iso) {
  const t = iso ? new Date(iso).getTime() : NaN;
  if (Number.isNaN(t)) return "unknown time";
  const mins = Math.max(0, Math.round((Date.now() - t) / 60000));
  if (mins < 60) return `${mins} min ago`;
  const h = Math.floor(mins / 60);
  return h < 48 ? `${h} h ${mins % 60} min ago` : `${Math.floor(h / 24)} d ago`;
}
function itemInfo(itemId, row) {
  const i = state.items.get(itemId) || {};
  return {
    name: i.name || row?.name || `Item ${itemId}`,
    quality: i.quality || row?.quality || "",
    cls: [i.item_class || row?.item_class, i.item_subclass || row?.item_subclass].filter(Boolean).join(" · "),
    icon: i.icon_url || "",
    named: Boolean(i.name || row?.name),
  };
}

// ---------- data ----------
async function fetchJSON(path) {
  const r = await fetch(`data/${path}`, { cache: "no-cache" });
  if (!r.ok) throw new Error(`${path}: ${r.status}`);
  return r.json();
}
async function load() {
  const [spreads, timing, items] = await Promise.all([
    fetchJSON("latest/spreads.json"), fetchJSON("latest/timing.json"), fetchJSON("items.json"),
  ]);
  state.spreads = spreads.rows;
  state.timing = timing.rows;
  state.generatedAt = spreads.generated_at;
  for (const it of items.items) state.items.set(it.item_id, it);
  document.getElementById("meta").textContent = `updated ${ago(state.generatedAt)} · ${state.spreads.length} spreads · ${state.timing.length} timing flips`;
  document.getElementById("realms").textContent = REALM_ORDER.join(" · ");
}

// ---------- filters ----------
function applyFilters(rows, kind) {
  const f = state.filters;
  const q = f.search.trim().toLowerCase();
  return rows.filter((r) => {
    const info = itemInfo(r.item_id, r);
    if (f.namedOnly && !info.named) return false;
    if (q && !info.name.toLowerCase().includes(q)) return false;
    if (f.quality && info.quality !== f.quality) return false;
    if (r.buy_price > f.maxBuy * COPPER) return false;
    if (r.net_profit < f.minProfit * COPPER) return false;
    if (r.roi * 100 < f.minRoi || r.roi * 100 > f.maxRoi) return false;
    if (kind === "spreads" && r.sell_listings < f.minSellListings) return false;
    return true;
  });
}
function sortRows(rows, [key, dir]) {
  return rows.slice().sort((a, b) => {
    const av = key === "name" ? itemInfo(a.item_id, a).name : a[key];
    const bv = key === "name" ? itemInfo(b.item_id, b).name : b[key];
    if (av === bv) return 0;
    if (av === null || av === undefined) return 1;
    if (bv === null || bv === undefined) return -1;
    return (av < bv ? -1 : 1) * dir;
  });
}
function filterBar(kind) {
  const f = state.filters;
  const num = (label, key, opts = {}) => el("label", {}, label,
    el("input", { type: "number", value: f[key], min: 0, step: opts.step || 1, oninput: (e) => { f[key] = Number(e.target.value) || 0; saveJSON("cw.filters", f); render(); } }));
  return el("div", { class: "filters" },
    el("label", {}, "Search", el("input", { class: "wide", type: "search", value: f.search, placeholder: "item name", oninput: (e) => { f.search = e.target.value; saveJSON("cw.filters", f); render(); } })),
    num("Max buy (g)", "maxBuy", { step: 1000 }),
    num("Min net profit (g)", "minProfit", { step: 100 }),
    num("Min ROI %", "minRoi", { step: 5 }),
    num("Max ROI %", "maxRoi", { step: 5 }),
    kind === "spreads" ? num("Min sell listings", "minSellListings") : null,
    el("label", {}, "Quality", el("select", { onchange: (e) => { f.quality = e.target.value; saveJSON("cw.filters", f); render(); } },
      el("option", { value: "" }, "any"),
      ...QUALITY_ORDER.map((q) => el("option", { value: q, selected: f.quality === q ? "" : null }, q.toLowerCase())))),
    el("label", { class: "check" }, el("input", { type: "checkbox", checked: f.namedOnly ? "" : null, onchange: (e) => { f.namedOnly = e.target.checked; saveJSON("cw.filters", f); render(); } }), "named items only"),
  );
}

// ---------- tables ----------
function starButton(itemId) {
  const on = state.favorites.has(itemId);
  return el("button", { class: `star${on ? " on" : ""}`, title: on ? "Remove favorite" : "Add favorite",
    onclick: (e) => { e.stopPropagation(); toggleFavorite(itemId); } }, on ? "★" : "☆");
}
function toggleFavorite(itemId) {
  if (state.favorites.has(itemId)) state.favorites.delete(itemId); else state.favorites.add(itemId);
  saveJSON("cw.favorites", [...state.favorites]);
  render();
}
function itemCell(itemId, row) {
  const info = itemInfo(itemId, row);
  return el("td", { class: "item left" },
    info.icon ? el("img", { src: info.icon, alt: "", loading: "lazy" }) : el("span", { style: "width:24px;height:24px" }),
    el("div", {}, el("div", { class: "name" }, info.name), el("div", { class: "sub" }, [info.quality.toLowerCase(), info.cls].filter(Boolean).join(" · "))));
}
function realmCell(realm, price, listings) {
  return el("td", {}, el("div", {}, el("span", { class: "dot", style: `background:${REALM_COLOR[realm] || "var(--muted)"}` }), goldFull(price)),
    el("div", { class: "realm" }, `${realm} · ${listings} listed`));
}
function table(kind, rows, columns) {
  const [sortKey, sortDir] = state.sort[kind];
  const sorted = sortRows(rows, [sortKey, sortDir]);
  const head = el("tr", {}, el("th", {}, ""), ...columns.map((c) => el("th", {
    class: `${c.left ? "left " : ""}${c.key === sortKey ? "sorted" : ""} ${sortDir > 0 && c.key === sortKey ? "asc" : ""}`,
    onclick: () => { state.sort[kind] = [c.key, c.key === sortKey ? -sortDir : (c.defaultDir || -1)]; render(); },
  }, c.label)));
  const body = sorted.map((r) => el("tr", { onclick: () => openDetail(r.item_id) },
    el("td", {}, starButton(r.item_id)), ...columns.map((c) => c.cell(r))));
  return el("div", { class: "card table-wrap" }, el("table", {}, el("thead", {}, head), el("tbody", {}, ...body)),
    rows.length ? null : el("div", { class: "empty" }, "Nothing matches the current filters."));
}
const SPREAD_COLS = [
  { key: "name", label: "Item", left: true, defaultDir: 1, cell: (r) => itemCell(r.item_id, r) },
  { key: "buy_price", label: "Buy", cell: (r) => realmCell(r.buy_realm, r.buy_price, r.buy_listings) },
  { key: "sell_price", label: "Sell (undercut)", cell: (r) => realmCell(r.sell_realm, r.sell_price, r.sell_listings) },
  { key: "net_profit", label: "Net profit", cell: (r) => el("td", { class: r.net_profit > 0 ? "pos" : "neg" }, goldFull(r.net_profit)) },
  { key: "roi", label: "ROI", cell: (r) => el("td", {}, pct(r.roi)) },
  { key: "realm_count", label: "Realms", cell: (r) => el("td", {}, r.realm_count) },
];
const TIMING_COLS = [
  { key: "name", label: "Item", left: true, defaultDir: 1, cell: (r) => itemCell(r.item_id, r) },
  { key: "realm_slug", label: "Realm", left: true, defaultDir: 1, cell: (r) => el("td", { class: "left" }, el("span", { class: "dot", style: `background:${REALM_COLOR[r.realm_slug]}` }), r.realm_slug) },
  { key: "buy_price", label: "Now", cell: (r) => el("td", {}, goldFull(r.buy_price), el("div", { class: "realm" }, `${r.listing_count} listed`)) },
  { key: "p50", label: "Normal (p50)", cell: (r) => el("td", {}, goldFull(r.p50), el("div", { class: "realm" }, `${gold(r.p25)}–${gold(r.p75)}`)) },
  { key: "discount", label: "Discount", cell: (r) => el("td", { class: "pos" }, pct(r.discount)) },
  { key: "net_profit", label: "Net profit", cell: (r) => el("td", { class: r.net_profit > 0 ? "pos" : "neg" }, goldFull(r.net_profit)) },
  { key: "roi", label: "ROI", cell: (r) => el("td", {}, pct(r.roi)) },
  { key: "turnover_events", label: "Turnover", cell: (r) => el("td", {}, r.turnover_events, el("div", { class: "realm" }, `${r.snapshots} h history`)) },
  { key: "zscore", label: "z", cell: (r) => el("td", {}, r.zscore) },
];
const FAV_COLS = [
  { key: "name", label: "Item", left: true, defaultDir: 1, cell: (r) => itemCell(r.item_id, r) },
  { key: "spread", label: "Best spread", cell: (r) => r.spread ? realmCell(r.spread.buy_realm, r.spread.buy_price, r.spread.buy_listings) : el("td", { class: "realm" }, "—") },
  { key: "spread_net", label: "Spread net", cell: (r) => el("td", { class: r.spread ? "pos" : "" }, r.spread ? goldFull(r.spread.net_profit) : "—") },
  { key: "timing_net", label: "Timing net", cell: (r) => el("td", { class: r.timing ? "pos" : "" }, r.timing ? `${goldFull(r.timing.net_profit)} on ${r.timing.realm_slug}` : "—") },
];

// ---------- views ----------
function renderList() {
  const app = document.getElementById("app");
  app.replaceChildren();
  const tabs = el("nav", { class: "tabs", role: "tablist" }, ...[
    ["spreads", "Cross-realm flips"], ["timing", "Timing flips"], ["favorites", `Favorites (${state.favorites.size})`],
  ].map(([id, label]) => el("button", { role: "tab", "aria-selected": String(state.tab === id), onclick: () => { state.tab = id; location.hash = id; render(); } }, label)));
  app.append(tabs);

  if (state.tab === "favorites") {
    const rows = [...state.favorites].map((id) => {
      const spread = sortRows(state.spreads.filter((r) => r.item_id === id), ["net_profit", -1])[0] || null;
      const timing = sortRows(state.timing.filter((r) => r.item_id === id), ["net_profit", -1])[0] || null;
      return { item_id: id, spread, timing, spread_net: spread?.net_profit ?? -1, timing_net: timing?.net_profit ?? -1 };
    });
    app.append(el("p", { class: "count" }, rows.length ? `${rows.length} favorites` : "Star items in the other tabs or on an item page to track them here."));
    app.append(table("favorites", rows, FAV_COLS));
    return;
  }
  const kind = state.tab;
  const source = kind === "spreads" ? state.spreads : state.timing;
  const rows = applyFilters(source, kind);
  app.append(filterBar(kind));
  app.append(el("p", { class: "count" }, `${rows.length} of ${source.length} shown`));
  if (kind === "timing" && !source.length) {
    app.append(el("div", { class: "card" }, el("div", { class: "empty" }, "No timing flips yet — this needs a day or two of hourly history before dips are measurable.")));
    return;
  }
  app.append(table(kind, rows, kind === "spreads" ? SPREAD_COLS : TIMING_COLS));
}

async function openDetail(itemId) {
  location.hash = `item/${itemId}`;
  state.detail = { itemId, history: null };
  render();
  try {
    const h = await fetchJSON(`history/${itemId}.json`);
    if (state.detail?.itemId === itemId) { state.detail.history = h.rows; render(); }
  } catch {
    if (state.detail?.itemId === itemId) { state.detail.history = []; render(); }
  }
}

function renderDetail() {
  const app = document.getElementById("app");
  app.replaceChildren();
  const { itemId, history } = state.detail;
  const info = itemInfo(itemId);
  app.append(el("button", { class: "back", onclick: () => { state.detail = null; location.hash = state.tab; render(); } }, "← back"));
  app.append(el("div", { class: "detail-head" },
    info.icon ? el("img", { src: info.icon, alt: "" }) : null,
    el("div", {}, el("h2", {}, info.name, " ", starButton(itemId)), el("div", { class: "sub" }, [info.quality.toLowerCase(), info.cls, `item ${itemId}`].filter(Boolean).join(" · "))),
  ));

  if (history === null) { app.append(el("div", { class: "chart-empty" }, "loading history…")); return; }

  // current per-realm snapshot from the latest timestamp per realm
  const latestByRealm = new Map();
  for (const r of history) {
    const cur = latestByRealm.get(r.realm_slug);
    if (!cur || r.fetched_at > cur.fetched_at) latestByRealm.set(r.realm_slug, r);
  }
  const tiles = REALM_ORDER.filter((r) => latestByRealm.has(r)).map((realm) => {
    const r = latestByRealm.get(realm);
    return el("div", { class: "tile" },
      el("div", { class: "l" }, el("span", { class: "dot", style: `background:${REALM_COLOR[realm]}` }), realm),
      el("div", { class: "v" }, goldFull(r.min_unit_price)),
      el("div", { class: "d" }, `median ${goldFull(r.median_unit_price)} · ${r.listing_count} listed · ${ago(r.fetched_at)}`));
  });
  app.append(el("div", { class: "grid" }, ...tiles, tiles.length ? null : el("div", { class: "tile" }, el("div", { class: "d" }, "Not currently listed on any tracked realm."))));

  const spread = sortRows(state.spreads.filter((r) => r.item_id === itemId), ["net_profit", -1])[0];
  if (spread) {
    app.append(el("div", { class: "grid" },
      el("div", { class: "tile" }, el("div", { class: "l" }, "Best cross-realm flip"), el("div", { class: "v pos" }, goldFull(spread.net_profit)),
        el("div", { class: "d" }, `buy ${spread.buy_realm} ${goldFull(spread.buy_price)} → sell ${spread.sell_realm} ${goldFull(spread.sell_price)} · ROI ${pct(spread.roi)}`))));
  }

  app.append(chartCard(history));
}

// ---------- chart ----------
function chartCard(history) {
  const c = state.chart;
  const card = el("div", { class: "card chart-card" });
  const ranges = [["24h", 1], ["7d", 7], ["14d", 14]];
  const seg = (items, current, onpick) => el("div", { class: "seg" }, ...items.map(([id, label]) =>
    el("button", { "aria-pressed": String(current === id), onclick: () => { onpick(id); render(); } }, label)));
  const realms = REALM_ORDER.filter((r) => history.some((h) => h.realm_slug === r));
  const legend = el("div", { class: "legend" }, ...realms.map((r) => el("label", { style: `color:${REALM_COLOR[r]}` },
    el("input", { type: "checkbox", checked: c.hidden.has(r) ? null : "", onchange: (e) => { if (e.target.checked) c.hidden.delete(r); else c.hidden.add(r); render(); } }),
    el("span", { class: "key" }), el("span", { style: "color:var(--ink-2)" }, r))));
  card.append(el("div", { class: "chart-controls" },
    seg(ranges.map(([id]) => [id, id]), c.range, (id) => { c.range = id; }),
    seg([["min_unit_price", "Cheapest"], ["median_unit_price", "Median"]], c.measure, (id) => { c.measure = id; }),
    legend));

  const days = ranges.find(([id]) => id === c.range)[1];
  const cutoff = Date.now() - days * 86400000;
  const series = realms.filter((r) => !c.hidden.has(r)).map((realm) => ({
    realm, color: REALM_COLOR[realm],
    points: history.filter((h) => h.realm_slug === realm && new Date(h.fetched_at).getTime() >= cutoff)
      .map((h) => ({ t: new Date(h.fetched_at).getTime(), v: h[c.measure] / COPPER, n: h.listing_count })),
  })).filter((s) => s.points.length);

  if (!series.length) { card.append(el("div", { class: "chart-empty" }, "No data in this range.")); return card; }
  card.append(lineChart(series));

  const rows = history.filter((h) => new Date(h.fetched_at).getTime() >= cutoff).sort((a, b) => b.fetched_at.localeCompare(a.fetched_at));
  card.append(el("details", { class: "tv" }, el("summary", {}, `Table view (${rows.length} rows)`),
    el("div", { class: "table-wrap" }, el("table", {},
      el("thead", {}, el("tr", {}, el("th", { class: "left" }, "Time"), el("th", { class: "left" }, "Realm"), el("th", {}, "Cheapest"), el("th", {}, "Median"), el("th", {}, "Listed"))),
      el("tbody", {}, ...rows.map((h) => el("tr", {}, el("td", { class: "left" }, new Date(h.fetched_at).toLocaleString()), el("td", { class: "left" }, h.realm_slug),
        el("td", {}, goldFull(h.min_unit_price)), el("td", {}, goldFull(h.median_unit_price)), el("td", {}, h.listing_count))))))));
  return card;
}

function lineChart(series) {
  const W = 1000, H = 280, P = { l: 64, r: 16, t: 12, b: 28 };
  const all = series.flatMap((s) => s.points);
  const tMin = Math.min(...all.map((p) => p.t)), tMax = Math.max(...all.map((p) => p.t));
  const vMax = Math.max(...all.map((p) => p.v)), vMinRaw = Math.min(...all.map((p) => p.v));
  const vMin = Math.max(0, vMinRaw - (vMax - vMinRaw) * 0.1);
  const vSpan = (vMax - vMin) || 1, tSpan = (tMax - tMin) || 1;
  const x = (t) => P.l + ((t - tMin) / tSpan) * (W - P.l - P.r);
  const y = (v) => P.t + (1 - (v - vMin) / vSpan) * (H - P.t - P.b);

  const svgNS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(svgNS, "svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.setAttribute("role", "img");
  const mk = (tag, attrs) => { const n = document.createElementNS(svgNS, tag); for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v); return n; };

  const ticks = niceTicks(vMin, vMax, 4);
  for (const v of ticks) {
    svg.append(mk("line", { x1: P.l, x2: W - P.r, y1: y(v), y2: y(v), stroke: "var(--grid)", "stroke-width": 1 }));
    const label = mk("text", { x: P.l - 8, y: y(v) + 4, "text-anchor": "end", fill: "var(--muted)", "font-size": 11 });
    label.textContent = gold(v * COPPER); svg.append(label);
  }
  svg.append(mk("line", { x1: P.l, x2: W - P.r, y1: H - P.b, y2: H - P.b, stroke: "var(--axis)", "stroke-width": 1 }));
  const nT = 5;
  for (let i = 0; i <= nT; i++) {
    const t = tMin + (tSpan * i) / nT;
    const label = mk("text", { x: x(t), y: H - 8, "text-anchor": i === 0 ? "start" : i === nT ? "end" : "middle", fill: "var(--muted)", "font-size": 11 });
    const d = new Date(t);
    label.textContent = tSpan > 2 * 86400000 ? d.toLocaleDateString(undefined, { month: "short", day: "numeric" }) : d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
    svg.append(label);
  }
  for (const s of series) {
    const pts = s.points.slice().sort((a, b) => a.t - b.t);
    const d = pts.map((p, i) => `${i ? "L" : "M"}${x(p.t).toFixed(1)},${y(p.v).toFixed(1)}`).join(" ");
    svg.append(mk("path", { d, fill: "none", stroke: s.color, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }));
    const last = pts[pts.length - 1];
    svg.append(mk("circle", { cx: x(last.t), cy: y(last.v), r: 4, fill: s.color, stroke: "var(--surface)", "stroke-width": 2 }));
  }
  const cross = mk("line", { y1: P.t, y2: H - P.b, stroke: "var(--axis)", "stroke-width": 1, visibility: "hidden" });
  svg.append(cross);

  const wrap = el("div", { class: "chart" });
  const tip = el("div", { class: "tip" });
  wrap.append(svg, tip);
  const times = [...new Set(all.map((p) => p.t))].sort((a, b) => a - b);
  svg.addEventListener("pointermove", (e) => {
    const rect = svg.getBoundingClientRect();
    const px = ((e.clientX - rect.left) / rect.width) * W;
    const t = tMin + ((px - P.l) / (W - P.l - P.r)) * tSpan;
    let best = times[0];
    for (const tt of times) if (Math.abs(tt - t) < Math.abs(best - t)) best = tt;
    cross.setAttribute("x1", x(best)); cross.setAttribute("x2", x(best)); cross.setAttribute("visibility", "visible");
    tip.replaceChildren(el("div", { class: "t" }, new Date(best).toLocaleString()));
    for (const s of series) {
      const p = s.points.find((q) => q.t === best);
      if (!p) continue;
      tip.append(el("div", { class: "row" }, el("span", {}, el("span", { class: "key", style: `background:${s.color}` }), s.realm), el("b", {}, `${goldFull(p.v * COPPER)} · ${p.n}`)));
    }
    tip.style.display = "block";
    const left = (x(best) / W) * rect.width;
    tip.style.left = `${Math.min(left + 12, rect.width - tip.offsetWidth - 4)}px`;
    tip.style.top = `${Math.max(0, e.clientY - rect.top - 10)}px`;
  });
  svg.addEventListener("pointerleave", () => { tip.style.display = "none"; cross.setAttribute("visibility", "hidden"); });
  return wrap;
}
function niceTicks(min, max, count) {
  const span = max - min || 1;
  const raw = span / count;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw);
  const out = [];
  for (let v = Math.ceil(min / step) * step; v <= max; v += step) out.push(v);
  return out;
}

// ---------- routing ----------
function render() {
  if (!state.spreads) return;
  if (state.detail) renderDetail(); else renderList();
}
function route() {
  const h = location.hash.slice(1);
  const m = h.match(/^item\/(\d+)$/);
  if (m) { openDetail(Number(m[1])); return; }
  if (["spreads", "timing", "favorites"].includes(h)) state.tab = h;
  state.detail = null;
  render();
}
window.addEventListener("hashchange", route);
load().then(route).catch((e) => {
  document.getElementById("meta").textContent = `failed to load data: ${e.message}`;
});
