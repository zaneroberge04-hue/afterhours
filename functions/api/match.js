// POST /api/match {tracks} -> Deezer match per track (Deezer has no CORS, so the browser can't ask it directly)
import {match} from "../_lib/deezer.js";
import {json, cached} from "../_lib/http.js";

export async function onRequestPost({request}) {
  const {tracks = []} = await request.json().catch(() => ({}));
  const list = tracks.slice(0, 80), matches = new Array(list.length).fill(null);
  // Four at a time keeps a 40-track set under Deezer's rate limit
  let next = 0;
  const worker = async () => {
    while (next < list.length) {
      const i = next++, t = list[i];
      matches[i] = await cached(`dz:${t.artist || ""}|${t.title || ""}`, 30 * 86400, () => match(t)).catch(() => null);
    }
  };
  await Promise.all(Array.from({length: 4}, worker));
  return json({matches});
}
