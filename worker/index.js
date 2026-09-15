const ALLOWED_PREFIXES = ["latest/", "history/", "items.json", "bonuses.json"];

// GitHub drops scheduled workflow runs under load; Cloudflare cron triggers
// are dependable, so the Worker dispatches the scan every hour. The workflow
// itself skips if another run succeeded in the last 50 minutes.
async function dispatchScan(env) {
  if (!env.GITHUB_DISPATCH_TOKEN) {
    console.log("no GITHUB_DISPATCH_TOKEN secret; skipping dispatch");
    return;
  }
  const resp = await fetch("https://api.github.com/repos/chrolicious/CoinWarden/actions/workflows/scan.yml/dispatches", {
    method: "POST",
    headers: {
      "authorization": `Bearer ${env.GITHUB_DISPATCH_TOKEN}`,
      "accept": "application/vnd.github+json",
      "user-agent": "coinwarden-worker",
      "content-type": "application/json",
    },
    body: JSON.stringify({ ref: "master" }),
  });
  console.log(`dispatch: ${resp.status}`);
}

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(dispatchScan(env));
  },

  async fetch(request, env) {
    const url = new URL(request.url);
    if (!url.pathname.startsWith("/data/")) {
      return env.ASSETS.fetch(request);
    }
    if (request.method !== "GET" && request.method !== "HEAD") {
      return new Response("method not allowed", { status: 405 });
    }

    const key = url.pathname.slice("/data/".length);
    if (!ALLOWED_PREFIXES.some((p) => key.startsWith(p))) {
      return new Response("not found", { status: 404 });
    }

    const obj = await env.DATA.get(key);
    if (!obj) {
      return new Response("not found", { status: 404 });
    }

    // Objects are stored gzipped; decompress here and let Cloudflare re-encode
    // for the client, so the browser always receives plain JSON.
    const headers = new Headers();
    headers.set("content-type", obj.httpMetadata?.contentType || "application/json");
    headers.set("cache-control", obj.httpMetadata?.cacheControl || "public, max-age=300");
    headers.set("etag", obj.httpEtag);
    if (request.method === "HEAD") {
      return new Response(null, { headers });
    }
    const body = obj.httpMetadata?.contentEncoding === "gzip"
      ? obj.body.pipeThrough(new DecompressionStream("gzip"))
      : obj.body;
    return new Response(body, { headers });
  },
};
