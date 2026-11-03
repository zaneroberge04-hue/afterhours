"""Build the public feed: recent sets from followed channels, fully tracklisted.

Runs on a schedule in GitHub Actions (.github/workflows/feed.yml) and writes static
files the site serves as-is:
  site/data/feed.json        newest sets, one summary line each
  site/data/charts.json      most-played tracks, most-supported artists, open IDs
  site/data/sets/<id>.json   one set: tracklist, Deezer matches, when it was last checked

New sets get parsed once. Sets under RECHECK_DAYS old that still have unnamed IDs, or no
tracklist yet, get re-read every RECHECK_HOURS, because tracklists and ID answers show up
in the comments after posting. Uses only the YouTube Data API and Deezer: no audio.

  YOUTUBE_API_KEY=... python build_feed.py
"""

import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
import deezer  # noqa: E402
import tracklist  # noqa: E402
import youtube_api  # noqa: E402

DATA = ROOT / "site" / "data"
SETS = DATA / "sets"
MIN_LEN, MAX_LEN = 25 * 60, 6 * 3600     # a set, not a clip and not a 12-hour stream
NEW_WITHIN_DAYS = int(os.environ.get("NEW_WITHIN_DAYS", 45))   # raise once to backfill
RECHECK_DAYS, RECHECK_HOURS = 21, 12
PER_CHANNEL = int(os.environ.get("PER_CHANNEL", 15))
KEEP = 200
CHART_DAYS = 30


def now():
    return datetime.now(timezone.utc)


def norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def lead(artist):
    return re.split(r",|&|\bfeat\.?|\bft\.?|\bvs\.?|\bx\b", artist or "", flags=re.I)[0].strip()


def recent_uploads(yt, channels):
    """Newest uploads per channel: 1 unit per channel, plus 1 per 50 videos for durations."""
    ids = {}
    for ch in channels:
        try:
            res = yt.get("playlistItems", part="contentDetails", playlistId="UU" + ch["id"][2:], maxResults=PER_CHANNEL)
        except RuntimeError as e:
            print(f"  skip {ch['name']}: {e}")
            continue
        for it in res.get("items") or []:
            ids[it["contentDetails"]["videoId"]] = ch["name"]
    out = []
    vids = list(ids)
    for i in range(0, len(vids), 50):
        res = yt.get("videos", part="snippet,contentDetails", id=",".join(vids[i:i + 50]))
        for v in res.get("items") or []:
            dur = youtube_api.iso_duration(v["contentDetails"].get("duration"))
            pub = datetime.fromisoformat(v["snippet"]["publishedAt"].replace("Z", "+00:00"))
            if dur and MIN_LEN <= dur <= MAX_LEN and now() - pub <= timedelta(days=NEW_WITHIN_DAYS) \
                    and v["snippet"].get("liveBroadcastContent") == "none":
                out.append({"id": v["id"], "channel": ids[v["id"]], "published": v["snippet"]["publishedAt"]})
    return out


def process(cfg, vid, channel, published):
    url = f"https://www.youtube.com/watch?v={vid}"
    tracklist._COMMENTS.pop(url, None)
    r = tracklist.extract(url, cfg=cfg)
    filled = 0
    if any(t["is_id"] and t["seconds"] is not None for t in r["tracks"]):
        ans = tracklist.answers_for(url, r["tracks"], r["set"]["duration"], cfg)
        for i, a in ans["fills"].items():
            r["tracks"][i].update(artist=a["artist"], title=a["title"], is_id=False, source="community",
                                  confidence="community",
                                  evidence=("Posted by the artist. " if a["by_artist"] else "") + a["evidence"])
            filled += 1
        r["identified"] = sum(1 for t in r["tracks"] if not t["is_id"])
    with ThreadPoolExecutor(max_workers=3) as pool:   # stays under Deezer's rate limit
        r["matches"] = list(pool.map(_safe_match, r["tracks"]))
    r["matched_at"] = now().isoformat(timespec="seconds")
    r["channel"], r["published"] = channel, published
    r["checked_at"] = now().isoformat(timespec="seconds")
    r["answered"] = {"filled": filled}
    return r


def _safe_match(t):
    try:
        return deezer.match(t)
    except Exception:
        return None


def rematch(s):
    """Retry Deezer for named tracks with no match, at most daily. Costs no YouTube quota.
    Catches tracks that were rate limited, and songs that came out since the set aired."""
    last = s.get("matched_at")
    if last and now() - datetime.fromisoformat(last) < timedelta(hours=24):
        return 0
    todo = [i for i, (t, m) in enumerate(zip(s["tracks"], s.get("matches") or [None] * len(s["tracks"])))
            if not m and not t["is_id"]]
    s.setdefault("matches", [None] * len(s["tracks"]))
    with ThreadPoolExecutor(max_workers=3) as pool:
        for i, m in zip(todo, pool.map(lambda i: _safe_match(s["tracks"][i]), todo)):
            s["matches"][i] = m
    s["matched_at"] = now().isoformat(timespec="seconds")
    return sum(1 for i in todo if s["matches"][i])


def needs_recheck(s):
    if not s["tracks"] or any(t["is_id"] for t in s["tracks"]):
        age = now() - datetime.fromisoformat(s["published"].replace("Z", "+00:00"))
        last = now() - datetime.fromisoformat(s["checked_at"])
        return age <= timedelta(days=RECHECK_DAYS) and last >= timedelta(hours=RECHECK_HOURS)
    return False


def summary(s):
    return {"id": s["set"]["id"], "title": s["set"]["title"], "channel": s["channel"], "published": s["published"],
            "thumbnail": s["set"]["thumbnail"], "duration_ts": s["set"]["duration_ts"], "tracks": len(s["tracks"]),
            "ids": sum(1 for t in s["tracks"] if t["is_id"]), "source": s["source"],
            "covers": [m["cover"] for m in s.get("matches") or [] if m and m.get("cover")][:4]}


def charts(sets):
    cutoff = now() - timedelta(days=CHART_DAYS)
    recent = [s for s in sets if datetime.fromisoformat(s["published"].replace("Z", "+00:00")) >= cutoff]
    plays, artists, open_ids = defaultdict(list), Counter(), []
    for s in recent:
        seen, credited = set(), set()
        dj = norm(re.split(r"\s+[|@-]\s+|\(", s["set"]["title"] or "")[0])   # "MPH | Mixmag Lab" -> mph
        for i, t in enumerate(s["tracks"]):
            if t["is_id"]:
                open_ids.append({"set": s["set"]["id"], "set_title": s["set"]["title"], "time": t["time"],
                                 "seconds": t["seconds"]})
                continue
            key = norm(lead(t["artist"])) + "|" + norm(re.split(r"[(\[]", t["title"])[0])
            if key in seen or not t["artist"]:
                continue
            seen.add(key)
            m = (s.get("matches") or [None] * len(s["tracks"]))[i]
            plays[key].append({"artist": t["artist"], "title": t["title"], "set": s["set"]["id"],
                               "set_title": s["set"]["title"], "cover": (m or {}).get("cover"),
                               "preview": (m or {}).get("preview")})
            # Support from other DJs: once per set, and not the DJ playing their own records
            for a in re.split(r"\s*(?:,|&|\bfeat\.?|\bft\.?|\bx\b)\s*", t["artist"], flags=re.I):
                k = norm(a)
                if len(k) > 1 and k not in credited and not (dj and (k in dj or dj in k)):
                    credited.add(k)
                    artists[a.strip()] += 1
    top = sorted(plays.values(), key=lambda v: -len(v))[:40]
    return {"built": now().isoformat(timespec="seconds"), "days": CHART_DAYS, "sets": len(recent),
            "tracks": [{"artist": v[0]["artist"], "title": v[0]["title"], "cover": next((x["cover"] for x in v if x["cover"]), None),
                        "preview": next((x["preview"] for x in v if x["preview"]), None), "plays": len(v),
                        "sets": [{"id": x["set"], "title": x["set_title"]} for x in v[:6]]} for v in top if len(v) >= 2],
            "artists": [{"name": n, "plays": c} for n, c in artists.most_common(30)],
            "open_ids": open_ids[:60]}


def main():
    key = os.environ.get("YOUTUBE_API_KEY") or json.loads((ROOT / "config.json").read_text()).get("youtube_api_key")
    if not key:
        sys.exit("Set YOUTUBE_API_KEY")
    cfg = {"youtube_api_key": key, "public_mode": True}
    yt = youtube_api.Source(key)
    calls = {"n": 0}
    real_get = youtube_api.Source.get

    def counted(self, endpoint, **p):   # quota units for the run log
        calls["n"] += 1
        return real_get(self, endpoint, **p)
    youtube_api.Source.get = counted

    SETS.mkdir(parents=True, exist_ok=True)
    channels = json.loads((ROOT / "channels.json").read_text())["channels"]
    stored = {p.stem: json.loads(p.read_text()) for p in SETS.glob("*.json")}
    t0 = time.time()
    uploads = recent_uploads(yt, channels)
    new = [u for u in uploads if u["id"] not in stored]
    redo = [s for s in stored.values() if needs_recheck(s)]
    print(f"{len(uploads)} recent sets, {len(new)} new, {len(redo)} to re-check")

    for u in new:
        try:
            stored[u["id"]] = process(cfg, u["id"], u["channel"], u["published"])
            s = stored[u["id"]]
            print(f"  new  {s['set']['title'][:60]}: {len(s['tracks'])} tracks ({s['source']})")
        except Exception as e:
            print(f"  fail {u['id']}: {e}")
    for s in redo:
        vid = s["set"]["id"]
        try:
            before = (len(s["tracks"]), s["identified"])
            stored[vid] = process(cfg, vid, s["channel"], s["published"])
            after = (len(stored[vid]["tracks"]), stored[vid]["identified"])
            print(f"  redo {s['set']['title'][:60]}: {before} -> {after}")
        except Exception as e:
            print(f"  fail recheck {vid}: {e}")

    keep = sorted(stored.values(), key=lambda s: s["published"], reverse=True)[:KEEP]
    found = sum(rematch(s) for s in keep)
    if found:
        print(f"  matched {found} more tracks on Deezer")
    keep_ids = {s["set"]["id"] for s in keep}
    for vid, s in stored.items():
        path = SETS / f"{vid}.json"
        if vid in keep_ids:
            path.write_text(json.dumps(s, separators=(",", ":")))
        elif path.exists():
            path.unlink()   # aged out of the feed
    (DATA / "feed.json").write_text(json.dumps({"built": now().isoformat(timespec="seconds"),
                                                "channels": [c["name"] for c in channels],
                                                "sets": [summary(s) for s in keep]}, separators=(",", ":")))
    (DATA / "charts.json").write_text(json.dumps(charts(keep), separators=(",", ":")))
    print(f"done: {len(keep)} sets in feed, {calls['n']} API units, {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
