"""Deezer lookups: cover art, 30 s previews, artist pictures. No key needed.

Shared by the local server and the feed builder; the Pages Functions have a JS twin.
"""

import json
import urllib.parse
import urllib.request

UA = "Afterhours/0.1 (personal project)"


class DeezerBusy(Exception):
    """Rate limited or down. Callers should not cache this as 'no match'."""


def get_json(url, timeout=12):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            j = json.loads(r.read().decode())
    except Exception as e:
        raise DeezerBusy(str(e))
    if isinstance(j, dict) and j.get("error"):   # Deezer reports rate limits as a 200 with an error body
        raise DeezerBusy(str(j["error"]))
    return j


def match(track):
    artist, title = track.get("artist", ""), track.get("title", "")
    if not title or track.get("is_id"):
        return None
    lead_artist = artist.split(",")[0].split("&")[0].split(" feat")[0].strip()
    base_title = title.split("(")[0].strip()
    queries = [
        f'artist:"{lead_artist}" track:"{base_title}"',
        f"{lead_artist} {base_title}",
    ]
    for q in queries:
        res = get_json("https://api.deezer.com/search?" + urllib.parse.urlencode({"q": q, "limit": 5}))
        for hit in res.get("data") or []:
            hit_artist = hit.get("artist", {}).get("name", "").lower()
            if lead_artist and lead_artist.lower()[:5] not in hit_artist and hit_artist[:5] not in artist.lower():
                continue
            return {
                "deezer_id": hit.get("id"),
                "preview": hit.get("preview"),
                "cover": hit.get("album", {}).get("cover_medium"),
                "album": hit.get("album", {}).get("title"),
                "link": hit.get("link"),
                "matched_title": hit.get("title"),
                "matched_artist": hit.get("artist", {}).get("name"),
                "artist_picture": hit.get("artist", {}).get("picture_medium"),
            }
    return None


def artist(name):
    res = get_json("https://api.deezer.com/search/artist?" + urllib.parse.urlencode({"q": name, "limit": 1}))
    hit = (res.get("data") or [None])[0]
    if not hit:
        return None
    return {"name": hit["name"], "picture": hit.get("picture_medium"), "fans": hit.get("nb_fan"),
            "deezer_id": hit.get("id"), "link": hit.get("link")}
