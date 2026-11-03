"""Local audio matcher: which version of a song did the DJ actually play?

Landmark fingerprinting (spectrogram peak pairs, the approach Shazam published)
run on our own machine, so we can compare a slice of the set against any
candidate audio we can fetch: originals, remixes, edits, bootlegs on SoundCloud.

DJs rarely play a track at its native speed, so the set slice is fingerprinted at
a grid of tempo ratios, both key-locked (atempo, the CDJ default) and pitched
(asetrate, vinyl style). The candidate that lines up best at any ratio wins.

A shared vocal will make the original score something against a remix, but the
right version matches the drums and bassline too, so it scores far higher.
"""

import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
import imageio_ffmpeg
import numpy as np
from scipy.ndimage import maximum_filter

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
SR = 8000
N_FFT = 1024
HOP = 256
FAN = 6            # pairs per anchor peak
DT_MAX = 48        # frames, about 1.5 s
DF_MAX = 80        # bins
RATIOS = [round(1 + k / 100, 2) for k in range(-8, 9)]


def load(src, start=None, dur=None, ratio=1.0, mode="tempo"):
    """Decode any file or URL to mono float at SR, optionally sped up by ratio."""
    cmd = [FFMPEG, "-loglevel", "error"]
    if start is not None:
        cmd += ["-ss", str(start)]
    if dur is not None:
        cmd += ["-t", str(dur)]
    cmd += ["-i", str(src), "-ac", "1"]
    if ratio != 1.0:
        # undo the DJ's speed change so the slice plays at the record's native speed
        cmd += ["-af", f"atempo={1 / ratio:.4f}" if mode == "tempo"
                else f"asetrate={SR * 2}*{1 / ratio:.4f},aresample={SR}"]  # variants run on the 16 kHz wav
    cmd += ["-ar", str(SR), "-f", "s16le", "-"]
    raw = subprocess.run(cmd, capture_output=True, check=True, timeout=120).stdout
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768


def peaks(y):
    if len(y) < N_FFT:
        return np.empty((0, 2), dtype=np.int32)
    frames = np.lib.stride_tricks.sliding_window_view(y, N_FFT)[::HOP] * np.hanning(N_FFT)
    S = np.log1p(np.abs(np.fft.rfft(frames, axis=1)).T * 10)   # (freq, time)
    S[:8] = 0                                                  # drop sub-bass rumble under ~60 Hz
    local = maximum_filter(S, size=(21, 11)) == S
    thresh = S > (S.mean() + 0.5 * S.std())
    f, t = np.nonzero(local & thresh)
    order = np.argsort(t)
    return np.stack([t[order], f[order]], axis=1)


def hashes(y):
    """Return (hash, time) arrays. hash packs f1, f2, dt. Pairs each peak with the next few."""
    p = peaks(y)
    hs, ts = [], []
    for k in range(1, FAN * 2 + 1):
        if len(p) <= k:
            break
        a, b = p[:-k], p[k:]
        dt = b[:, 0] - a[:, 0]
        ok = (dt >= 1) & (dt <= DT_MAX) & (np.abs(b[:, 1] - a[:, 1]) <= DF_MAX)
        hs.append((a[ok, 1].astype(np.int64) << 16) | (b[ok, 1].astype(np.int64) << 6) | dt[ok])
        ts.append(a[ok, 0])
    if not hs:
        return np.empty(0, np.int64), np.empty(0, np.int32)
    return np.concatenate(hs), np.concatenate(ts).astype(np.int32)


class Reference:
    """One candidate version, indexed for lookups."""

    def __init__(self, y=None, h=None, t=None):
        if y is not None:
            h, t = hashes(y)
        order = np.argsort(h, kind="stable")
        self.h, self.t = h[order], t[order]
        self.n = len(h)

    # A saved fingerprint is about 1% of the audio's size and is all a comparison needs
    def save(self, path):
        np.savez_compressed(path, h=self.h, t=self.t)

    @classmethod
    def open(cls, path):
        with np.load(path) as d:
            return cls(h=d["h"], t=d["t"])


def query_variants(src, start, dur, jobs=None):
    """Fingerprint the set slice once per tempo ratio and mode (ffmpeg runs in parallel)."""
    if jobs is None:
        jobs = [(r, m) for m in ("tempo", "rate") for r in RATIOS if not (m == "rate" and r == 1.0)]
    with tempfile.NamedTemporaryFile(suffix=".wav") as wav:
        # decode the slice once, every variant then stretches a small local wav
        subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-ss", str(start), "-t", str(dur), "-i", str(src),
                        "-ac", "1", "-ar", str(SR * 2), wav.name], check=True, timeout=120)

        def one(job):
            h, t = hashes(load(wav.name, None, None, *job))
            return {"ratio": job[0], "mode": job[1], "h": h, "t": t}
        with ThreadPoolExecutor(max_workers=8) as pool:
            return list(pool.map(one, jobs))


def refine_jobs(hits):
    """Finer ratios around each coarse best guess. A real DJ speed like 1.016 (126 to 128 BPM)
    falls between the 1% steps, and over a 90 s slice that drift smears the alignment."""
    jobs = set()
    for ratio, mode in hits:
        for d in (-0.0075, -0.005, -0.0025, 0.0025, 0.005, 0.0075):
            jobs.add((round(ratio + d, 4), mode))
    return sorted(jobs)


def score(variants, ref):
    """Best count of hashes agreeing on one time offset, across all variants."""
    best = {"aligned": 0, "ratio": 1.0, "mode": "tempo"}
    for v in variants:
        lo = np.searchsorted(ref.h, v["h"], "left")
        hi = np.searchsorted(ref.h, v["h"], "right")
        cnt = hi - lo
        if not cnt.sum():
            continue
        qi = np.repeat(np.arange(len(v["h"])), cnt)
        ri = np.concatenate([np.arange(a, b) for a, b in zip(lo[cnt > 0], hi[cnt > 0])])
        offs = (ref.t[ri] - v["t"][qi]) // 2          # 2-frame bins absorb small jitter
        vals, counts = np.unique(offs, return_counts=True)
        i = counts.argmax()
        if counts[i] > best["aligned"]:
            # Coverage: in how many 5 s windows of the slice does this version line up?
            # The record actually playing lines up the whole way, a rework that borrows
            # the original vocal only lines up where it reuses parts.
            hit_t = v["t"][qi][offs == vals[i]]
            win = int(5 * SR / HOP)
            n_win = max(1, int(v["t"].max()) // win + 1) if len(v["t"]) else 1
            windows = np.unique(hit_t // win, return_counts=True)
            covered = int((windows[1] >= 3).sum())
            best = {"aligned": int(counts[i]), "ratio": v["ratio"], "mode": v["mode"],
                    "offset_s": round(float(vals[i]) * 2 * HOP / SR, 1),
                    "coverage": round(covered / n_win, 2)}
        best.setdefault("coverage", 0)
    q = len(variants[0]["h"]) if variants else 1
    best["share"] = round(best["aligned"] / max(1, q), 4)
    best["fit"] = round(best["aligned"] * best.get("coverage", 0), 1)
    return best


def better(a, b):
    """Keep whichever score lines up better, by fit then raw count."""
    return a if (a.get("fit", 0), a["aligned"]) >= (b.get("fit", 0), b["aligned"]) else b


def rank(set_src, start, dur, candidates, fetch):
    """candidates: list of dicts. fetch(c) -> audio path or URL. Returns ranked list."""
    variants = query_variants(set_src, start, dur)
    out = []
    for c in candidates:
        try:
            ref = Reference(load(fetch(c)))
            s = score(variants, ref)
        except Exception as e:
            s = {"aligned": 0, "share": 0, "error": str(e)}
        out.append({**c, **s})
    out.sort(key=lambda c: -c["aligned"])
    return out
