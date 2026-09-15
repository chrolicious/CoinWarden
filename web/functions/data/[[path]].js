const ALLOWED_PREFIXES = ["latest/", "history/", "items.json"];

export async function onRequestGet({ params, env }) {
  const key = (params.path || []).join("/");
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
  return new Response(obj.body, { headers });
}
