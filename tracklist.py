"""Tracklist extraction for DJ sets.

Pulls set metadata with yt-dlp (YouTube, SoundCloud, Mixcloud, and anything else
yt-dlp supports), then finds the tracklist in, in order of reliability:
  1. chapters
  2. timestamped lines in the description
  3. untimed "Artist - Title" lines under a "Tracklist" heading
  4. top comments (YouTube only, slower, used when 1-3 find too little)

Every track comes back as {artist, title, time, source, confidence}.
"""

import json
import time
from difflib import SequenceMatcher
import re
import subprocess
import sys
from pathlib import Path

YTDLP = str(Path(__file__).parent / ".venv" / "bin" / "yt-dlp")

TS = r"(?:\d{1,2}:)?\d{1,2}:\d{2}"
TS_LINE = re.compile(
    # "0:00 - 05:20 Artist - Title" ranges: keep the start, drop the end time
    rf"^\s*(?:\d{{1,3}}[.)]\s*)?[\[(]?(?P<ts>{TS})(?:\s*[-–—~]\s*{TS})?[\])]?\s*[-–—|:.]?\s*(?P<rest>.+?)\s*$"
)
# "1. Artist - Title [Label] 00:00": radio shows put the time at the end
TS_TRAIL = re.compile(rf"^\s*(?:\d{{1,3}}[.)]\s*)?(?P<rest>.+?)\s*[\[(]?(?P<ts>{TS})[\])]?\s*$")
NUMBERED_LINE = re.compile(r"^\s*(?:\d{1,3}[.)]|[-•*])\s*(?P<rest>.+?)\s*$")
SEP = re.compile(r"\s+[-–—]\s+")
TRACKLIST_HEADING = re.compile(r"track\s*list|tracks?:|setlist|track\s*listing", re.I)
URL = re.compile(r"https?://\S+")
TL1001 = re.compile(r"https?://(?:www\.)?(?:1001tracklists\.com|1001\.tl)/\S+", re.I)

# A side that looks like a title rather than an artist
TITLE_HINTS = re.compile(r"\((?:[^)]*\b(?:remix|mix|edit|dub|version|rework|vip|bootleg|flip)\b)", re.I)
ARTIST_HINTS = re.compile(r",|&|\bfeat\.?|\bft\.?|\bvs\.?|\bx\b|\band\b", re.I)
UNKNOWN = re.compile(r"^\s*(?:id|unknown(?: artist)?|\?+)\s*$", re.I)
# Bracketed notes that are not part of the title: [unreleased], [Soundcloud], [Free DL], [Label]
BRACKET = re.compile(r"\s*\[([^\]]{1,40})\]")
VERSION_IN_BRACKET = re.compile(r"\b(remix|mix|edit|dub|version|rework|vip|bootleg|flip)\b", re.I)
UNRELEASED_TAG = re.compile(r"\b(unreleased|forthcoming|dub ?plate|id)\b", re.I)
MASHUP = re.compile(r"\s+(?:w/|with|vs\.?|x)\s+(?=.*\s[-–—]\s|id\b)", re.I)
JUNK_REST = re.compile(r"^(?:intro|outro|start|end|tracklist|break)$", re.I)


def ts_to_sec(ts):
    parts = [int(p) for p in ts.split(":")]
    sec = 0
    for p in parts:
        sec = sec * 60 + p
    return sec


def sec_to_ts(sec):
    if sec is None:
        return None
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def clean(s):
    s = URL.sub("", s)
    s = re.sub(r"[‘’]", "'", s)
    s = re.sub(r"[ \u00a0\r]+", " ", s)
    s = re.sub(r"\s*\t[\s\t]*", "\t", s)  # keep tabs, some lists use them as the separator
    return s.strip(" -–—|*•\t")


def split_pair(text):
    """Return (left, right) on the first spaced dash, or None."""
    text = clean(text)
    if "\t" in text:
        cells = [c.strip() for c in text.split("\t") if c.strip()]
        dashed = [c for c in cells if SEP.search(c)]
        if dashed:
            text = dashed[0]
        elif len(cells) == 2:
            return cells[0], cells[1].rstrip(",")
        else:
            text = " ".join(cells)
    parts = SEP.split(text, maxsplit=1)
    if len(parts) != 2:
        # tolerate "Artist -Title" or "Artist- Title"
        m = re.match(r"^(.+?)\s?[-–—]\s?(.+)$", text)
        if not m or len(m.group(1)) < 2:
            return None
        parts = [m.group(1), m.group(2)]
    left, right = parts[0].strip(), parts[1].strip()
    if right.lower().startswith(left.lower() + " - "):  # "Artist - Artist - Title"
        right = right[len(left) + 3:].strip()
    if not left or not right:
        return None
    return left, right


def side_score(left, right):
    """Positive means left is the title (Title - Artist order)."""
    score = 0
    if TITLE_HINTS.search(left):
        score += 2
    if TITLE_HINTS.search(right):
        score -= 2
    if ARTIST_HINTS.search(right) and not ARTIST_HINTS.search(left):
        score += 1
    if ARTIST_HINTS.search(left) and not ARTIST_HINTS.search(right):
        score -= 1
    return score


def orient(pairs):
    """Decide set-wide whether lines are Artist - Title (default) or Title - Artist."""
    total = sum(side_score(l, r) for l, r in pairs)
    return "title_first" if total > 0 else "artist_first"


def build_tracks(raw, source):
    """raw: list of (seconds_or_None, text). Returns track dicts."""
    pairs, rows = [], []
    for sec, text in raw:
        text = clean(text)
        if not text or JUNK_REST.match(text):
            continue
        pair = split_pair(text)
        rows.append((sec, text, pair))
        if pair:
            pairs.append(pair)
    order = orient(pairs) if pairs else "artist_first"
    tracks = []
    for sec, text, pair in rows:
        if pair:
            own = side_score(*pair)
            flip = own >= 2 or (order == "title_first" and own > -2)
            artist, title = (pair[1], pair[0]) if flip else pair
            artist, title = artist.replace("\t", " "), title.replace("\t", " ")
        else:
            artist, title = "", text
        tags = []
        for m in BRACKET.finditer(title):
            if not VERSION_IN_BRACKET.search(m.group(1)):
                tags.append(m.group(1).strip())
        title = BRACKET.sub(lambda m: m.group(0) if VERSION_IN_BRACKET.search(m.group(1)) else "", title).strip()
        mashup = None
        parts = MASHUP.split(title, maxsplit=1)
        if len(parts) == 2:   # "Never Stop w/ Masters At Work - Work": play two records at once
            title, mashup = parts[0].strip(), parts[1].strip()
        is_id = bool(UNKNOWN.match(title) or UNKNOWN.match(artist) or re.match(r"^id\s*-\s*id$", text, re.I))
        conf = "high" if pair and source in ("chapters", "description") else "medium"
        if is_id or not pair:
            conf = "low"
        tracks.append({
            "time": sec_to_ts(sec),
            "seconds": sec,
            "artist": artist,
            "title": title,
            "is_id": is_id,
            "tags": tags,
            "unreleased": any(UNRELEASED_TAG.search(t) for t in tags),
            "mashup": mashup,
            "source": source,
            "confidence": conf,
        })
    return tracks, order


def from_chapters(chapters):
    raw = [(c.get("start_time"), c.get("title", "")) for c in chapters or []]
    # Chapters that are only "Intro" or numbered placeholders are not a tracklist
    usable = [r for r in raw if split_pair(r[1])]
    if len(usable) < max(3, len(raw) // 2):
        return []
    return raw


def from_text_timestamps(text):
    raw = []
    for line in text.splitlines():
        m = TS_LINE.match(line)
        if m and len(m.group("rest")) > 2:
            raw.append((ts_to_sec(m.group("ts")), m.group("rest")))
    if len(raw) >= 3:
        return raw
    trail = []
    for line in text.splitlines():
        m = TS_TRAIL.match(line)
        if m and SEP.search(m.group("rest")):
            trail.append((ts_to_sec(m.group("ts")), m.group("rest")))
    return trail if len(trail) > len(raw) else raw


def from_text_untimed(text):
    """Untimed lines after a Tracklist heading, until a blank gap or a link block."""
    lines = text.splitlines()
    raw, in_list, blanks = [], False, 0
    for line in lines:
        if not in_list:
            if TRACKLIST_HEADING.search(line) and len(line) < 60:
                in_list = True
            continue
        if not line.strip():
            blanks += 1
            if blanks >= 2 and raw:
                break
            continue
        blanks = 0
        body = NUMBERED_LINE.match(line)
        body = body.group("rest") if body else line
        if URL.fullmatch(body.strip()):
            if raw:
                break
            continue
        if split_pair(body):
            raw.append((None, body))
        elif raw:
            break
    return raw


NUMBERED_ANY = re.compile(r"^\s*(\d{1,3})(?:[.)]|\t| )\s*(.+?)\s*$")


def from_text_numbered(text):
    """Untimed numbered lists with no heading: 1 Artist - Title, 2 ..., counting up."""
    raw, expect = [], None
    for line in text.splitlines():
        m = NUMBERED_ANY.match(line)
        if not m:
            continue
        n = int(m.group(1))
        if expect is None and n > 2:
            continue
        if expect is not None and n != expect:
            if len(raw) >= 3:
                break
            raw = []
            if n > 2:
                expect = None
                continue
        if split_pair(m.group(2)) or len(m.group(2)) > 3:
            raw.append((None, m.group(2)))
        expect = n + 1
    return raw


def best_from_text(text):
    timed = from_text_timestamps(text)
    if len(timed) >= 3:
        return timed
    for fn in (from_text_untimed, from_text_numbered):
        untimed = fn(text)
        if len(untimed) >= 3:
            return untimed
    return []


def run_ytdlp(args, timeout):
    proc = subprocess.run([YTDLP, *args], capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        err = proc.stderr.strip().splitlines()
        raise RuntimeError(err[-1] if err else "yt-dlp failed")
    return json.loads(proc.stdout)


# ---------- sources ----------
# Every source returns the same two shapes, so parsing never cares where data came from:
#   info:     {url, title, uploader, duration, thumbnail, platform, upload_date, id, description, chapters}
#   comments: [{id, parent ("root" or a comment id), text, likes, author, is_uploader}]

YT_ID = re.compile(r"(?:youtube\.com/(?:watch\?(?:.*&)?v=|live/|shorts/|embed/)|youtu\.be/)([A-Za-z0-9_-]{11})")


def youtube_id(url):
    m = YT_ID.search(url or "")
    return m.group(1) if m else None


class YtdlpSource:
    """Local mode. Reads public pages with yt-dlp; fine on your own machine, not for a public server."""
    name = "yt-dlp"

    def info(self, url):
        d = run_ytdlp(["-J", "--skip-download", "--no-playlist", "--no-warnings", url], timeout=45)
        return {"url": d.get("webpage_url") or url, "title": d.get("title"),
                "uploader": d.get("uploader") or d.get("channel"), "duration": d.get("duration"),
                "thumbnail": d.get("thumbnail"), "platform": d.get("extractor_key"),
                "upload_date": d.get("upload_date"), "id": d.get("id"),
                "description": d.get("description") or "", "chapters": d.get("chapters")}

    def comments(self, url):
        d = run_ytdlp(["-J", "--skip-download", "--no-playlist", "--no-warnings", "--write-comments",
                       "--extractor-args", "youtube:max_comments=300,all,300,10;comment_sort=top", url], timeout=120)
        return [{"id": c.get("id"), "parent": c.get("parent") or "root", "text": c.get("text") or "",
                 "likes": c.get("like_count") or 0, "author": c.get("author") or "",
                 "is_uploader": bool(c.get("author_is_uploader"))} for c in d.get("comments") or []]


def source_for(url, cfg=None):
    cfg = cfg or {}
    if youtube_id(url) and cfg.get("youtube_api_key"):
        import youtube_api
        return youtube_api.Source(cfg["youtube_api_key"])
    if cfg.get("public_mode"):
        if youtube_id(url):
            raise RuntimeError("Public mode needs a YouTube API key in config.json")
        raise RuntimeError("Only YouTube links are supported on the public site for now")
    return YtdlpSource()


# ---------- comment answers ----------
# Under a set, people post "13:40 ID???" and someone replies "Miro (AR) - Watching Me".
# Producers post "Track at 1:09:00 is my unreleased track 'Give Me The Rythm'". Both are
# answers to an ID: a timestamp, plus a name, from a reply or from the comment itself.

ASK = re.compile(r"\b(id|track ?id|what(?:'s| is)? (?:the |this )?(?:song|track|tune)|song name|track name|name of)\b|\?\s*$", re.I)
QUOTED = re.compile(r"[\"“‘']([^\"“”‘’']{3,60})[\"”’']")
SELF_ID = re.compile(r"\b(my (?:new |unreleased |upcoming )?(?:track|tune|record|edit|remix|song)|"
                     r"thx|thanks|thank you)\b.*\b(for (?:the )?(?:support|spin|playing|supporting))?", re.I)
TS_ANY = re.compile(TS)
EMOJI = re.compile(r":[a-z][a-z0-9_-]*:|[\U0001F000-\U0001FAFF\u2600-\u27BF\u200d\ufe0f]", re.I)
CHATTER = re.compile(r"\s+(?:thx|thanks|thank you|huge|need|what a|so good|banger|fire|love|<3|!).*$", re.I)
LABEL = re.compile(r"^\s*(?:it'?s|this is|(?:the )?(?:track|song|tune|id)(?: is)?\s*[:\-]?)\s+", re.I)


def tidy_title(t):
    t = EMOJI.sub("", t)
    t = CHATTER.sub("", t)
    return re.sub(r"\s+", " ", t).strip(" -–:|.,")


def name_in(text):
    """An 'Artist - Title' or a quoted title in free text. Returns (artist, title) or None."""
    text = EMOJI.sub(" ", re.sub(r"@\S+", "", text))
    for line in text.splitlines():
        line = re.sub(r"[\[(]?\s*" + TS + r"\s*[\])]?", "", line)
        line = LABEL.sub("", line.strip(" -–:|")).strip()
        if not SEP.search(line) or re.search(r"https?://", line):   # free text needs a real spaced dash
            continue
        pair = split_pair(line)
        if pair:
            a, t = LABEL.sub("", pair[0]).strip(), tidy_title(pair[1])
            # "Big up for the spin at FINKY - Move Ya Body": keep what follows the lead-in
            if len(a.split()) > 3:
                a = re.sub(r"^.*\b(?:at|is|by|from|of|spin|playing|played)\s+", "", a, flags=re.I).strip()
            if t and len(a.split()) <= 8 and len(t.split()) <= 14:
                return a, t
    m = QUOTED.search(text)
    if m:
        return "", tidy_title(m.group(1))
    return None


def comment_answers(comments, duration=None):
    """Group every name people attach to a timestamp. Returns answers sorted by time."""
    kids = {}
    for c in comments:
        if c["parent"] != "root":
            kids.setdefault(c["parent"], []).append(c)
    found = []
    for c in comments:
        if c["parent"] != "root":
            continue
        stamps = TS_ANY.findall(c["text"])
        if len(stamps) != 1:          # tracklists and multi-stamp comments are handled elsewhere
            continue
        at = ts_to_sec(stamps[0])
        if duration and at > duration:
            continue
        own = name_in(c["text"])
        if own and (own[0] or SELF_ID.search(c["text"])):
            # A quoted title with "my track" or "thanks for the support": the commenter made it
            artist = own[0] or re.sub(r"^@|(official|oficial|music)$", "", c["author"], flags=re.I).strip()
            title = own[1]
            collab = re.search(r"\s+(?:w/|with|together with|feat\.?|ft\.?)\s+(.+)$", title, re.I) or \
                re.search(r"[\"”’']\s*(?:w/|with|together with)\s+([A-Z][\w.]*(?:\s[A-Z][\w.]*)*)", c["text"])
            if collab:
                title = title[:collab.start()].strip() if collab.re.pattern.startswith(r"\s+") else title
                artist = f"{artist}, {collab.group(1).strip()}"
            own = (artist, title)
            found.append({"seconds": at, "artist": own[0], "title": own[1], "votes": 1 + c["likes"],
                          "by_artist": bool(SELF_ID.search(c["text"])), "evidence": c["text"][:160]})
        if ASK.search(c["text"]) or not own:
            for k in kids.get(c["id"], []):
                ans = name_in(k["text"])
                if ans and ans[0]:   # replies need a full Artist - Title, quotes alone are too noisy
                    found.append({"seconds": at, "artist": ans[0], "title": ans[1], "votes": 1 + k["likes"],
                                  "by_artist": False, "evidence": f'{c["text"][:80]} -> {k["text"][:80]}'})
    # Merge the same name said near the same time
    merged = []
    for f in sorted(found, key=lambda f: f["seconds"]):
        key = re.sub(r"[^a-z0-9]", "", re.split(r"[(\[]", f["title"].lower())[0])
        # "Give Me The Rythm" and "Give Me The Rhythm" said a minute apart are the same answer
        hit = next((m for m in merged if abs(m["seconds"] - f["seconds"]) <= 180 and
                    SequenceMatcher(None, m["_key"], key).ratio() >= 0.8), None)
        if hit:
            hit["votes"] += f["votes"]
            hit["by_artist"] |= f["by_artist"]
            hit["mentions"] += 1
            if not hit["artist"] and f["artist"]:
                hit["artist"] = f["artist"]
        else:
            merged.append({**f, "_key": key, "mentions": 1})
    for m in merged:
        m.pop("_key")
        m["time"] = sec_to_ts(m["seconds"])
    return merged


def apply_answers(tracks, answers, duration=None):
    """Fill ID slots from answers that fall inside them. Returns {track index: answer}."""
    fills = {}
    for i, t in enumerate(tracks):
        if not t.get("is_id") or t.get("seconds") is None:
            continue
        end = tracks[i + 1]["seconds"] if i + 1 < len(tracks) and tracks[i + 1].get("seconds") else (duration or 1e9)
        inside = [a for a in answers if t["seconds"] - 30 <= a["seconds"] < end]
        if inside:
            fills[i] = max(inside, key=lambda a: (a["by_artist"], a["votes"]))
    return fills


_COMMENTS = {}   # url -> (fetched at, comments); extract and answers_for share one fetch


def comments_for(src, url):
    hit = _COMMENTS.get(url)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    cs = src.comments(url)
    _COMMENTS[url] = (time.time(), cs)
    return cs


def answers_for(url, tracks, duration, cfg=None):
    comments = comments_for(source_for(url, cfg), url)
    answers = comment_answers(comments, duration)
    return {"answers": answers, "fills": apply_answers(tracks, answers, duration), "comments_read": len(comments)}


def extract(url, allow_comments=True, cfg=None):
    src = source_for(url, cfg)
    info = src.info(url)
    meta = {k: info.get(k) for k in ("url", "title", "uploader", "duration", "thumbnail", "platform", "upload_date", "id")}
    meta["duration_ts"] = sec_to_ts(info.get("duration"))
    meta["via"] = src.name
    desc = info.get("description") or ""
    links_1001 = sorted(set(TL1001.findall(desc)))

    raw, source = from_chapters(info.get("chapters")), "chapters"
    if len(raw) < 3:
        raw, source = best_from_text(desc), "description"

    comment_note, answers = None, []
    if len(raw) < 3 and allow_comments and meta["platform"] == "Youtube":
        try:
            comments = comments_for(src, url)
            best = []
            for c in comments:
                if c["parent"] == "root":
                    cand = best_from_text(c["text"])
                    if len(cand) > len(best):
                        best = cand
            if len(best) >= 3:
                raw, source = best, "comments"
            else:
                # No full tracklist anywhere: build one from the IDs people answered
                answers = comment_answers(comments, meta["duration"])
                comment_note = ("No full tracklist yet. These are the tracks people named in the comments."
                                if answers else "Checked the comments, no tracklist there yet.")
        except Exception as e:  # comments are a bonus, never fatal
            comment_note = f"Could not read comments ({e})."

    if raw:
        tracks, order = build_tracks(raw, source)
    elif answers:
        tracks, order = build_tracks([(a["seconds"], f'{a["artist"]} - {a["title"]}' if a["artist"] else a["title"])
                                      for a in answers], "answers"), "artist_first"
        tracks = tracks[0]
        source = "answers"
    else:
        tracks, order = [], None

    # Fill end times so each track has a length
    for i, t in enumerate(tracks):
        nxt = tracks[i + 1]["seconds"] if i + 1 < len(tracks) else meta["duration"]
        if t["seconds"] is not None and nxt:
            t["length"] = max(0, int(nxt - t["seconds"]))

    return {
        "set": meta,
        "tracks": tracks,
        "source": source if tracks else None,
        "order": order,
        "links_1001": links_1001,
        "note": comment_note,
        "identified": sum(1 for t in tracks if not t["is_id"]),
    }


if __name__ == "__main__":
    print(json.dumps(extract(sys.argv[1]), indent=2))
