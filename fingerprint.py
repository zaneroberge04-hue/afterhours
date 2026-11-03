"""Audio fingerprinting for sets nobody has written a tracklist for.

Downloads the set's audio once with yt-dlp, cuts short samples with ffmpeg, sends
each to a recognition provider, and merges back-to-back hits into tracks.

Providers (set one in config.json):
  AudD:     {"audd_token": "..."}                       https://dashboard.audd.io
  ACRCloud: {"acr_host": "identify-us-west-2.acrcloud.com",
             "acr_key": "...", "acr_secret": "..."}      https://console.acrcloud.com
  Mock:     {"fingerprint_mock": true}  fake hits, for testing the pipeline only

Modes:
  scan  sample the whole set every `interval` seconds
  fill  sample only the ID slots of an existing tracklist
"""

import base64
import hashlib
import hmac
import json
import subprocess
import tempfile
import threading
import time
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import imageio_ffmpeg

import tracklist

ROOT = Path(__file__).parent
AUDIO = ROOT / "cache" / "audio"
AUDIO.mkdir(parents=True, exist_ok=True)
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
SAMPLE_LEN = 12          # seconds per lookup, plenty for both providers
SKIP_EDGES = 20          # ignore the first/last seconds (talking, silence)
WORKERS = 4

JOBS = {}
JOBS_LOCK = threading.Lock()


# ---------- providers ----------

def multipart(fields, file_field, file_bytes, filename="sample.mp3"):
    boundary = uuid.uuid4().hex
    out = b""
    for k, v in fields.items():
        out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n").encode()
    out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; "
            f"filename=\"{filename}\"\r\nContent-Type: audio/mpeg\r\n\r\n").encode()
    out += file_bytes + f"\r\n--{boundary}--\r\n".encode()
    return out, f"multipart/form-data; boundary={boundary}"


def post(url, body, ctype, timeout=30):
    req = urllib.request.Request(url, data=body, headers={"Content-Type": ctype, "User-Agent": "Afterhours/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


class ProviderError(Exception):
    """Account-level failure (bad key, out of credits). Stops the job."""


def recognize_audd(cfg, audio):
    body, ctype = multipart({"api_token": cfg["audd_token"], "return": "spotify,deezer"}, "file", audio)
    res = post("https://api.audd.io/", body, ctype)
    if res.get("status") == "error":
        err = res.get("error") or {}
        raise ProviderError(f"AudD {err.get('error_code')}: {err.get('error_message')}")
    r = res.get("result")
    if not r:
        return None
    return {"artist": r.get("artist", ""), "title": r.get("title", ""), "album": r.get("album"),
            "label": r.get("label"), "link": r.get("song_link"), "score": None}


def recognize_acr(cfg, audio):
    ts = str(int(time.time()))
    to_sign = "\n".join(["POST", "/v1/identify", cfg["acr_key"], "audio", "1", ts])
    sig = base64.b64encode(hmac.new(cfg["acr_secret"].encode(), to_sign.encode(), hashlib.sha1).digest()).decode()
    fields = {"access_key": cfg["acr_key"], "sample_bytes": len(audio), "timestamp": ts,
              "signature": sig, "data_type": "audio", "signature_version": "1"}
    body, ctype = multipart(fields, "sample", audio)
    res = post(f"https://{cfg['acr_host']}/v1/identify", body, ctype)
    code = (res.get("status") or {}).get("code")
    if code == 1001:  # no result
        return None
    if code != 0:
        raise ProviderError(f"ACRCloud {code}: {(res.get('status') or {}).get('msg')}")
    music = ((res.get("metadata") or {}).get("music") or [None])[0]
    if not music:
        return None
    return {"artist": ", ".join(a["name"] for a in music.get("artists") or []),
            "title": music.get("title", ""), "album": (music.get("album") or {}).get("name"),
            "label": music.get("label"), "link": None, "score": music.get("score")}


MOCK_TRACKS = [("Mock Artist A", "First Tune"), ("Mock Artist B", "Second Tune (Extended Mix)"),
               ("Mock Artist C", "Third Tune")]


def recognize_mock(cfg, audio, at=0):
    time.sleep(0.2)
    slot = int(at // 180)
    if slot % 4 == 3:
        return None  # simulate an unreleased ID
    a, t = MOCK_TRACKS[slot % len(MOCK_TRACKS)]
    return {"artist": a, "title": f"{t} #{slot}", "album": None, "label": None, "link": None, "score": None}


def provider(cfg):
    if cfg.get("acr_host") and cfg.get("acr_key") and cfg.get("acr_secret"):
        return "ACRCloud", lambda audio, at: recognize_acr(cfg, audio)
    if cfg.get("audd_token"):
        return "AudD", lambda audio, at: recognize_audd(cfg, audio)
    if cfg.get("fingerprint_mock"):
        return "Mock", lambda audio, at: recognize_mock(cfg, audio, at)
    return None, None


# ---------- audio ----------

KEEP_AUDIO = 30 * 60   # a set's audio stays while you work through it, gone 30 idle minutes later


def prune_audio():
    for f in AUDIO.iterdir():
        if time.time() - f.stat().st_mtime > KEEP_AUDIO:
            f.unlink(missing_ok=True)


def _janitor():
    while True:
        time.sleep(300)
        try:
            prune_audio()
        except OSError:
            pass


threading.Thread(target=_janitor, daemon=True).start()


def download_audio(url, set_id, progress):
    prune_audio()
    hits = [p for p in AUDIO.glob(f"{set_id}.*") if not p.name.endswith(".part")]
    if hits:
        hits[0].touch()   # still in use, restart the idle clock
        return hits[0]
    proc = subprocess.Popen(
        [tracklist.YTDLP, "-f", "bestaudio[abr<=96]/bestaudio/best", "--no-playlist", "--newline", "--no-warnings",
         "-o", str(AUDIO / f"{set_id}.%(ext)s"), url],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for line in proc.stdout:
        if "[download]" in line and "%" in line:
            try:
                progress(float(line.split("%")[0].split()[-1]))
            except ValueError:
                pass
    proc.wait()
    hits = [p for p in AUDIO.glob(f"{set_id}.*") if not p.name.endswith(".part")]
    if proc.returncode != 0 or not hits:
        raise RuntimeError("Could not download the set's audio")
    return hits[0]


def cut_sample(path, at):
    with tempfile.NamedTemporaryFile(suffix=".mp3") as tmp:
        subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-ss", str(at), "-t", str(SAMPLE_LEN),
                        "-i", str(path), "-ac", "1", "-ar", "22050", "-b:a", "64k", tmp.name],
                       check=True, timeout=60)
        return Path(tmp.name).read_bytes()


# ---------- planning + merging ----------

def scan_points(duration, interval):
    start, end = SKIP_EDGES, max(SKIP_EDGES, duration - SKIP_EDGES - SAMPLE_LEN)
    pts, t = [], start
    while t <= end:
        pts.append(int(t))
        t += interval
    return pts


def fill_points(tracks, duration):
    """Two samples inside each ID slot, a third into the slot and two thirds in."""
    pts = []
    for i, t in enumerate(tracks):
        if not t.get("is_id") or t.get("seconds") is None:
            continue
        start = t["seconds"]
        end = tracks[i + 1]["seconds"] if i + 1 < len(tracks) and tracks[i + 1].get("seconds") else duration
        span = max(SAMPLE_LEN, (end or start + 180) - start)
        for frac in (0.33, 0.66):
            pts.append({"at": int(start + span * frac), "slot": i})
    return pts


def key_of(hit):
    return (tracklist.clean(hit["artist"]).lower(), tracklist.clean(hit["title"]).split("(")[0].strip().lower())


def merge_scan(samples, duration):
    """samples: sorted [(at, hit_or_None)]. Collapse runs of the same song into one track."""
    tracks = []
    for at, hit in samples:
        if hit and tracks and not tracks[-1]["is_id"] and key_of(hit) == tracks[-1]["_key"]:
            tracks[-1]["hits"] += 1
            continue
        if not hit and tracks and tracks[-1]["is_id"]:
            continue  # one ID block per gap
        tracks.append({
            "seconds": at, "time": tracklist.sec_to_ts(at),
            "artist": hit["artist"] if hit else "", "title": hit["title"] if hit else "ID",
            "album": hit.get("album") if hit else None, "label": hit.get("label") if hit else None,
            "is_id": not hit, "source": "fingerprint", "hits": 1 if hit else 0,
            "_key": key_of(hit) if hit else None,
        })
    # A song heard once between two IDs may be a misfire, keep it but flag it
    for t in tracks:
        t["confidence"] = "low" if t["is_id"] else "high" if t["hits"] >= 2 else "medium"
        t.pop("_key")
    for i, t in enumerate(tracks):
        nxt = tracks[i + 1]["seconds"] if i + 1 < len(tracks) else duration
        t["length"] = max(0, int((nxt or t["seconds"]) - t["seconds"]))
    return tracks


# ---------- jobs ----------

def run_job(job, cfg):
    name, recognize = provider(cfg)
    try:
        job["provider"] = name
        job["phase"] = "Downloading audio"
        path = download_audio(job["url"], job["set_id"], lambda p: job.update(download=p))
        job["download"] = 100.0

        if job["mode"] == "fill":
            plan = fill_points(job["tracks"], job["duration"])
        else:
            plan = [{"at": a, "slot": None} for a in scan_points(job["duration"], job["interval"])]
        job["total"] = len(plan)
        job["phase"] = f"Listening with {name}"

        results = {}

        def one(p):
            if job["cancel"]:
                return
            try:
                hit = recognize(cut_sample(path, p["at"]), p["at"])
            except ProviderError:
                job["cancel"] = True  # bad key or no credits, stop the other workers too
                raise
            except Exception as e:
                hit, job["warnings"] = None, (job["warnings"] + [f"{tracklist.sec_to_ts(p['at'])}: {e}"])[-5:]
            results[p["at"]] = (p, hit)
            job["done"] += 1
            if hit:
                job["hits"] += 1
            if job["mode"] == "scan":
                job["tracks"] = merge_scan(sorted((a, h) for a, (_, h) in results.items()), job["duration"])

        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            for f in [pool.submit(one, p) for p in plan]:
                f.result()

        if job["mode"] == "fill":
            filled = {}
            for at, (p, hit) in sorted(results.items()):
                if hit and p["slot"] not in filled:
                    filled[p["slot"]] = {**hit, "at": at}
            job["filled"] = filled
        job["phase"] = "Cancelled" if job["cancel"] else "Done"
        job["status"] = "cancelled" if job["cancel"] else "done"
    except Exception as e:
        job["status"], job["error"] = "error", str(e)
    finally:
        job["finished"] = time.time()


def start(url, set_id, duration, cfg, mode="scan", interval=60, tracks=None):
    name, _ = provider(cfg)
    if not name:
        raise ValueError("No fingerprint provider configured")
    if not duration:
        raise ValueError("This set has no known length, cannot plan samples")
    job = {"id": uuid.uuid4().hex[:10], "url": url, "set_id": set_id, "duration": duration,
           "mode": mode, "interval": max(30, int(interval)), "tracks": tracks or [], "status": "running",
           "phase": "Starting", "download": 0.0, "done": 0, "total": 0, "hits": 0,
           "warnings": [], "error": None, "cancel": False, "filled": None, "provider": name,
           "started": time.time(), "finished": None}
    with JOBS_LOCK:
        JOBS[job["id"]] = job
    threading.Thread(target=run_job, args=(job, cfg), daemon=True).start()
    return job


def estimate(duration, interval, cfg, mode="scan", tracks=None):
    name, _ = provider(cfg)
    n = len(fill_points(tracks or [], duration)) if mode == "fill" else len(scan_points(duration or 0, interval))
    return {"provider": name, "lookups": n}
