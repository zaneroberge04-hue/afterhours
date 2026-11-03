// GET /api/artist?q=name -> artist picture and fan count from Deezer
import {artist} from "../_lib/deezer.js";
import {json, cached} from "../_lib/http.js";

export async function onRequestGet({request}) {
  const q = new URL(request.url).searchParams.get("q") || "";
  if (!q) return json({});
  return json(await cached(`dza:${q.toLowerCase()}`, 14 * 86400, () => artist(q)).catch(() => null) || {});
}
