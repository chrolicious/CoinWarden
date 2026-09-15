const ALLOWED_PREFIXES = ["latest/", "history/", "items.json"];

export default {
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
