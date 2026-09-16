"use strict";

const COPPER = 10_000;
const GOAL_GOLD = 7_000_000;
const PAGE_SIZE = 100;
const HISTORY_SHARDS = 256;
const REALM_ORDER = ["ravencrest", "frostmane", "darkspear", "silvermoon", "sylvanas"];
const REALM_COLOR = Object.fromEntries(REALM_ORDER.map((r, i) => [r, `var(--s${i + 1})`]));
const QUALITY_ORDER = ["POOR", "COMMON", "UNCOMMON", "RARE", "EPIC", "LEGENDARY", "ARTIFACT", "HEIRLOOM"];
const STAT_NAMES = { 32: "Crit", 36: "Haste", 40: "Vers", 49: "Mastery" };

const state = {
  tab: "dashboard",
  spreads: null,
  timing: null,
  items: new Map(),
  bonuses: {},
  generatedAt: null,
  favorites: new Set(loadFavorites()),
  filters: Object.assign({
    maxBuy: 100000, minProfit: 100, minScore: 0, minSoldEvidence: 1,
    quality: "", itemClass: "", itemSubclass: "", slot: "", search: "", namedOnly: false,
  }, loadJSON("cw.filters", {})),
  sort: { spreads: ["score_per_day", -1], timing: ["score_per_day", -1], favorites: ["name", 1] },
  visible: { spreads: PAGE_SIZE, timing: PAGE_SIZE, favorites: PAGE_SIZE },
  detail: null,
  chart: { range: "7d", measure: "min", hidden: new Set() },
  trades: null,
  networth: null,
  ledgerForm: Object.assign({ character: "", realm: REALM_ORDER[0], action: "buy", itemQuery: "",
    selectedItem: null, variant: "", price: "", quantity: 1, notes: "" }, loadJSON("cw.ledgerFormDefaults", {})),
};

// ---------- utils ----------
function loadJSON(key, fallback) {
  try { const v = localStorage.getItem(key); return v ? JSON.parse(v) : fallback; } catch { return fallback; }
}
function saveJSON(key, value) {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch {}
}
function loadFavorites() {
  // Older builds stored bare item IDs; variant-aware keys are "id|variant".
  return loadJSON("cw.favorites", []).map((v) => (typeof v === "number" ? `${v}|` : String(v)));
}
function rowKey(r) { return `${r.item_id}|${r.variant || ""}`; }
function splitKey(key) { const i = key.indexOf("|"); return { itemId: Number(key.slice(0, i)), variant: key.slice(i + 1) }; }
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
  const itemClass = i.item_class || row?.item_class || "";
  const itemSubclass = i.item_subclass || row?.item_subclass || "";
  return {
    name: i.name || row?.name || `Item ${itemId}`,
    quality: i.quality || row?.quality || "",
    itemClass, itemSubclass,
    slot: i.inventory_type || "",
    level: i.item_level || null,
    cls: [itemClass, itemSubclass].filter(Boolean).join(" · "),
    icon: i.icon_url || "",
    named: Boolean(i.name || row?.name),
  };
}
function cap(s) { return s ? s.charAt(0).toUpperCase() + s.slice(1).toLowerCase() : s; }
function titleCase(s) { return s.split(" ").map(cap).join(" "); }
function slotLabel(slot) {
  return titleCase(slot.replace(/_/g, " ").replace(/non equip/i, "not equippable"));
}

// ---------- variants ----------
function parseVariant(variant) {
  if (!variant) return { bonus: [], mods: {} };
  const [head, ...modParts] = variant.split("|");
  const bonus = head ? head.split(":").map(Number) : [];
  const mods = {};
  for (const p of modParts) { const [t, v] = p.split("="); mods[Number(t)] = Number(v); }
  return { bonus, mods };
}
// Human label for a variant from the published bonus table; incomplete by
// design (item-level curves are not decodable client-side) - the Wowhead
// tooltip carries the exact result.
function variantLabel(variant, baseLevel) {
  const { bonus, mods } = parseVariant(variant);
  const parts = [];
  let ilvl = null, delta = 0;
  for (const id of bonus) {
    const b = state.bonuses[id];
    if (!b) continue;
    if (b.itemLevel?.amount) ilvl = b.itemLevel.amount;
    if (b.level) delta += b.level;
    if (b.levelOffset?.amount) delta += b.levelOffset.amount;
    if (b.tag) parts.push(b.tag);
    if (b.name) parts.push(b.name);
    if (b.socket) parts.push("socket");
    if (b.stats && /Leech|Avoidance|RunSpeed|Indestructible/.test(b.stats)) parts.push(b.stats.replace(/^100% /, "").replace(/ \[.*$/, "").replace("RunSpeed", "Speed"));
  }
  if (ilvl) parts.unshift(`ilvl ${ilvl}`);
  else if (delta && baseLevel) parts.unshift(`ilvl ${baseLevel + delta}`);
  else if (delta) parts.unshift(`${delta > 0 ? "+" : ""}${delta} ilvl`);
  const stats = [mods[29], mods[30]].filter(Boolean).map((s) => STAT_NAMES[s] || `stat ${s}`);
  if (stats.length) parts.push(stats.join("/"));
  if (mods[9]) parts.push(`lvl ${mods[9]}`);
  if (!parts.length && bonus.length) parts.push(`variant ${bonus.join(":")}`);
  return parts.join(" · ");
}
function whLink(itemId, variant, text) {
  const { bonus } = parseVariant(variant);
  const href = `https://www.wowhead.com/item=${itemId}${bonus.length ? `&bonus=${bonus.join(":")}` : ""}`;
  return el("a", { class: "wh", href, target: "_blank", rel: "noopener", onclick: (e) => e.stopPropagation() }, text);
}
function refreshTooltips() {
  if (window.$WowheadPower?.refreshLinks) window.$WowheadPower.refreshLinks();
}

// ---------- data ----------
async function fetchJSON(path) {
  const r = await fetch(`data/${path}`, { cache: "no-cache" });
  if (!r.ok) throw new Error(`${path}: ${r.status}`);
  return r.json();
}

// ---------- ledger ----------
function getLedgerToken() { try { return localStorage.getItem("cw.ledgerToken") || ""; } catch { return ""; } }
function setLedgerToken(v) { try { if (v) localStorage.setItem("cw.ledgerToken", v); else localStorage.removeItem("cw.ledgerToken"); } catch {} }
async function loadTrades() {
  const r = await fetch("api/ledger", { cache: "no-cache" });
  if (!r.ok) throw new Error(`ledger: ${r.status}`);
  const data = await r.json();
  state.trades = data.trades.sort((a, b) => b.ts.localeCompare(a.ts));
}
async function submitTrade(trade) {
  const token = getLedgerToken();
  if (!token) throw new Error("No ledger token set. Click “Set ledger token” first.");
  const r = await fetch("api/ledger", {
    method: "POST",
    headers: { "content-type": "application/json", "x-ledger-token": token },
    body: JSON.stringify(trade),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `ledger: ${r.status}`);
  return data.trade;
}
async function deleteTrade(id) {
  const token = getLedgerToken();
  if (!token) throw new Error("No ledger token set.");
  const r = await fetch(`api/ledger/${encodeURIComponent(id)}`, {
    method: "DELETE",
    headers: { "x-ledger-token": token },
  });
  if (!r.ok) { const data = await r.json().catch(() => ({})); throw new Error(data.error || `ledger: ${r.status}`); }
}
async function loadNetworth() {
  const r = await fetch("api/networth", { cache: "no-cache" });
  if (!r.ok) throw new Error(`networth: ${r.status}`);
  state.networth = await r.json();
}
async function saveLiquidGold(goldValue) {
  const token = getLedgerToken();
  if (!token) throw new Error("No ledger token set.");
  const r = await fetch("api/networth", {
    method: "PUT",
    headers: { "content-type": "application/json", "x-ledger-token": token },
    body: JSON.stringify({ liquid_gold: Math.round(goldValue * COPPER) }),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `networth: ${r.status}`);
  state.networth = data;
}
async function load() {
  const [spreads, timing, items, bonuses] = await Promise.all([
    fetchJSON("latest/spreads.json"), fetchJSON("latest/timing.json"), fetchJSON("items.json"),
    fetchJSON("bonuses.json").catch(() => ({ bonuses: {} })),
  ]);
  state.spreads = spreads.rows.map((r) => ({ ...r, variant: r.variant || "" }));
  state.timing = timing.rows.map((r) => ({ ...r, variant: r.variant || "" }));
  state.generatedAt = spreads.generated_at;
  for (const it of items.items) state.items.set(it.item_id, it);
  state.bonuses = bonuses.bonuses || {};
  document.getElementById("meta").textContent = `updated ${ago(state.generatedAt)} · ${state.spreads.length} spreads · ${state.timing.length} timing flips`;
  document.getElementById("realms").textContent = REALM_ORDER.map(cap).join(" · ");
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
    if (f.itemClass && info.itemClass !== f.itemClass) return false;
    if (f.itemSubclass && info.itemSubclass !== f.itemSubclass) return false;
    if (f.slot && info.slot !== f.slot) return false;
    if (r.buy_price > f.maxBuy * COPPER) return false;
    if (r.net_profit < f.minProfit * COPPER) return false;
    if (r.score_per_day < f.minScore * COPPER) return false;
    if (r.sold_7d < f.minSoldEvidence) return false;
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
let searchTimer = null;
function filterBar(kind, source) {
  const f = state.filters;
  const set = (key, value) => { f[key] = value; saveJSON("cw.filters", f); state.visible[kind] = PAGE_SIZE; render(); };
  const setDebounced = (key, value) => { clearTimeout(searchTimer); searchTimer = setTimeout(() => set(key, value), 200); };
  const num = (label, key, opts = {}) => el("label", {}, label,
    el("input", { type: "number", value: f[key], min: 0, step: opts.step || 1, "data-fkey": `filter-${key}`,
      oninput: (e) => setDebounced(key, Number(e.target.value) || 0) }));
  const select = (label, key, values, format = (v) => v) => el("label", {}, label,
    el("select", { onchange: (e) => set(key, e.target.value) },
      el("option", { value: "" }, "any"),
      ...values.map((v) => el("option", { value: v, selected: f[key] === v ? "" : null }, format(v)))));

  const infos = source.map((r) => itemInfo(r.item_id, r));
  const uniq = (xs) => [...new Set(xs.filter(Boolean))].sort();
  const classes = uniq(infos.map((i) => i.itemClass));
  const subclasses = uniq(infos.filter((i) => !f.itemClass || i.itemClass === f.itemClass).map((i) => i.itemSubclass));
  const slots = uniq(infos.filter((i) => !f.itemClass || i.itemClass === f.itemClass).map((i) => i.slot));
  if (f.itemSubclass && !subclasses.includes(f.itemSubclass)) f.itemSubclass = "";

  return el("div", { class: "filters" },
    el("label", {}, "Search", el("input", { class: "wide", type: "search", value: f.search, placeholder: "item name", "data-fkey": "filter-search",
      oninput: (e) => setDebounced("search", e.target.value) })),
    select("Type", "itemClass", classes),
    select("Subtype", "itemSubclass", subclasses),
    select("Slot", "slot", slots, slotLabel),
    select("Quality", "quality", QUALITY_ORDER.filter((q) => infos.some((i) => i.quality === q)), cap),
    num("Max buy (g)", "maxBuy", { step: 1000 }),
    num("Min net profit (g)", "minProfit", { step: 100 }),
    num("Min profit/day (g)", "minScore", { step: 10 }),
    num("Min sales seen (7d)", "minSoldEvidence"),
    el("label", { class: "check" }, el("input", { type: "checkbox", checked: f.namedOnly ? "" : null, onchange: (e) => set("namedOnly", e.target.checked) }), "named items only"),
    el("button", { class: "reset", onclick: () => { for (const k of ["quality", "itemClass", "itemSubclass", "slot", "search"]) f[k] = ""; saveJSON("cw.filters", f); render(); } }, "clear"),
  );
}

// ---------- tables ----------
function starButton(key) {
  const on = state.favorites.has(key);
  return el("button", { class: `star${on ? " on" : ""}`, title: on ? "Remove favorite" : "Add favorite",
    onclick: (e) => { e.stopPropagation(); toggleFavorite(key); } }, on ? "★" : "☆");
}
function toggleFavorite(key) {
  if (state.favorites.has(key)) state.favorites.delete(key); else state.favorites.add(key);
  saveJSON("cw.favorites", [...state.favorites]);
  render();
}
function itemCell(r) {
  const info = itemInfo(r.item_id, r);
  const variant = variantLabel(r.variant, info.level);
  return el("td", { class: "item left" },
    info.icon ? whLink(r.item_id, r.variant, el("img", { src: info.icon, alt: "", loading: "lazy" })) : el("span", { style: "width:24px;height:24px" }),
    el("div", { class: "body" },
      el("div", { class: "name" }, whLink(r.item_id, r.variant, info.name)),
      variant ? el("div", { class: "variant" }, variant) : null,
      el("div", { class: "sub" }, [cap(info.quality), info.cls, info.slot ? slotLabel(info.slot) : "", info.level && !variant.startsWith("ilvl") ? `Item Level ${info.level}` : ""].filter(Boolean).join(" · "))));
}
function realmCell(realm, price, listings) {
  return el("td", {}, el("div", {}, goldFull(price)),
    el("div", { class: "realm" }, `${cap(realm)} · ${listings} listed`));
}
function table(kind, rows, columns) {
  const [sortKey, sortDir] = state.sort[kind];
  const sorted = sortRows(rows, [sortKey, sortDir]);
  const head = el("tr", {}, el("th", {}, ""), ...columns.map((c) => el("th", {
    class: `${c.left ? "left " : ""}${c.key === sortKey ? "sorted" : ""} ${sortDir > 0 && c.key === sortKey ? "asc" : ""}`,
    title: c.hint || null,
    onclick: () => { state.sort[kind] = [c.key, c.key === sortKey ? -sortDir : (c.defaultDir || -1)]; render(); },
  }, c.label)));
  const shown = sorted.slice(0, state.visible[kind]);
  const body = shown.map((r) => el("tr", { onclick: () => openDetail(rowKey(r)) },
    el("td", {}, starButton(rowKey(r))), ...columns.map((c) => c.cell(r))));
  const remaining = sorted.length - shown.length;
  return el("div", { class: "card table-wrap" }, el("table", {}, el("thead", {}, head), el("tbody", {}, ...body)),
    rows.length ? null : el("div", { class: "empty" }, "Nothing matches the current filters."),
    remaining > 0 ? el("div", { class: "more" }, el("button", { onclick: () => { state.visible[kind] += PAGE_SIZE; render(); } },
      `Show ${Math.min(PAGE_SIZE, remaining)} more (${remaining} remaining)`)) : null);
}
// Sold-side cell: the realized 7d sold median is the number the ranking is
// built on; current ask is shown only as secondary context (it's a wish).
function soldCell(r, showRealm) {
  // current_ask only exists on spread rows (the sell-realm's cheapest current
  // listing); timing rows already show that same number in the "Buy now"
  // column, so it's omitted there instead of rendering "ask NaN".
  const askPart = showRealm ? ` · ask ${gold(r.current_ask)} x${r.sell_listings}` : "";
  return el("td", {},
    el("div", {}, `Sold @ ${goldFull(r.sold_median_7d)}`),
    showRealm ? el("div", { class: "realm" }, `on ${cap(r.sell_realm)}`) : null,
    el("div", { class: "realm" }, `${r.sold_7d}× in 7d${askPart}`));
}
function evDayCell(r) {
  return el("td", { class: r.score_per_day > 0 ? "pos" : "neg" }, goldFull(r.score_per_day) + "/d",
    el("div", { class: "realm" }, `${pct(r.p_sold_48h)} in 48h · ~${r.days_to_sell}d wait`));
}
function riskCell(r) {
  return el("td", { class: r.net_profit > 0 ? "pos" : "neg" }, goldFull(r.net_profit),
    el("div", { class: "realm" }, `deposit risk ${goldFull(r.deposit_estimate)}`));
}
const SPREAD_COLS = [
  { key: "name", label: "Item", left: true, defaultDir: 1, cell: (r) => itemCell(r) },
  { key: "buy_price", label: "Buy", hint: "Cheapest current listing on the buy realm - the price you'd pay now.",
    cell: (r) => realmCell(r.buy_realm, r.buy_price, r.buy_listings) },
  { key: "sold_median_7d", label: "Sell on", hint: "The realm and price this exact variant actually sold for (median of the last 7 days), not the current asking price. The × count is how many confirmed sales back that number.",
    cell: (r) => soldCell(r, true) },
  { key: "score_per_day", label: "Expected Profit / Day", hint: "Expected value per day your buy gold is tied up: (probability it sells within 48h × net profit) minus (probability it doesn't × deposit loss), divided by expected days-to-sell. “Expected” means probability-weighted, not a guess. This is what the list is sorted by.",
    cell: evDayCell },
  { key: "net_profit", label: "Net if Sold", hint: "Profit if the sale happens at the 7-day sold price, before weighting by the odds of that happening (see Expected Profit / Day for the risk-adjusted number).",
    cell: riskCell },
  { key: "realm_count", label: "Realms", hint: "How many of the 5 tracked realms currently have any listing of this exact item variant.",
    cell: (r) => el("td", {}, r.realm_count) },
];
const TIMING_COLS = [
  { key: "name", label: "Item", left: true, defaultDir: 1, cell: (r) => itemCell(r) },
  { key: "realm_slug", label: "Realm", left: true, defaultDir: 1, cell: (r) => el("td", { class: "left" }, cap(r.realm_slug)) },
  { key: "buy_price", label: "Buy now", hint: "Cheapest current listing on this realm.",
    cell: (r) => el("td", {}, goldFull(r.buy_price), el("div", { class: "realm" }, `${r.sell_listings} listed`)) },
  { key: "p50", label: "Normal ask (p50)", hint: "This variant's typical asking price on this realm (median over its trailing history) - what “priced below normal” is measured against.",
    cell: (r) => el("td", {}, goldFull(r.p50), el("div", { class: "realm" }, `${gold(r.p25)}–${gold(r.p75)} · ${r.source === "daily" ? `${r.days} d` : `${r.samples} h`}`)) },
  { key: "discount", label: "Discount", hint: "How far the current price sits below the normal ask (p50).",
    cell: (r) => el("td", { class: "pos" }, pct(r.discount)) },
  { key: "sold_median_7d", label: "Sell (7d evidence)", hint: "What this variant actually sold for on this realm (median of the last 7 days) - the price used for profit, not the current ask.",
    cell: (r) => soldCell(r, false) },
  { key: "score_per_day", label: "Expected Profit / Day", hint: "Expected value per day your buy gold is tied up: (probability it sells within 48h × net profit) minus (probability it doesn't × deposit loss), divided by expected days-to-sell. “Expected” means probability-weighted, not a guess. This is what the list is sorted by.",
    cell: evDayCell },
  { key: "net_profit", label: "Net if Sold", hint: "Profit if the sale happens at the 7-day sold price, before weighting by the odds of that happening (see Expected Profit / Day for the risk-adjusted number).",
    cell: riskCell },
  { key: "turnover_events", label: "Turnover", hint: "Hour-to-hour listing-count drops in the lookback window - a rough liquidity signal.",
    cell: (r) => el("td", {}, r.turnover_events) },
  { key: "zscore", label: "z", cell: (r) => el("td", {}, r.zscore) },
];
const FAV_COLS = [
  { key: "name", label: "Item", left: true, defaultDir: 1, cell: (r) => itemCell(r) },
  { key: "spread", label: "Best spread", cell: (r) => r.spread ? realmCell(r.spread.buy_realm, r.spread.buy_price, r.spread.buy_listings) : el("td", { class: "realm" }, "—") },
  { key: "spread_net", label: "Spread Profit/Day", cell: (r) => el("td", { class: r.spread ? "pos" : "" }, r.spread ? `${goldFull(r.spread.score_per_day)}/d` : "—") },
  { key: "timing_net", label: "Timing Profit/Day", cell: (r) => el("td", { class: r.timing ? "pos" : "" }, r.timing ? `${goldFull(r.timing.score_per_day)}/d on ${cap(r.timing.realm_slug)}` : "—") },
];

// ---------- views ----------
function renderList() {
  const app = document.getElementById("app");
  app.replaceChildren();
  const tabs = el("nav", { class: "tabs", role: "tablist" }, ...[
    ["dashboard", "Dashboard"], ["spreads", "Cross-realm flips"], ["timing", "Timing flips"],
    ["favorites", `Favorites (${state.favorites.size})`], ["ledger", "Ledger"], ["holdings", "Holdings & Goal"],
  ].map(([id, label]) => el("button", { role: "tab", "aria-selected": String(state.tab === id), onclick: () => { state.tab = id; location.hash = id; render(); } }, label)));
  app.append(tabs);

  if (state.tab === "dashboard") { renderDashboard(app); return; }
  if (state.tab === "ledger") { renderLedger(app); return; }
  if (state.tab === "holdings") { renderHoldings(app); return; }

  if (state.tab === "favorites") {
    const rows = [...state.favorites].map((key) => {
      const { itemId, variant } = splitKey(key);
      const spread = sortRows(state.spreads.filter((r) => rowKey(r) === key), ["score_per_day", -1])[0] || null;
      const timing = sortRows(state.timing.filter((r) => rowKey(r) === key), ["score_per_day", -1])[0] || null;
      return { item_id: itemId, variant, spread, timing, spread_net: spread?.score_per_day ?? -1, timing_net: timing?.score_per_day ?? -1 };
    });
    app.append(el("p", { class: "count" }, rows.length ? `${rows.length} favorites` : "Star items in the other tabs or on an item page to track them here."));
    app.append(table("favorites", rows, FAV_COLS));
    return;
  }
  const kind = state.tab;
  const source = kind === "spreads" ? state.spreads : state.timing;
  const rows = applyFilters(source, kind);
  app.append(filterBar(kind, source));
  app.append(el("p", { class: "count" }, `${rows.length} of ${source.length} shown`));
  if (kind === "timing" && !source.length) {
    app.append(el("div", { class: "card" }, el("div", { class: "empty" }, "No timing flips yet — this needs a day or two of hourly history before dips are measurable.")));
    return;
  }
  app.append(table(kind, rows, kind === "spreads" ? SPREAD_COLS : TIMING_COLS));
}

async function openDetail(key) {
  location.hash = `item/${encodeURIComponent(key)}`;
  state.detail = { key, history: null };
  render();
  const { itemId } = splitKey(key);
  try {
    const shard = await fetchJSON(`history/${itemId % HISTORY_SHARDS}.json`);
    if (state.detail?.key === key) { state.detail.history = shard.items[key] || { hourly: [], daily: [] }; render(); }
  } catch {
    if (state.detail?.key === key) { state.detail.history = { hourly: [], daily: [] }; render(); }
  }
}

function renderDetail() {
  const app = document.getElementById("app");
  app.replaceChildren();
  const { key, history } = state.detail;
  const { itemId, variant } = splitKey(key);
  const info = itemInfo(itemId);
  const vlabel = variantLabel(variant, info.level);
  app.append(el("button", { class: "back", onclick: () => { state.detail = null; location.hash = state.tab; render(); } }, "← back"));
  app.append(el("div", { class: "detail-head" },
    info.icon ? whLink(itemId, variant, el("img", { src: info.icon, alt: "" })) : null,
    el("div", {}, el("h2", {}, whLink(itemId, variant, info.name), " ", starButton(key)),
      vlabel ? el("div", { class: "variant" }, vlabel) : null,
      el("div", { class: "sub" }, [cap(info.quality), info.cls, `Item ${itemId}`].filter(Boolean).join(" · "))),
  ));

  if (history === null) { app.append(el("div", { class: "chart-empty" }, "loading history…")); return; }

  const latestByRealm = new Map();
  for (const r of history.hourly) {
    const cur = latestByRealm.get(r.realm_slug);
    if (!cur || r.fetched_at > cur.fetched_at) latestByRealm.set(r.realm_slug, r);
  }
  const tiles = REALM_ORDER.filter((r) => latestByRealm.has(r)).map((realm) => {
    const r = latestByRealm.get(realm);
    return el("div", { class: "tile" },
      el("div", { class: "l" }, el("span", { class: "dot", style: `background:${REALM_COLOR[realm]}` }), cap(realm)),
      el("div", { class: "v" }, goldFull(r.min_unit_price)),
      el("div", { class: "d" }, `median ${goldFull(r.median_unit_price)} · ${r.listing_count} listed · ${ago(r.fetched_at)}`));
  });
  const hasAny = history.hourly.length || history.daily.length;
  app.append(el("div", { class: "grid" }, ...tiles, tiles.length ? null : el("div", { class: "tile" }, el("div", { class: "d" },
    hasAny ? "Not listed on any tracked realm in the last 3 days." : "No published history for this variant yet — histories cover variants that appeared in the lists within the last 7 days."))));

  const sales = history.sales || [];
  if (sales.length) {
    const byRealm = new Map();
    for (const s of sales) {
      const cur = byRealm.get(s.realm_slug) || { sold: 0, relist: 0, expired: 0, medians: [] };
      cur.sold += s.sold_count; cur.relist += s.relist_count; cur.expired += s.expired_count;
      if (s.sold_median_price) cur.medians.push(s.sold_median_price);
      byRealm.set(s.realm_slug, cur);
    }
    const days = new Set(sales.map((s) => s.day)).size;
    app.append(el("div", { class: "grid" }, ...REALM_ORDER.filter((r) => byRealm.has(r)).map((realm) => {
      const s = byRealm.get(realm);
      const med = s.medians.length ? s.medians.sort((a, b) => a - b)[Math.floor(s.medians.length / 2)] : null;
      return el("div", { class: "tile" },
        el("div", { class: "l" }, el("span", { class: "dot", style: `background:${REALM_COLOR[realm]}` }), `${cap(realm)} sales · ${days} d`),
        el("div", { class: "v" }, `${s.sold} sold`),
        el("div", { class: "d" }, [med ? `median ${goldFull(med)}` : "", `${s.relist} relists`, `${s.expired} expired`].filter(Boolean).join(" · ")));
    })));
  }

  const spread = sortRows(state.spreads.filter((r) => rowKey(r) === key), ["score_per_day", -1])[0];
  if (spread) {
    app.append(el("div", { class: "grid" },
      el("div", { class: "tile" }, el("div", { class: "l" }, "Best cross-realm flip"), el("div", { class: "v pos" }, `${goldFull(spread.score_per_day)}/day`),
        el("div", { class: "d" }, `buy ${spread.buy_realm} ${goldFull(spread.buy_price)} → sold ${spread.sell_realm} @ ${goldFull(spread.sold_median_7d)} (${spread.sold_7d}× in 7d) · net ${goldFull(spread.net_profit)} · P(sold 48h) ${pct(spread.p_sold_48h)}`))));
  }

  if (hasAny) app.append(chartCard(history));
}

// ---------- ledger view ----------
function itemSearchResults(query) {
  const q = query.trim().toLowerCase();
  if (q.length < 2) return [];
  const out = [];
  for (const it of state.items.values()) {
    if (it.name && it.name.toLowerCase().includes(q)) {
      out.push(it);
      if (out.length >= 8) break;
    }
  }
  return out;
}

function renderLedgerForm(app) {
  const f = state.ledgerForm;
  const persist = () => saveJSON("cw.ledgerFormDefaults", { character: f.character, realm: f.realm });
  const setField = (key, value) => { f[key] = value; render(); };

  const itemField = el("div", { class: "item-search" },
    el("label", {}, "Item",
      el("input", {
        type: "text", value: f.itemQuery, placeholder: "search item name", "data-fkey": "ledger-item-query",
        oninput: (e) => { f.itemQuery = e.target.value; f.selectedItem = null; render(); },
      })),
    f.selectedItem ? el("div", { class: "picked" },
      f.selectedItem.icon_url ? el("img", { src: f.selectedItem.icon_url, alt: "" }) : null,
      f.selectedItem.name,
      el("button", { type: "button", class: "clear-pick", onclick: () => { f.selectedItem = null; f.itemQuery = ""; render(); } }, "×"))
      : (f.itemQuery.trim().length >= 2 ? el("div", { class: "suggestions" },
          ...itemSearchResults(f.itemQuery).map((it) => el("div", {
            class: "suggestion", onclick: () => { f.selectedItem = it; f.itemQuery = it.name; render(); },
          }, it.icon_url ? el("img", { src: it.icon_url, alt: "" }) : null, it.name)),
          itemSearchResults(f.itemQuery).length === 0 ? el("div", { class: "suggestion muted" }, "no match") : null)
        : null));

  const form = el("form", {
    class: "ledger-form",
    onsubmit: async (e) => {
      e.preventDefault();
      const errBox = document.getElementById("ledger-form-error");
      errBox.textContent = "";
      if (!f.selectedItem) { errBox.textContent = "Pick an item from the search results."; return; }
      const price = Number(f.price);
      const qty = Number(f.quantity);
      if (!(price > 0)) { errBox.textContent = "Price must be a positive number of gold."; return; }
      if (!(qty >= 1)) { errBox.textContent = "Quantity must be at least 1."; return; }
      try {
        await submitTrade({
          character: f.character.trim(), realm: f.realm, action: f.action,
          item_id: f.selectedItem.item_id, item_name: f.selectedItem.name, variant: f.variant.trim(),
          unit_price: Math.round(price * COPPER), quantity: Math.round(qty), notes: f.notes.trim(),
        });
        f.selectedItem = null; f.itemQuery = ""; f.variant = ""; f.price = ""; f.quantity = 1; f.notes = "";
        persist();
        await loadTrades();
        render();
      } catch (err) {
        errBox.textContent = err.message;
      }
    },
  },
    el("div", { class: "ledger-row" },
      el("label", {}, "Character", el("input", { type: "text", required: "", value: f.character, maxlength: 64, "data-fkey": "ledger-character",
        oninput: (e) => setField("character", e.target.value) })),
      el("label", {}, "Realm", el("select", { onchange: (e) => setField("realm", e.target.value) },
        ...REALM_ORDER.map((r) => el("option", { value: r, selected: f.realm === r ? "" : null }, cap(r))))),
      el("label", {}, "Action", el("select", { onchange: (e) => setField("action", e.target.value) },
        el("option", { value: "buy", selected: f.action === "buy" ? "" : null }, "Buy"),
        el("option", { value: "sell", selected: f.action === "sell" ? "" : null }, "Sell"))),
    ),
    itemField,
    el("div", { class: "ledger-row" },
      el("label", {}, "Variant (optional)", el("input", { type: "text", value: f.variant, placeholder: "bonus IDs, leave blank for base", "data-fkey": "ledger-variant",
        oninput: (e) => setField("variant", e.target.value) })),
      el("label", {}, "Unit price (g)", el("input", { type: "number", min: 0, step: "0.01", required: "", value: f.price, "data-fkey": "ledger-price",
        oninput: (e) => setField("price", e.target.value) })),
      el("label", {}, "Quantity", el("input", { type: "number", min: 1, step: 1, required: "", value: f.quantity, "data-fkey": "ledger-quantity",
        oninput: (e) => setField("quantity", e.target.value) })),
    ),
    el("label", { class: "notes" }, "Notes (optional)", el("input", { type: "text", value: f.notes, maxlength: 256, "data-fkey": "ledger-notes",
      oninput: (e) => setField("notes", e.target.value) })),
    el("div", { class: "ledger-row" },
      el("button", { type: "submit", class: "primary" }, `Log ${cap(f.action)}`),
      el("span", { id: "ledger-form-error", class: "form-error" }),
    ),
  );
  app.append(el("div", { class: "card ledger-card" }, form));
}

function renderLedgerTable(app) {
  const trades = state.trades || [];
  if (!trades.length) {
    app.append(el("div", { class: "card" }, el("div", { class: "empty" }, "No trades logged yet.")));
    return;
  }
  const rows = trades.map((t) => el("tr", {},
    el("td", { class: "left" }, new Date(t.ts).toLocaleString()),
    el("td", { class: "left" }, t.character),
    el("td", { class: "left" }, cap(t.realm)),
    el("td", { class: t.action === "buy" ? "neg" : "pos" }, cap(t.action)),
    el("td", { class: "left" }, t.item_name, t.variant ? el("div", { class: "variant" }, t.variant) : null),
    el("td", {}, goldFull(t.unit_price)),
    el("td", {}, t.quantity),
    el("td", {}, goldFull(t.unit_price * t.quantity)),
    el("td", { class: "left" }, t.notes),
    el("td", {}, el("button", {
      class: "star", title: "Delete",
      onclick: async () => {
        if (!confirm(`Delete this ${t.action} of ${t.item_name}?`)) return;
        try { await deleteTrade(t.id); await loadTrades(); render(); }
        catch (err) { alert(err.message); }
      },
    }, "🗑")),
  ));
  app.append(el("div", { class: "card table-wrap" }, el("table", {},
    el("thead", {}, el("tr", {},
      el("th", { class: "left" }, "Time"), el("th", { class: "left" }, "Character"), el("th", { class: "left" }, "Realm"),
      el("th", { class: "left" }, "Action"), el("th", { class: "left" }, "Item"), el("th", {}, "Unit"),
      el("th", {}, "Qty"), el("th", {}, "Total"), el("th", { class: "left" }, "Notes"), el("th", {}, ""))),
    el("tbody", {}, ...rows))));
}

function renderLedger(app) {
  const hasToken = Boolean(getLedgerToken());
  app.append(el("div", { class: "ledger-header" },
    el("p", { class: "count" }, "Manual trade log. See the Holdings & Goal tab for P&L and capital-at-risk."),
    el("button", { class: "reset", onclick: () => {
      const v = prompt(hasToken ? "Replace ledger token (leave blank to clear):" : "Enter ledger write token:", "");
      if (v !== null) { setLedgerToken(v); render(); }
    } }, hasToken ? "Change ledger token" : "Set ledger token"),
  ));
  if (!hasToken) {
    app.append(el("div", { class: "card" }, el("div", { class: "empty" },
      "Set the ledger token to log trades. Reading the existing log below doesn't need one.")));
  } else {
    renderLedgerForm(app);
  }
  if (state.trades === null) {
    app.append(el("div", { class: "card" }, el("div", { class: "empty" }, "loading…")));
  } else {
    renderLedgerTable(app);
  }
}

// ---------- holdings, P&L, capital-at-risk, goal tracker ----------

// FIFO-matches sells against earlier buys, pooled per (item, variant) across
// all characters/realms - both gold and items move freely account-wide via
// the Warband bank, so per-character silos would be artificial. A sell with
// no matching buy lot (item obtained some other way, or logged out of order)
// is flagged rather than silently assumed free.
function computeLedger(trades) {
  const keyOf = (t) => `${t.item_id}|${t.variant || ""}`;
  const lots = new Map();
  const realized = new Map();
  const flagged = [];
  const nameOf = new Map();

  for (const t of trades.slice().sort((a, b) => a.ts.localeCompare(b.ts))) {
    const k = keyOf(t);
    nameOf.set(k, { item_id: t.item_id, variant: t.variant || "", name: t.item_name });

    if (t.action === "buy") {
      if (!lots.has(k)) lots.set(k, []);
      lots.get(k).push({ qty: t.quantity, unit_price: t.unit_price });
      continue;
    }

    const queue = lots.get(k) || [];
    let remaining = t.quantity;
    let profit = 0;
    let costBasisQty = 0;
    while (remaining > 0 && queue.length) {
      const lot = queue[0];
      const take = Math.min(lot.qty, remaining);
      profit += (t.unit_price - lot.unit_price) * take;
      costBasisQty += take;
      lot.qty -= take;
      remaining -= take;
      if (lot.qty <= 0) queue.shift();
    }
    if (remaining > 0) {
      profit += t.unit_price * remaining;
      flagged.push({ trade: t, unmatchedQty: remaining });
    }
    if (!realized.has(k)) realized.set(k, { profit: 0, soldQty: 0, costBasisQty: 0, unmatchedQty: 0 });
    const r = realized.get(k);
    r.profit += profit;
    r.soldQty += t.quantity;
    r.costBasisQty += costBasisQty;
    r.unmatchedQty += t.quantity - costBasisQty;
  }

  const positions = [];
  for (const [k, queue] of lots.entries()) {
    const qty = queue.reduce((s, l) => s + l.qty, 0);
    if (qty <= 0) continue;
    const totalCost = queue.reduce((s, l) => s + l.qty * l.unit_price, 0);
    positions.push({ ...nameOf.get(k), qty, totalCost, avgCost: totalCost / qty });
  }
  const realizedList = [...realized.entries()].map(([k, r]) => ({ ...nameOf.get(k), ...r }));

  return { positions, realized: realizedList, flagged };
}

// Best-effort current value: highest realized sold price seen for this
// variant across tracked realms in today's published lists. Returns null
// (shown as "no live price") rather than guessing when the item isn't
// currently featured in either list.
function estimateLiveValue(item_id, variant) {
  let best = null;
  for (const r of state.spreads) {
    if (r.item_id === item_id && (r.variant || "") === variant) best = Math.max(best ?? 0, r.sold_median_7d ?? 0, r.current_ask ?? 0);
  }
  for (const r of state.timing) {
    if (r.item_id === item_id && (r.variant || "") === variant) best = Math.max(best ?? 0, r.sold_median_7d ?? 0, r.p50 ?? 0);
  }
  return best;
}

// Shared by the Dashboard and Holdings tabs so both read the same numbers.
function computeGoalSummary() {
  const { positions, realized, flagged } = computeLedger(state.trades || []);
  const totalCostBasis = positions.reduce((s, p) => s + p.totalCost, 0);
  let totalLiveValue = 0, positionsWithLivePrice = 0;
  const positionRows = positions.map((p) => {
    const live = estimateLiveValue(p.item_id, p.variant);
    if (live != null) { totalLiveValue += live * p.qty; positionsWithLivePrice++; }
    return { ...p, live };
  }).sort((a, b) => b.totalCost - a.totalCost);
  const liquidGold = (state.networth && state.networth.liquid_gold) || 0;
  const holdingsValue = totalLiveValue || totalCostBasis; // fall back to cost basis when nothing has a live price
  const netWorth = liquidGold + holdingsValue;
  const goalCopper = GOAL_GOLD * COPPER;
  const progress = Math.min(1, netWorth / goalCopper);
  const totalRealized = realized.reduce((s, r) => s + r.profit, 0);
  return { positions: positionRows, realized, flagged, totalCostBasis, totalLiveValue, positionsWithLivePrice,
    liquidGold, holdingsValue, netWorth, goalCopper, progress, totalRealized };
}

function goalCard(g) {
  return el("div", { class: "card goal-card" },
    el("div", { class: "goal-top" },
      el("div", {}, el("div", { class: "l" }, "Net worth"), el("div", { class: "v" }, goldFull(g.netWorth))),
      el("div", {}, el("div", { class: "l" }, "Goal"), el("div", { class: "v" }, goldFull(g.goalCopper))),
      el("div", {}, el("div", { class: "l" }, "Remaining"), el("div", { class: "v" }, goldFull(Math.max(0, g.goalCopper - g.netWorth)))),
    ),
    el("div", { class: "goal-bar" }, el("div", { class: "goal-fill", style: `width:${(g.progress * 100).toFixed(1)}%` })),
    el("div", { class: "count" },
      `${goldFull(g.liquidGold)} liquid${state.networth?.updated_at ? ` (updated ${ago(state.networth.updated_at)})` : " (not set)"} `
      + `+ ${goldFull(g.holdingsValue)} in holdings ${g.totalLiveValue ? `(${g.positionsWithLivePrice}/${g.positions.length} at live price, rest at cost)` : "(cost basis - no live prices found)"}`));
}

function renderHoldings(app) {
  if (state.trades === null || state.networth === null) {
    app.append(el("div", { class: "card" }, el("div", { class: "empty" }, "loading…")));
    return;
  }
  const hasToken = Boolean(getLedgerToken());
  const g = computeGoalSummary();
  const { realized, flagged, totalCostBasis } = g;
  const positionRows = g.positions;
  const liquidGold = g.liquidGold;

  app.append(el("div", { class: "ledger-header" },
    el("p", { class: "count" }, "Computed from the ledger - FIFO cost basis, pooled across characters/realms."),
    hasToken ? el("button", { class: "reset", onclick: async () => {
      const v = prompt("Update liquid gold (across all characters, in gold):", String((liquidGold / COPPER).toFixed(0)));
      if (v === null) return;
      const n = Number(v);
      if (!Number.isFinite(n) || n < 0) { alert("Enter a non-negative number."); return; }
      try { await saveLiquidGold(n); render(); } catch (e) { alert(e.message); }
    } }, "Update liquid gold") : null,
  ));

  app.append(goalCard(g));

  // Capital-at-risk
  app.append(el("h3", { class: "section-h" }, "Capital at risk (open positions)"));
  if (!positionRows.length) {
    app.append(el("div", { class: "card" }, el("div", { class: "empty" }, "No open positions - everything logged as bought has also been logged as sold.")));
  } else {
    app.append(el("div", { class: "card table-wrap" }, el("table", {},
      el("thead", {}, el("tr", {},
        el("th", { class: "left" }, "Item"), el("th", {}, "Qty"), el("th", {}, "Avg cost"), el("th", {}, "Total cost"),
        el("th", {}, "% of capital"), el("th", {}, "Live value"), el("th", {}, "Unrealized"))),
      el("tbody", {}, ...positionRows.map((p) => {
        const pct2 = totalCostBasis ? p.totalCost / totalCostBasis : 0;
        const unrealized = p.live != null ? (p.live - p.avgCost) * p.qty : null;
        return el("tr", {},
          el("td", { class: "left" }, p.name, p.variant ? el("div", { class: "variant" }, p.variant) : null),
          el("td", {}, p.qty),
          el("td", {}, goldFull(p.avgCost)),
          el("td", {}, goldFull(p.totalCost)),
          el("td", {}, pct(pct2)),
          el("td", {}, p.live != null ? goldFull(p.live) : el("span", { class: "muted" }, "—")),
          el("td", { class: unrealized == null ? "muted" : unrealized >= 0 ? "pos" : "neg" }, unrealized != null ? goldFull(unrealized) : "no live price"));
      })))));
  }

  // Realized P&L
  app.append(el("h3", { class: "section-h" }, "Realized P&L"));
  const totalRealized = g.totalRealized;
  app.append(el("div", { class: "card goal-card" },
    el("div", { class: "goal-top" },
      el("div", {}, el("div", { class: "l" }, "Total realized"), el("div", { class: `v ${totalRealized >= 0 ? "pos" : "neg"}` }, goldFull(totalRealized))),
      el("div", {}, el("div", { class: "l" }, "Closed flips"), el("div", { class: "v" }, realized.reduce((s, r) => s + r.soldQty, 0))),
      el("div", {}, el("div", { class: "l" }, "Distinct items"), el("div", { class: "v" }, realized.length)),
    )));
  if (realized.length) {
    app.append(el("div", { class: "card table-wrap" }, el("table", {},
      el("thead", {}, el("tr", {}, el("th", { class: "left" }, "Item"), el("th", {}, "Sold qty"), el("th", {}, "Realized P&L"))),
      el("tbody", {}, ...realized.sort((a, b) => b.profit - a.profit).map((r) => el("tr", {},
        el("td", { class: "left" }, r.name, r.variant ? el("div", { class: "variant" }, r.variant) : null),
        el("td", {}, r.soldQty),
        el("td", { class: r.profit >= 0 ? "pos" : "neg" }, goldFull(r.profit))))))));
  }

  if (flagged.length) {
    app.append(el("div", { class: "card empty" },
      `${flagged.length} sell${flagged.length > 1 ? "s" : ""} had no matching logged buy for part of the quantity `
      + "(item obtained another way, or logged out of order) - counted as pure profit against zero cost for that portion."));
  }
}

// ---------- dashboard ----------
// A compact row for the dashboard's top-opportunities lists - not the full
// sortable table, just enough to act on or click through to the item page.
function highlightRow(r, kind) {
  const info = itemInfo(r.item_id, r);
  const sellLine = kind === "spread"
    ? `Buy ${cap(r.buy_realm)} ${goldFull(r.buy_price)} → sell ${cap(r.sell_realm)} @ ${goldFull(r.sold_median_7d)}`
    : `${cap(r.realm_slug)}: now ${goldFull(r.buy_price)}, normally ${goldFull(r.p50)} (${pct(r.discount)} below)`;
  return el("div", { class: "hl-row", onclick: () => openDetail(rowKey(r)) },
    info.icon ? whLink(r.item_id, r.variant, el("img", { src: info.icon, alt: "", loading: "lazy" })) : null,
    el("div", { class: "hl-body" },
      el("div", { class: "hl-name" }, whLink(r.item_id, r.variant, info.name)),
      el("div", { class: "hl-sub" }, sellLine)),
    el("div", { class: `hl-score ${r.score_per_day > 0 ? "pos" : "neg"}` }, `${goldFull(r.score_per_day)}/d`));
}

function renderDashboard(app) {
  if (state.trades === null || state.networth === null) {
    app.append(el("div", { class: "card" }, el("div", { class: "empty" }, "loading…")));
    return;
  }
  const g = computeGoalSummary();

  app.append(el("h3", { class: "section-h" }, "Goal"));
  app.append(goalCard(g));

  const topSpreads = sortRows(state.spreads, ["score_per_day", -1]).slice(0, 5);
  const topTiming = sortRows(state.timing, ["score_per_day", -1]).slice(0, 5);
  app.append(el("div", { class: "dash-cols" },
    el("div", {},
      el("h3", { class: "section-h" }, "Top cross-realm flips right now"),
      topSpreads.length
        ? el("div", { class: "card" }, ...topSpreads.map((r) => highlightRow(r, "spread")))
        : el("div", { class: "card" }, el("div", { class: "empty" }, "Nothing clears the evidence bar yet.")),
      el("p", { class: "count" }, el("a", { href: "#spreads", onclick: (e) => { e.preventDefault(); state.tab = "spreads"; location.hash = "spreads"; render(); } }, "See full list →"))),
    el("div", {},
      el("h3", { class: "section-h" }, "Top timing flips right now"),
      topTiming.length
        ? el("div", { class: "card" }, ...topTiming.map((r) => highlightRow(r, "timing")))
        : el("div", { class: "card" }, el("div", { class: "empty" }, "None yet - needs more price history to detect real dips.")),
      el("p", { class: "count" }, el("a", { href: "#timing", onclick: (e) => { e.preventDefault(); state.tab = "timing"; location.hash = "timing"; render(); } }, "See full list →"))),
  ));

  const favActive = [...state.favorites].map((key) => {
    const spread = sortRows(state.spreads.filter((r) => rowKey(r) === key), ["score_per_day", -1])[0];
    const timing = sortRows(state.timing.filter((r) => rowKey(r) === key), ["score_per_day", -1])[0];
    const best = [spread, timing].filter(Boolean).sort((a, b) => b.score_per_day - a.score_per_day)[0];
    return best ? { ...best, kind: spread === best ? "spread" : "timing" } : null;
  }).filter(Boolean).sort((a, b) => b.score_per_day - a.score_per_day);
  if (favActive.length) {
    app.append(el("h3", { class: "section-h" }, "Your favorites, active right now"));
    app.append(el("div", { class: "card" }, ...favActive.map((r) => highlightRow(r, r.kind))));
  }

  app.append(el("div", { class: "dash-cols" },
    el("div", {},
      el("h3", { class: "section-h" }, "Capital at risk"),
      el("div", { class: "card goal-card" }, el("div", { class: "goal-top" },
        el("div", {}, el("div", { class: "l" }, "Open positions"), el("div", { class: "v" }, g.positions.length)),
        el("div", {}, el("div", { class: "l" }, "Tied up"), el("div", { class: "v" }, goldFull(g.totalCostBasis))),
        el("div", {}, el("div", { class: "l" }, "Realized so far"), el("div", { class: `v ${g.totalRealized >= 0 ? "pos" : "neg"}` }, goldFull(g.totalRealized))),
      )),
      el("p", { class: "count" }, el("a", { href: "#holdings", onclick: (e) => { e.preventDefault(); state.tab = "holdings"; location.hash = "holdings"; render(); } }, "See holdings & P&L →"))),
    el("div", {},
      el("h3", { class: "section-h" }, "Recent activity"),
      state.trades.length
        ? el("div", { class: "card table-wrap" }, el("table", {},
            el("tbody", {}, ...state.trades.slice().sort((a, b) => b.ts.localeCompare(a.ts)).slice(0, 5).map((t) =>
              el("tr", {},
                el("td", { class: "left" }, ago(t.ts)),
                el("td", { class: t.action === "buy" ? "neg" : "pos" }, cap(t.action)),
                el("td", { class: "left" }, t.item_name),
                el("td", {}, goldFull(t.unit_price * t.quantity)))))))
        : el("div", { class: "card" }, el("div", { class: "empty" }, "No trades logged yet.")),
      el("p", { class: "count" }, el("a", { href: "#ledger", onclick: (e) => { e.preventDefault(); state.tab = "ledger"; location.hash = "ledger"; render(); } }, "Log a trade →"))),
  ));
}

// ---------- chart ----------
function chartCard(history) {
  const c = state.chart;
  const card = el("div", { class: "card chart-card" });
  const ranges = [["24h", 1], ["3d", 3], ["7d", 7], ["30d", 30], ["60d", 60]];
  const seg = (items, current, onpick) => el("div", { class: "seg" }, ...items.map(([id, label]) =>
    el("button", { "aria-pressed": String(current === id), onclick: () => { onpick(id); render(); } }, label)));
  const realms = REALM_ORDER.filter((r) => history.hourly.some((h) => h.realm_slug === r) || history.daily.some((d) => d.realm_slug === r));
  const legend = el("div", { class: "legend" }, ...realms.map((r) => el("label", { style: `color:${REALM_COLOR[r]}` },
    el("input", { type: "checkbox", checked: c.hidden.has(r) ? null : "", onchange: (e) => { if (e.target.checked) c.hidden.delete(r); else c.hidden.add(r); render(); } }),
    el("span", { class: "key" }), el("span", { style: "color:var(--ink-2)" }, cap(r)))));
  card.append(el("div", { class: "chart-controls" },
    seg(ranges.map(([id]) => [id, id]), c.range, (id) => { c.range = id; }),
    seg([["min", "Cheapest"], ["median", "Median"]], c.measure, (id) => { c.measure = id; }),
    legend));

  const days = ranges.find(([id]) => id === c.range)[1];
  const cutoff = Date.now() - days * 86400000;
  // Hourly points cover the last 3 days; daily rollups (plotted at noon UTC)
  // cover the rest, using the day's typical cheapest / mean median price.
  const hourlyStart = history.hourly.length ? Math.min(...history.hourly.map((h) => new Date(h.fetched_at).getTime())) : Infinity;
  const series = realms.filter((r) => !c.hidden.has(r)).map((realm) => {
    const pts = history.hourly.filter((h) => h.realm_slug === realm).map((h) => ({
      t: new Date(h.fetched_at).getTime(), v: (c.measure === "min" ? h.min_unit_price : h.median_unit_price) / COPPER, n: h.listing_count, daily: false }));
    for (const d of history.daily.filter((d) => d.realm_slug === realm)) {
      const t = new Date(`${d.day}T12:00:00Z`).getTime();
      if (t < hourlyStart) pts.push({ t, v: (c.measure === "min" ? d.typical_price : d.median_price) / COPPER, n: Math.round(d.avg_listing_count), daily: true });
    }
    return { realm, color: REALM_COLOR[realm], points: pts.filter((p) => p.t >= cutoff).sort((a, b) => a.t - b.t) };
  }).filter((s) => s.points.length);

  if (!series.length) { card.append(el("div", { class: "chart-empty" }, "No data in this range.")); return card; }
  card.append(lineChart(series));

  const rows = [
    ...history.hourly.map((h) => ({ t: h.fetched_at, realm: h.realm_slug, low: h.min_unit_price, med: h.median_unit_price, n: h.listing_count, kind: "hour" })),
    ...history.daily.map((d) => ({ t: d.day, realm: d.realm_slug, low: d.low_price, med: d.median_price, n: Math.round(d.avg_listing_count), kind: "day" })),
  ].filter((r) => new Date(r.t).getTime() >= cutoff).sort((a, b) => b.t.localeCompare(a.t));
  card.append(el("details", { class: "tv" }, el("summary", {}, `Table view (${rows.length} rows)`),
    el("div", { class: "table-wrap" }, el("table", {},
      el("thead", {}, el("tr", {}, el("th", { class: "left" }, "Time"), el("th", { class: "left" }, "Realm"), el("th", {}, "Cheapest"), el("th", {}, "Median"), el("th", {}, "Listed"))),
      el("tbody", {}, ...rows.map((r) => el("tr", {}, el("td", { class: "left" }, r.kind === "day" ? `${r.t} (day)` : new Date(r.t).toLocaleString()), el("td", { class: "left" }, r.realm),
        el("td", {}, goldFull(r.low)), el("td", {}, goldFull(r.med)), el("td", {}, r.n))))))));
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

  for (const v of niceTicks(vMin, vMax, 4)) {
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
    const pts = s.points;
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
    const anyDaily = all.some((p) => p.t === best && p.daily);
    tip.replaceChildren(el("div", { class: "t" }, anyDaily ? `${new Date(best).toLocaleDateString()} (daily)` : new Date(best).toLocaleString()));
    for (const s of series) {
      const p = s.points.find((q) => q.t === best);
      if (!p) continue;
      tip.append(el("div", { class: "row" }, el("span", {}, el("span", { class: "key", style: `background:${s.color}` }), cap(s.realm)), el("b", {}, `${goldFull(p.v * COPPER)} · ${p.n}`)));
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
// Every render() rebuilds the DOM from scratch, which would normally drop
// focus out of whatever input the user is typing in. Inputs that matter for
// typing carry a stable data-fkey; we snapshot the focused one (and its
// cursor position) before rebuilding and restore it after.
function render() {
  if (!state.spreads) return;
  const active = document.activeElement;
  const fkey = active?.dataset?.fkey;
  const focusInfo = fkey ? { fkey, selStart: active.selectionStart, selEnd: active.selectionEnd } : null;

  if (state.detail) renderDetail(); else renderList();
  refreshTooltips();

  if (focusInfo) {
    const el2 = document.querySelector(`[data-fkey="${focusInfo.fkey}"]`);
    if (el2) {
      el2.focus();
      if (focusInfo.selStart != null && typeof el2.setSelectionRange === "function") {
        try { el2.setSelectionRange(focusInfo.selStart, focusInfo.selEnd); } catch {}
      }
    }
  }
}
function route() {
  const h = location.hash.slice(1);
  const m = h.match(/^item\/(.+)$/);
  if (m) {
    const key = decodeURIComponent(m[1]);
    openDetail(key.includes("|") ? key : `${key}|`);
    return;
  }
  if (["dashboard", "spreads", "timing", "favorites", "ledger", "holdings"].includes(h)) state.tab = h;
  else if (!h) state.tab = "dashboard";
  if ((state.tab === "ledger" || state.tab === "holdings" || state.tab === "dashboard") && state.trades === null) {
    state.trades = []; // avoid re-triggering while the fetch is in flight
    loadTrades().then(render).catch((e) => { state.trades = null; console.error(e); });
  }
  if ((state.tab === "holdings" || state.tab === "dashboard") && state.networth === null) {
    state.networth = {}; // avoid re-triggering while the fetch is in flight
    loadNetworth().then(render).catch((e) => { state.networth = null; console.error(e); });
  }
  state.detail = null;
  render();
}
window.addEventListener("hashchange", route);
load().then(route).catch((e) => {
  document.getElementById("meta").textContent = `failed to load data: ${e.message}`;
});

// Hash routing never triggers a real page load, so a tab left open across a
// deploy silently keeps running the old app.js. Poll app.js's ETag and show
// a banner rather than relying on the user to remember to hard-refresh.
(function watchForUpdates() {
  let currentEtag = null;
  async function check() {
    try {
      const r = await fetch("app.js", { method: "HEAD", cache: "no-store" });
      const etag = r.headers.get("etag");
      if (!etag) return;
      if (currentEtag === null) { currentEtag = etag; return; }
      if (etag !== currentEtag && !document.getElementById("update-banner")) {
        const bar = el("div", { id: "update-banner", style:
          "position:fixed;bottom:0;left:0;right:0;z-index:50;background:var(--accent);color:#fff;" +
          "padding:10px 16px;display:flex;gap:12px;align-items:center;justify-content:center;font-size:13px;" },
          "A new version of CoinWarden is available.",
          el("button", { style: "background:#fff;color:var(--accent);border:0;border-radius:6px;padding:4px 12px;cursor:pointer;font-weight:600;",
            onclick: () => location.reload() }, "Reload"));
        document.body.append(bar);
      }
    } catch {}
  }
  check();
  setInterval(check, 5 * 60 * 1000);
})();
