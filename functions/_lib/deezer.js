// JavaScript twin of deezer.py: cover art, previews, artist pictures. No key needed.
// Deezer allows about 50 requests per 5 seconds and answers a busy moment with an
// error object, not a 4xx. Throw on that, so the caller never caches a miss that was
// really a rate limit.
async function get(url) {
  const j = await (await fetch(url, {headers: {"User-Agent": "Afterhours/0.1"}})).json();
  if (j && j.error) throw new Error(`Deezer: ${j.error.message || j.error.type}`);
  return j;
}

export async function match(track) {
  const artist = track.artist || "", title = track.title || "";
  if (!title || track.is_id) return null;
  const lead = artist.split(",")[0].split("&")[0].split(" feat")[0].trim();
  const base = title.split("(")[0].trim();
  for (const q of [`artist:"${lead}" track:"${base}"`, `${lead} ${base}`]) {
    const res = await get("https://api.deezer.com/search?" + new URLSearchParams({q, limit: 5}));
    for (const hit of res.data || []) {
      const ha = ((hit.artist || {}).name || "").toLowerCase();
      if (lead && !ha.includes(lead.toLowerCase().slice(0, 5)) && !artist.toLowerCase().includes(ha.slice(0, 5))) continue;
      return {deezer_id: hit.id, preview: hit.preview, cover: (hit.album || {}).cover_medium, album: (hit.album || {}).title,
        link: hit.link, matched_title: hit.title, matched_artist: (hit.artist || {}).name,
        artist_picture: (hit.artist || {}).picture_medium};
    }
  }
  return null;
}

export async function artist(name) {
  const res = await get("https://api.deezer.com/search/artist?" + new URLSearchParams({q: name, limit: 1}));
  const hit = (res.data || [])[0];
  return hit ? {name: hit.name, picture: hit.picture_medium, fans: hit.nb_fan, deezer_id: hit.id, link: hit.link} : null;
}
