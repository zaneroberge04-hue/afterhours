"""Afterhours local server.

  python3 server.py            -> http://localhost:5240

Keys (optional) go in config.json next to this file:
  {"edmtrain_key": "...", "ticketmaster_key": "..."}     live shows
  {"audd_token": "..."} or {"acr_host", "acr_key", "acr_secret"}   fingerprinting
or as env vars EDMTRAIN_KEY / TICKETMASTER_KEY.
"""

import hashlib
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).parent
SITE = ROOT / "site"   # the UI and baked feed, shared with the public Cloudflare site
CACHE = ROOT / "cache"
CACHE.mkdir(exist_ok=True)
PORT = int(os.environ.get("PORT", 5240))
UA = "Afterhours/0.1 (personal project)"

sys.path.insert(0, str(ROOT))
import deezer  # noqa: E402
import fingerprint  # noqa: E402
import tracklist  # noqa: E402
import versions  # noqa: E402


def load_config():
    cfg = {}
    p = ROOT / "config.json"
    if p.exists():
        cfg = json.loads(p.read_text())
    cfg.setdefault("edmtrain_key", os.environ.get("EDMTRAIN_KEY", ""))
    cfg.setdefault("ticketmaster_key", os.environ.get("TICKETMASTER_KEY", ""))
    cfg.setdefault("youtube_api_key", os.environ.get("YOUTUBE_API_KEY", ""))
    # Public mode: what a hosted site may do. Official APIs only, nothing downloads audio.
    cfg.setdefault("public_mode", os.environ.get("PUBLIC_MODE", "") == "1")
    return cfg


def get_json(url, timeout=12):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def cached(key, ttl, fn):
    path = CACHE / (hashlib.sha1(key.encode()).hexdigest() + ".json")
    if path.exists() and time.time() - path.stat().st_mtime < ttl:
        return json.loads(path.read_text())
    data = fn()
    path.write_text(json.dumps(data))
    return data


# ---------- tracks ----------

def match_tracks(tracks):
    def one(t):
        key = "dz:" + t.get("artist", "") + "|" + t.get("title", "")
        try:
            return cached(key, 30 * 86400, lambda: deezer.match(t))
        except deezer.DeezerBusy:
            return None   # not cached, so the next look tries again
    with ThreadPoolExecutor(max_workers=4) as pool:
        return list(pool.map(one, tracks))


def artist_lookup(name):
    return cached("dza:" + name.lower(), 14 * 86400, lambda: deezer.artist(name))


# ---------- location + shows ----------

def geocode(q):
    res = get_json("https://geocoding-api.open-meteo.com/v1/search?" +
                   urllib.parse.urlencode({"name": q, "count": 6, "language": "en"}))
    return [{"name": r["name"], "region": r.get("admin1"), "country": r.get("country_code"),
             "lat": r["latitude"], "lon": r["longitude"]} for r in res.get("results") or []]


def events_edmtrain(key, lat, lon, days):
    params = {"latitude": lat, "longitude": lon, "client": key,
              "startDate": date.today().isoformat(),
              "endDate": (date.today() + timedelta(days=days)).isoformat(),
              "livestreamInd": "false"}
    res = get_json("https://edmtrain.com/api/events?" + urllib.parse.urlencode(params))
    if not res.get("success"):
        raise RuntimeError(res.get("message") or "EDMTrain request failed")
    out = []
    for e in res.get("data") or []:
        v = e.get("venue") or {}
        artists = [a["name"] for a in e.get("artistList") or []]
        out.append({
            "id": f"et{e['id']}", "name": e.get("name") or ", ".join(artists[:3]),
            "date": e.get("date"), "time": e.get("startTime"),
            "venue": v.get("name"), "city": v.get("location"), "address": v.get("address"),
            "lat": v.get("latitude"), "lon": v.get("longitude"),
            "artists": artists, "link": e.get("link"), "ages": e.get("ages"),
            "festival": bool(e.get("festivalInd")), "image": None, "source": "EDMTrain",
        })
    return out


def events_ticketmaster(key, lat, lon, radius, days):
    params = {"apikey": key, "latlong": f"{lat},{lon}", "radius": radius, "unit": "miles",
              "classificationName": "Dance/Electronic", "size": 100, "sort": "date,asc",
              "startDateTime": date.today().isoformat() + "T00:00:00Z",
              "endDateTime": (date.today() + timedelta(days=days)).isoformat() + "T23:59:59Z"}
    res = get_json("https://app.ticketmaster.com/discovery/v2/events.json?" + urllib.parse.urlencode(params))
    out = []
    for e in (res.get("_embedded") or {}).get("events") or []:
        v = ((e.get("_embedded") or {}).get("venues") or [{}])[0]
        start = (e.get("dates") or {}).get("start") or {}
        cls = (e.get("classifications") or [{}])[0]
        imgs = sorted(e.get("images") or [], key=lambda i: i.get("width", 0))
        out.append({
            "id": f"tm{e['id']}", "name": e.get("name"),
            "date": start.get("localDate"), "time": start.get("localTime"),
            "venue": v.get("name"), "city": (v.get("city") or {}).get("name"),
            "address": (v.get("address") or {}).get("line1"),
            "lat": float((v.get("location") or {}).get("latitude") or 0) or None,
            "lon": float((v.get("location") or {}).get("longitude") or 0) or None,
            "artists": [a["name"] for a in (e.get("_embedded") or {}).get("attractions") or []],
            "link": e.get("url"), "genre": (cls.get("subGenre") or {}).get("name"),
            "festival": False, "image": imgs[len(imgs) // 2]["url"] if imgs else None,
            "source": "Ticketmaster",
        })
    return out


def events(lat, lon, radius, days):
    cfg = load_config()
    out, sources, errors = [], [], []
    if cfg["edmtrain_key"]:
        try:
            out += events_edmtrain(cfg["edmtrain_key"], lat, lon, days)
            sources.append("EDMTrain")
        except Exception as e:
            errors.append(f"EDMTrain: {e}")
    if cfg["ticketmaster_key"]:
        try:
            seen = {(e["date"], (e["venue"] or "").lower()) for e in out}
            for e in events_ticketmaster(cfg["ticketmaster_key"], lat, lon, radius, days):
                if (e["date"], (e["venue"] or "").lower()) not in seen:
                    out.append(e)
            sources.append("Ticketmaster")
        except Exception as e:
            errors.append(f"Ticketmaster: {e}")
    out.sort(key=lambda e: (e["date"] or "", e["time"] or ""))
    return {"events": out, "sources": sources, "errors": errors,
            "needs_key": not (cfg["edmtrain_key"] or cfg["ticketmaster_key"])}


# ---------- http ----------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("[afterhours] " + fmt % args + "\n")

    def send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        try:
            if u.path in ("/", "/index.html"):
                return self.send(200, (SITE / "index.html").read_bytes(), "text/html; charset=utf-8")
            if u.path.startswith("/data/"):   # the baked feed, same files the public site serves
                f = (SITE / u.path.lstrip("/")).resolve()
                if SITE.resolve() in f.parents and f.is_file():
                    return self.send(200, f.read_bytes())
                return self.send(404, {"error": "not found"})
            if u.path == "/api/status":
                cfg = load_config()
                public = cfg["public_mode"]
                return self.send(200, {"edmtrain": bool(cfg["edmtrain_key"]),
                                       "ticketmaster": bool(cfg["ticketmaster_key"]),
                                       "youtube_api": bool(cfg["youtube_api_key"]),
                                       "public_mode": public,
                                       "audio": not public,   # version finder + fingerprint scan
                                       "fingerprint": None if public else fingerprint.provider(cfg)[0]})
            if u.path == "/api/geocode":
                return self.send(200, cached("geo:" + q["q"].lower(), 30 * 86400, lambda: geocode(q["q"])))
            if u.path == "/api/events":
                lat, lon = round(float(q["lat"]), 3), round(float(q["lon"]), 3)
                radius, days = int(q.get("radius", 60)), int(q.get("days", 60))
                key = f"ev:{lat},{lon},{radius},{days},{date.today()}"
                return self.send(200, cached(key, 3 * 3600, lambda: events(lat, lon, radius, days)))
            if u.path == "/api/fingerprint/job":
                job = fingerprint.JOBS.get(q.get("id", ""))
                if not job:
                    return self.send(404, {"error": "Unknown job"})
                return self.send(200, {k: v for k, v in job.items() if k != "url"})
            if u.path == "/api/versions/job":
                job = versions.JOBS.get(q.get("id", ""))
                if not job:
                    return self.send(404, {"error": "Unknown job"})
                return self.send(200, {k: v for k, v in job.items() if k != "url"})
            if u.path == "/api/artist":
                return self.send(200, artist_lookup(q["q"]) or {})
            self.send(404, {"error": "not found"})
        except Exception as e:
            self.send(500, {"error": str(e)})

    def do_POST(self):
        try:
            data = self.body()
            cfg = load_config()
            if cfg["public_mode"] and self.path.startswith(("/api/fingerprint/", "/api/versions/")):
                return self.send(403, {"error": "Audio features only run in the local app"})
            if self.path == "/api/set":
                url = (data.get("url") or "").strip()
                if not url.startswith("http"):
                    return self.send(400, {"error": "Paste a full link to the set."})
                # keep each mode's results apart, so public mode never serves what yt-dlp fetched locally
                via = ("api" if cfg["youtube_api_key"] and tracklist.youtube_id(url)
                       else "public" if cfg["public_mode"] else "local")
                return self.send(200, cached(f"set:{via}:{url}", 7 * 86400, lambda: tracklist.extract(url, cfg=cfg)))
            if self.path == "/api/answers":
                url = data.get("url") or ""
                tracks = data.get("tracks") or []
                key = f"ans:{'public' if cfg['public_mode'] else 'local'}:{bool(cfg['youtube_api_key'])}:{url}:" + hashlib.sha1(json.dumps([(t.get('seconds'), t.get('is_id')) for t in tracks]).encode()).hexdigest()
                return self.send(200, cached(key, 86400, lambda: tracklist.answers_for(url, tracks, data.get("duration"), cfg)))
            if self.path in ("/api/fingerprint/estimate", "/api/fingerprint/start"):
                args = dict(duration=data.get("duration"), cfg=cfg, mode=data.get("mode", "scan"),
                            interval=data.get("interval", 60), tracks=data.get("tracks"))
                if self.path.endswith("estimate"):
                    return self.send(200, fingerprint.estimate(**args))
                job = fingerprint.start(data["url"], data["set_id"], **args)
                return self.send(200, {"id": job["id"]})
            if self.path == "/api/versions/start":
                job = versions.start(data["url"], data["set_id"], data.get("artist", ""), data["title"],
                                     data.get("seconds"), data.get("length"), data.get("mashup"))
                return self.send(200, {"id": job["id"]})
            if self.path == "/api/fingerprint/cancel":
                job = fingerprint.JOBS.get(data.get("id", ""))
                if job:
                    job["cancel"] = True
                return self.send(200, {"ok": bool(job)})
            if self.path == "/api/match":
                return self.send(200, {"matches": match_tracks(data.get("tracks") or [])})
            self.send(404, {"error": "not found"})
        except Exception as e:
            self.send(500, {"error": str(e)})


if __name__ == "__main__":
    print(f"Afterhours on http://localhost:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
