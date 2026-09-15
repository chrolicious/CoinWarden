const ALLOWED_PREFIXES = ["latest/", "history/", "items.json", "bonuses.json"];
const LEDGER_KEY = "ledger/trades.json";
const MAX_TRADES = 20000; // sanity cap, not a realistic ceiling for personal use

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json" },
  });
}

async function readData(request, env) {
  const url = new URL(request.url);
  if (request.method !== "GET" && request.method !== "HEAD") {
    return new Response("method not allowed", { status: 405 });
  }
  const key = url.pathname.slice("/data/".length);
  if (!ALLOWED_PREFIXES.some((p) => key.startsWith(p))) {
    return new Response("not found", { status: 404 });
  }
  const obj = await env.DATA.get(key);
  if (!obj) return new Response("not found", { status: 404 });

  // Objects are stored gzipped; decompress here and let Cloudflare re-encode
  // for the client, so the browser always receives plain JSON.
  const headers = new Headers();
  headers.set("content-type", obj.httpMetadata?.contentType || "application/json");
  headers.set("cache-control", obj.httpMetadata?.cacheControl || "public, max-age=300");
  headers.set("etag", obj.httpEtag);
  if (request.method === "HEAD") return new Response(null, { headers });
  const body = obj.httpMetadata?.contentEncoding === "gzip"
    ? obj.body.pipeThrough(new DecompressionStream("gzip"))
    : obj.body;
  return new Response(body, { headers });
}

async function getLedger(env) {
  const obj = await env.DATA.get(LEDGER_KEY);
  if (!obj) return { trades: [], etag: null };
  const data = await obj.json();
  return { trades: data.trades || [], etag: obj.httpEtag };
}

async function putLedger(env, trades, etag) {
  const body = JSON.stringify({ trades });
  const opts = { httpMetadata: { contentType: "application/json" } };
  if (etag) opts.onlyIf = { etagMatches: etag };
  return env.DATA.put(LEDGER_KEY, body, opts);
}

// Read-modify-write with a conditional put so a rare concurrent write (e.g.
// two tabs open) fails loudly and retries instead of silently losing a trade.
async function withLedger(env, mutate) {
  for (let attempt = 0; attempt < 5; attempt++) {
    const { trades, etag } = await getLedger(env);
    const result = mutate(trades);
    if (result === null) return null; // mutate signalled "not found" etc.
    try {
      await putLedger(env, trades, etag);
      return result;
    } catch (e) {
      if (attempt === 4) throw e;
    }
  }
}

function requireToken(request, env) {
  const token = request.headers.get("x-ledger-token");
  return env.LEDGER_WRITE_TOKEN && token === env.LEDGER_WRITE_TOKEN;
}

const REQUIRED_TRADE_FIELDS = ["character", "realm", "item_id", "item_name", "action", "unit_price", "quantity"];

async function handleLedger(request, env, id) {
  if (request.method === "GET") {
    const { trades } = await getLedger(env);
    return json({ trades });
  }

  if (!requireToken(request, env)) {
    return json({ error: "unauthorized" }, 401);
  }

  if (request.method === "POST" && !id) {
    let body;
    try {
      body = await request.json();
    } catch {
      return json({ error: "invalid json" }, 400);
    }
    for (const f of REQUIRED_TRADE_FIELDS) {
      if (body[f] === undefined || body[f] === null || body[f] === "") {
        return json({ error: `missing field: ${f}` }, 400);
      }
    }
    if (body.action !== "buy" && body.action !== "sell") {
      return json({ error: "action must be 'buy' or 'sell'" }, 400);
    }
    const trade = {
      id: crypto.randomUUID(),
      ts: new Date().toISOString(),
      character: String(body.character).slice(0, 64),
      realm: String(body.realm).slice(0, 32),
      item_id: Number(body.item_id),
      variant: String(body.variant || "").slice(0, 200),
      item_name: String(body.item_name).slice(0, 128),
      action: body.action,
      unit_price: Math.round(Number(body.unit_price)),
      quantity: Math.max(1, Math.round(Number(body.quantity))),
      notes: String(body.notes || "").slice(0, 256),
    };
    if (!Number.isFinite(trade.item_id) || !Number.isFinite(trade.unit_price) || trade.unit_price < 0) {
      return json({ error: "invalid numeric field" }, 400);
    }
    const result = await withLedger(env, (trades) => {
      if (trades.length >= MAX_TRADES) return null;
      trades.push(trade);
      return trade;
    });
    if (result === null) return json({ error: "ledger full" }, 507);
    return json({ trade: result }, 201);
  }

  if (request.method === "DELETE" && id) {
    const result = await withLedger(env, (trades) => {
      const idx = trades.findIndex((t) => t.id === id);
      if (idx === -1) return null;
      trades.splice(idx, 1);
      return true;
    });
    if (result === null) return json({ error: "not found" }, 404);
    return json({ deleted: id });
  }

  return json({ error: "method not allowed" }, 405);
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname.startsWith("/data/")) return readData(request, env);
    if (url.pathname === "/api/ledger") return handleLedger(request, env, null);
    const m = url.pathname.match(/^\/api\/ledger\/([^/]+)$/);
    if (m) return handleLedger(request, env, decodeURIComponent(m[1]));
    return env.ASSETS.fetch(request);
  },
};
