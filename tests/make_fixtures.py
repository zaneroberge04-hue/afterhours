"""Fetch real sets through the YouTube API, run the Python parser, save inputs + outputs.
tests/parity.mjs then runs the JS parser on the same inputs and diffs the results.

  .venv/bin/python tests/make_fixtures.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import tracklist  # noqa: E402

SETS = ["UE6wjxvMRz0", "JUDUC87VuPU", "Z7AdRuZFaA0", "bk6Xst6euQk", "pRnr9hRooAM", "qDAgkOuijUg",
        "sXf-DrNInHQ", "c0-hvjV2A5Y", "MieRtF-IpQA", "rAOHJqJMYDA"]
cfg = json.loads((ROOT / "config.json").read_text())
out = ROOT / "tests" / "fixtures"
out.mkdir(parents=True, exist_ok=True)


class Fixed:
    name = "YouTube API"

    def __init__(self, info, comments):
        self._i, self._c = info, comments

    def info(self, url):
        return self._i

    def comments(self, url):
        return self._c


# Text formats seen in the wild, checked line for line between the two parsers
TEXTS = [
    "Tracklist:\n1. Kiko Navarro - Sex & Love Affair [Discoterraneo] 00:00\n2. Sidwho? - Shake Your Body (Yusek Remix) [Glitterbox recordings] 05:06\n3. Vicky D - This Beat Is Mine (Michael Gray Remix) [Nervous] 10:11",
    "0:00 - 05:20 Soul Mass Transit System - Jump (Rushing mix)\n05:20 - 09:12 Sydney Charles, BUGS - Trip Advisor\n18:22 - 19:30 ID - ID",
    "00:00 Happy Music - Supershy\n01:57 Oh Honey - Angelo Ferreri\n10:21 Da Luna (Mobin Master Extended Remix) - Supafly",
    "FULL TRACKLIST\n\n1\tLocky, Max Dean - Can't Decide (Extended)\n\n2\tHector Couto - Red Velvet (Original Mix)\n\n3\tToomy Disco - Chances",
    "Tracklist.........\n1\tLover For An Angel (Extended)\tLee Butler\n2\tFreed from Desire (Extended Mix)\tRata\n3\tAt Night (Extended)\tLee Butler",
    "[0:14:37] FINKY - Move Ya Body [unreleased]\n29:50 Simes, Nic Lorenz - Lovers Paradise [unreleased]\n44:40 PAWSA - TOO COOL w/ Cloonee - Sippin' Yak",
]
texts = []
for t in TEXTS:
    tr, order = tracklist.build_tracks(tracklist.best_from_text(t), "description")
    texts.append({"text": t, "tracks": tr, "order": order})
(out / "_texts.json").write_text(json.dumps(texts))
print(f"{len(texts)} text samples")

real_source_for = tracklist.source_for
for vid in SETS:
    url = f"https://www.youtube.com/watch?v={vid}"
    src = real_source_for(url, cfg)
    info, comments = src.info(url), src.comments(url)
    tracklist._COMMENTS.clear()
    tracklist.source_for = lambda u, c=None, f=Fixed(info, comments): f
    r = tracklist.extract(url, cfg=cfg)
    answers = tracklist.comment_answers(comments, info["duration"])
    fills = tracklist.apply_answers(r["tracks"], answers, info["duration"])
    (out / f"{vid}.json").write_text(json.dumps({"info": info, "comments": comments,
        "expected": {"result": r, "answers": answers, "fills": {str(k): v for k, v in fills.items()}}}))
    print(f"{vid}: {r['source']} {len(r['tracks'])} tracks, {len(answers)} answers, {len(comments)} comments")
