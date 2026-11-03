// Small helpers shared by the Pages Functions: JSON replies and an edge cache.
export const json = (body, status = 200, extra = {}) =>
  new Response(JSON.stringify(body), {status, headers: {"Content-Type": "application/json", ...extra}});

// Cloudflare's edge cache, keyed by a made-up URL. Every set is fetched from YouTube
// at most once per TTL, which is what keeps the free API quota safe.
// fn throwing means "try again later": nothing is stored.
export async function cached(key, ttl, fn) {
  const cache = typeof caches !== "undefined" ? caches.default : null;
  const req = new Request("https://cache.afterhours.internal/" + encodeURIComponent(key));
  if (cache) {
    const hit = await cache.match(req);
    if (hit) return hit.json();
  }
  const data = await fn();
  if (cache) await cache.put(req, json(data, 200, {"Cache-Control": `max-age=${ttl}`}));
  return data;
}
