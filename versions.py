"""Which version did the DJ play?

Takes a track whose base song is known (from the tracklist or a fingerprint hit),
gathers every version findable online (original, extended, remixes, edits,
bootlegs) from Deezer, YouTube and SoundCloud, then compares a slice of the set
against each with remix_match and says which one it is, or that none of them are.
"""

import json
import re
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import fingerprint
import remix_match
import tracklist

PRINTS = Path(__file__).parent / "cache" / "prints"   # fingerprints only, never audio
PRINTS.mkdir(parents=True, exist_ok=True)
MAX_CANDIDATES = 24
MATCH_MIN = 60        # aligned hashes; wrong versions scored under 40 in testing, right ones 120+
MARGIN = 3.0          # winner must beat the best other version family by this factor
COVERAGE_MIN = 0.4    # share of 5 s windows that line up; the record playing scored 0.53 and 1.0, a mismatch 0.28

JOBS = {}

VERSION_WORDS = re.compile(r"\b(remix|mix|edit|dub|version|rework|vip|bootleg|flip|refix|rerub|re-edit)\b", re.I)
FAMILY_NOISE = re.compile(r"\b(extended|radio|club|original|edit|mix|version|short|long|full|instrumental|unreleased|id|"
                          r"free|dl|download|official|audio|video|visualizer|lyric|lyrics|remake|out now|premiere)\b|[\"'()\[\]+]", re.I)


def norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def core_title(title):
    core = re.sub(r"\s*[\(\[].*?[\)\]]", "", title or "").strip()
    core = re.split(r"\s+(?:feat\.?|ft\.?)\s+", core, flags=re.I)[0].strip()
    if not core or core.lower() == "id":
        # "ID (Momma Used To Dance Like That)": the working title is in the brackets
        inner = re.findall(r"[\(\[](.*?)[\)\]]", title or "")
        core = inner[0].strip() if inner else core
    return core


STRONG_VERSION = re.compile(r"\b(remix|edit|bootleg|rework|flip|refix|rerub|re-edit|dub)\b", re.I)


def version_label(title):
    tags = [t for t in re.findall(r"[\(\[](.*?)[\)\]]", title or "") if VERSION_WORDS.search(t)]
    # "(Jersey Remix) [Prod by VIP]": the remix tag says who reworked it, not the producer credit
    strong = [t for t in tags if STRONG_VERSION.search(t)]
    tags = strong or tags
    return tags[-1].strip() if tags else ""


def family(title, core=""):
    """Who reworked it: 'X Extended Remix', 'X Remix Edit' and 'X EDIT (Unreleased ID)' all
    collapse to 'x'. '' is the original."""
    label = version_label(title)
    if not label:
        # YouTube/SoundCloud titles often say 'Song - Someone Remix' or 'Song SOMEONE EDIT' without brackets
        m = re.search(r"[-–]\s*([^-–]*\b(?:remix|edit|bootleg|rework|flip|vip)\b[^-–]*)$", title or "", re.I)
        label = m.group(1) if m else ""
    words = set(re.findall(r"[a-z0-9]+", core.lower()))
    f = FAMILY_NOISE.sub(" ", label.lower())
    f = " ".join(w for w in f.split() if w not in words)
    if f.strip() != "remix":
        f = re.sub(r"\bremix\b", " ", f)
    return re.sub(r"\s+", " ", f).strip(" -&")


def lead_artist(artist):
    return re.split(r",|&|\bfeat\.?|\bft\.?|\bvs\.?|\bx\b", artist or "", flags=re.I)[0].strip()


# ---------- candidate search ----------

def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Afterhours/0.1"})
    with urllib.request.urlopen(req, timeout=12) as r:
        return json.loads(r.read().decode())


def search_deezer(q):
    res = get_json("https://api.deezer.com/search?" + urllib.parse.urlencode({"q": q, "limit": 25}))
    return [{"source": "Deezer", "id": str(d["id"]), "title": d["title"], "artist": d["artist"]["name"],
             "duration": d.get("duration"), "link": d.get("link"), "audio": d.get("preview"),
             "preview_only": True} for d in res.get("data") or [] if d.get("preview")]


def search_ytdlp(prefix, q, n=8):
    proc = subprocess.run([tracklist.YTDLP, f"{prefix}{n}:{q}", "--flat-playlist", "-J", "--no-warnings"],
                          capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        return []
    src = "YouTube" if prefix == "ytsearch" else "SoundCloud"
    out = []
    for e in json.loads(proc.stdout).get("entries") or []:
        url = e.get("url") or e.get("webpage_url")
        if src == "YouTube" and e.get("id"):
            url = f"https://www.youtube.com/watch?v={e['id']}"
        out.append({"source": src, "id": str(e.get("id")), "title": e.get("title") or "",
                    "artist": e.get("uploader") or e.get("channel") or "", "duration": e.get("duration"),
                    "link": url, "audio": None, "preview_only": False})
    return out


def find_candidates(artist, title):
    """Every findable version of the song. Edits mostly live on SoundCloud, titled like
    'Got To Be Real (Silva Bumpa Edit)' and uploaded by the editor, not the original artist,
    so SoundCloud gets searched by version tag, not just by artist."""
    core, lead, label = core_title(title), lead_artist(artist), version_label(title)
    wanted = family(title, core)
    yt = lambda q, n=8: (search_ytdlp, ("ytsearch", q, n))
    sc = lambda q, n=12: (search_ytdlp, ("scsearch", q, n))
    searches = [
        (search_deezer, (f"{core} {lead}",)),
        yt(f"{lead} {core}"), yt(f"{core} remix"), yt(f"{core} edit"),
        sc(f"{lead} {core}"), sc(f"{core} edit"), sc(f"{core} remix"), sc(f"{core} bootleg"), sc(f"{core} rework flip"),
    ]
    if label:   # the tracklist names the remixer: look for exactly that, on both sites
        searches += [yt(f"{core} {label}"), sc(f"{core} {label}"), sc(f"{wanted} {core}")]
    with ThreadPoolExecutor(max_workers=len(searches)) as pool:
        batches = list(pool.map(lambda s: _safe(s[0], *s[1]), searches))

    key = norm(core)
    seen, out = set(), []
    for batch in batches:
        for c in batch:
            if key not in norm(c["title"]) or (c["source"], c["id"]) in seen:
                continue
            d = c.get("duration") or 0
            if d and (d < 90 or d > 900):          # skip clips, previews of mixes, and full sets
                continue
            if re.search(r"\b(karaoke|cover|tutorial|how to|slowed|sped up|nightcore|8d audio|reaction)\b", c["title"], re.I):
                continue   # not a version a DJ would play
            if re.search(r"\b(mix(tape)?|set|live at|boiler room|full album|hour|awakenings)\b", c["title"], re.I) \
                    and not VERSION_WORDS.search(version_label(c["title"]) or "x"):
                continue
            seen.add((c["source"], c["id"]))
            c["family"] = family(c["title"], core)
            out.append(c)

    # Fill the slots in tiers so one site or one remix cannot crowd out the rest:
    # the version the tracklist names, then the original, then one of every other
    # version alternating SoundCloud and YouTube, then second copies if room is left.
    picked, ids = [], set()

    def take(c):
        if id(c) not in ids and len(picked) < MAX_CANDIDATES:
            picked.append(c)
            ids.add(id(c))

    full_first = lambda c: (c["preview_only"], c["source"] != "YouTube")
    if wanted:
        for c in sorted([c for c in out if c["family"] == wanted], key=full_first)[:3]:
            take(c)
    for c in sorted([c for c in out if not c["family"]], key=full_first)[:2]:
        take(c)
    by_family = {}
    for c in out:
        if c["family"]:
            by_family.setdefault(c["family"], []).append(c)
    firsts = [sorted(v, key=lambda c: (c["preview_only"], c["source"] != "SoundCloud"))[0] for v in by_family.values()]
    sc_first = [c for c in firsts if c["source"] == "SoundCloud"]
    rest = [c for c in firsts if c["source"] != "SoundCloud"]
    for i in range(max(len(sc_first), len(rest))):
        for lst in (sc_first, rest):
            if i < len(lst):
                take(lst[i])
    for v in by_family.values():
        for c in v:
            take(c)
    for c in out:
        take(c)
    return picked


def _safe(fn, *args):
    try:
        return fn(*args)
    except Exception:
        return []


def print_path(c):
    return PRINTS / f"{c['source'].lower()}_{re.sub(r'[^A-Za-z0-9_-]', '', c['id'])}.npz"


def reference_for(c):
    """Fingerprint for one version. Audio is fetched to a temp folder, fingerprinted,
    and deleted straight away; only the fingerprint is kept."""
    path = print_path(c)
    if path.exists():
        return remix_match.Reference.open(path)
    if c["audio"]:   # Deezer preview, ffmpeg streams the URL, nothing touches disk
        ref = remix_match.Reference(remix_match.load(c["audio"]))
    else:
        with tempfile.TemporaryDirectory(dir=PRINTS) as tmp:
            subprocess.run([tracklist.YTDLP, "-q", "--no-warnings", "-f", "bestaudio[abr<=96]/bestaudio",
                            "-o", str(Path(tmp) / "a.%(ext)s"), c["link"]], capture_output=True, timeout=120)
            hits = [p for p in Path(tmp).iterdir() if not p.name.endswith(".part")]
            if not hits:
                raise RuntimeError("download failed")
            ref = remix_match.Reference(remix_match.load(hits[0]))
    ref.save(path)
    return ref


# ---------- verdict ----------

def verdict(ranked, title, artist="", mashup=None):
    core = core_title(title)
    lead = norm(lead_artist(artist))
    by_artist = any(lead and lead in norm(c["title"] + " " + c.get("artist", "")) for c in ranked)
    if (not ranked or ranked[0]["aligned"] < MATCH_MIN) and not by_artist:
        return {"kind": "notfound", "headline": f"Could not find {core} online",
                "detail": f"Nothing by {lead_artist(artist) or 'this artist'} with this title on Deezer, YouTube or "
                          "SoundCloud. It may be unreleased, or only on Bandcamp or Beatport."}
    if not ranked or ranked[0]["aligned"] < MATCH_MIN:
        return {"kind": "unreleased",
                "headline": f"Not any version of {core} that is online",
                "detail": f"Compared {len(ranked)} versions and none line up. Most likely an unreleased edit or remix."}
    top = ranked[0]
    rival = next((c for c in ranked[1:] if c["family"] != top["family"]), None)
    margin = top.get("fit", 0) / max(1, rival.get("fit", 0)) if rival else 99
    speed = ""
    if top.get("ratio") and top["ratio"] != 1.0:
        pct = round((top["ratio"] - 1) * 100, 1)
        speed = f"Played {abs(pct):g}% {'faster' if pct > 0 else 'slower'}" + \
                (", key lock on" if top.get("mode") == "tempo" else ", pitched like vinyl")
    name = top["title"]
    if (top.get("coverage") or 0) < COVERAGE_MIN:
        why = (f"The tracklist says it was mashed with {mashup}, which explains the gaps." if mashup else
               "Most likely an unreleased edit, or the DJ was layering another record.")
        return {"kind": "partial", "headline": f"Built on {core}, but not a version that is online",
                "detail": f"The closest, {name}, only lines up {round(top['coverage'] * 100)}% of the time. {why}",
                "speed": speed, "pick": top}
    if margin < MARGIN:
        detail = f"Close call with {rival['title']}. Listen to both."
        if mashup:
            # The tracklist says what it was mashed with; point at a strong candidate that names that partner
            partner = norm(core_title(re.split(r"\s+[-–]\s+", mashup)[-1]))
            hit = next((c for c in ranked[:5] if partner and partner in norm(c["title"]) and c is not top
                        and c.get("fit", 0) >= 0.4 * top.get("fit", 0)), None)
            if hit:
                detail += f" {hit['title']} also fits and matches the mashup in the tracklist."
        return {"kind": "close", "headline": f"Most likely {name}", "detail": detail, "speed": speed, "pick": top}
    kind = "original" if not top["family"] else "match"
    return {"kind": kind, "headline": ("The original: " if kind == "original" else "It's ") + name,
            "detail": f"Lines up {margin:.0f}x better than any other version found.", "speed": speed, "pick": top}


# ---------- jobs ----------

def run(job):
    try:
        job["phase"] = "Getting the set audio"
        set_audio = fingerprint.download_audio(job["url"], job["set_id"], lambda p: job.update(download=p))

        job["phase"] = "Finding versions online"
        cands = find_candidates(job["artist"], job["title"])
        job["total"] = len(cands)
        if not cands:
            job.update(status="done", phase="Done", verdict={"kind": "none", "headline": "No versions found online",
                       "detail": "Nothing on Deezer, YouTube or SoundCloud with this title."})
            return

        job["phase"] = f"Comparing {len(cands)} versions"
        variants = remix_match.query_variants(set_audio, job["start"], job["dur"])
        refs = {}

        def one(c):
            try:
                refs[id(c)] = reference_for(c)
                c.update(remix_match.score(variants, refs[id(c)]))
            except Exception as e:
                c.update(aligned=0, share=0, coverage=0, fit=0, error=str(e))
            job["done"] += 1
            return c

        with ThreadPoolExecutor(max_workers=6) as pool:
            ranked = sorted(pool.map(one, cands), key=lambda c: (-c.get("fit", 0), -c["aligned"]))

        # Second pass: fine tempo steps around the leaders' best guesses
        # Low bar on purpose: a record played between steps can score under MATCH_MIN until refined
        leaders = [c for c in ranked[:5] if c["aligned"] >= 20 and id(c) in refs]
        if leaders:
            job["phase"] = "Fine-tuning tempo"
            fine = remix_match.query_variants(set_audio, job["start"], job["dur"],
                                              remix_match.refine_jobs({(c["ratio"], c["mode"]) for c in leaders}))
            for c in leaders:
                c.update(remix_match.better(remix_match.score(fine, refs[id(c)]), {k: c[k] for k in
                         ("aligned", "ratio", "mode", "offset_s", "coverage", "share", "fit") if k in c}))
            ranked.sort(key=lambda c: (-c.get("fit", 0), -c["aligned"]))
        job["candidates"] = [{k: c.get(k) for k in ("source", "title", "artist", "link", "aligned", "family",
                                                     "ratio", "mode", "coverage", "fit", "preview_only", "error")} for c in ranked]
        job["verdict"] = verdict(ranked, job["title"], job["artist"], job.get("mashup"))
        job.update(status="done", phase="Done")
    except Exception as e:
        job.update(status="error", error=str(e))


def start(url, set_id, artist, title, seconds, length, mashup=None):
    if seconds is None:
        raise ValueError("This track has no timestamp, so there is no slice of the set to compare")
    length = length or 180
    # skip the mix-in and mix-out, where two records play at once
    start_at = int(seconds + min(45, length * 0.25))
    dur = int(max(30, min(90, length * 0.55)))
    job = {"id": uuid.uuid4().hex[:10], "url": url, "set_id": set_id, "artist": artist, "title": title,
           "start": start_at, "dur": dur, "mashup": mashup, "status": "running", "phase": "Starting", "download": 0.0,
           "done": 0, "total": 0, "candidates": [], "verdict": None, "error": None, "started": time.time()}
    JOBS[job["id"]] = job
    threading.Thread(target=run, args=(job,), daemon=True).start()
    return job
