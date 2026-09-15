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

    const headers = new Headers();
    obj.writeHttpMetadata(headers);
    headers.set("etag", obj.httpEtag);
    if (!headers.has("cache-control")) {
      headers.set("cache-control", "public, max-age=300");
    }
    return new Response(request.method === "HEAD" ? null : obj.body, { headers });
  },
};
