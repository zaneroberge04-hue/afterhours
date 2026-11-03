// Entry point when Afterhours is deployed as a Cloudflare Worker (static assets + this
// script) rather than a Pages project. It routes /api/* to the same handlers Pages
// Functions would use, and everything else to the files in site/.
import * as set from "./functions/api/set.js";
import * as match from "./functions/api/match.js";
import * as artist from "./functions/api/artist.js";
import * as status from "./functions/api/status.js";

const routes = {"/api/set": set, "/api/match": match, "/api/artist": artist, "/api/status": status};

export default {
  async fetch(request, env, ctx) {
    const {pathname} = new URL(request.url);
    const mod = routes[pathname];
    if (!mod) return env.ASSETS.fetch(request);
    const handler = request.method === "POST" ? mod.onRequestPost : request.method === "GET" ? mod.onRequestGet : null;
    if (!handler) return new Response(JSON.stringify({error: "Method not allowed"}), {status: 405});
    return handler({request, env, waitUntil: p => ctx.waitUntil(p)});
  },
};
