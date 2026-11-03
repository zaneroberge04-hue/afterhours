// JavaScript twin of tracklist.py, for the Cloudflare Pages Functions.
// Same rules, same output shapes. tests/parity.mjs runs both on the same sets and
// fails on any difference, so change the two files together.

export const TS = String.raw`(?:\d{1,2}:)?\d{1,2}:\d{2}`;
const TS_LINE = new RegExp(
  // "0:00 - 05:20 Artist - Title" ranges: keep the start, drop the end time
  String.raw`^\s*(?:\d{1,3}[.)]\s*)?[\[(]?(${TS})(?:\s*[-–—~]\s*${TS})?[\])]?\s*[-–—|:.]?\s*(.+?)\s*$`);
// "1. Artist - Title [Label] 00:00": radio shows put the time at the end
const TS_TRAIL = new RegExp(String.raw`^\s*(?:\d{1,3}[.)]\s*)?(.+?)\s*[\[(]?(${TS})[\])]?\s*$`);
const NUMBERED_LINE = /^\s*(?:\d{1,3}[.)]|[-•*])\s*(.+?)\s*$/;
const SEP = /\s+[-–—]\s+/;
const TRACKLIST_HEADING = /track\s*list|tracks?:|setlist|track\s*listing/i;
const URL_RE = /https?:\/\/\S+/g;
const TL1001 = /https?:\/\/(?:www\.)?(?:1001tracklists\.com|1001\.tl)\/\S+/gi;

const TITLE_HINTS = /\((?:[^)]*\b(?:remix|mix|edit|dub|version|rework|vip|bootleg|flip)\b)/i;
const ARTIST_HINTS = /,|&|\bfeat\.?|\bft\.?|\bvs\.?|\bx\b|\band\b/i;
const UNKNOWN = /^\s*(?:id|unknown(?: artist)?|\?+)\s*$/i;
const BRACKET = /\s*\[([^\]]{1,40})\]/g;
const VERSION_IN_BRACKET = /\b(remix|mix|edit|dub|version|rework|vip|bootleg|flip)\b/i;
const UNRELEASED_TAG = /\b(unreleased|forthcoming|dub ?plate|id)\b/i;
const MASHUP = /\s+(?:w\/|with|vs\.?|x)\s+(?=.*\s[-–—]\s|id\b)/i;
const JUNK_REST = /^(?:intro|outro|start|end|tracklist|break)$/i;

// ---------- python-isms ----------
const lines = s => s.split(/\r\n|\r|\n/);
const words = s => s.trim() ? s.trim().split(/\s+/) : [];
function strip(s, chars) {
  let a = 0, b = s.length;
  while (a < b && chars.includes(s[a])) a++;
  while (b > a && chars.includes(s[b - 1])) b--;
  return s.slice(a, b);
}
// difflib.SequenceMatcher(None, a, b).ratio() for short strings (no autojunk under 200 chars)
export function seqRatio(a, b) {
  const matches = (a1, a2, b1, b2) => {
    let best = 0, bi = a1, bj = b1;
    for (let i = a1; i < a2; i++) for (let j = b1; j < b2; j++) {
      let k = 0;
      while (i + k < a2 && j + k < b2 && a[i + k] === b[j + k]) k++;
      if (k > best) { best = k; bi = i; bj = j; }
    }
    if (!best) return 0;
    return best + matches(a1, bi, b1, bj) + matches(bi + best, a2, bj + best, b2);
  };
  const t = a.length + b.length;
  return t ? 2 * matches(0, a.length, 0, b.length) / t : 1;
}

export function tsToSec(ts) { return ts.split(":").reduce((s, p) => s * 60 + parseInt(p, 10), 0); }
export function secToTs(sec) {
  if (sec == null) return null;
  sec = Math.floor(sec);
  const h = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60), s = sec % 60;
  const p = n => String(n).padStart(2, "0");
  return h ? `${h}:${p(m)}:${p(s)}` : `${m}:${p(s)}`;
}

export function clean(s) {
  s = s.replace(URL_RE, "");
  s = s.replace(/[‘’]/g, "'");
  s = s.replace(/[  \r]+/g, " ");
  s = s.replace(/\s*\t[\s\t]*/g, "\t");
  return strip(s, " -–—|*•\t");
}

export function splitPair(text) {
  text = clean(text);
  if (text.includes("\t")) {
    const cells = text.split("\t").map(c => c.trim()).filter(Boolean);
    const dashed = cells.filter(c => SEP.test(c));
    if (dashed.length) text = dashed[0];
    else if (cells.length === 2) return [cells[0], cells[1].replace(/,+$/, "")];
    else text = cells.join(" ");
  }
  let parts;
  const m0 = text.match(SEP);
  if (m0) parts = [text.slice(0, m0.index), text.slice(m0.index + m0[0].length)];
  else {
    const m = text.match(/^(.+?)\s?[-–—]\s?(.+)$/);
    if (!m || m[1].length < 2) return null;
    parts = [m[1], m[2]];
  }
  let left = parts[0].trim(), right = parts[1].trim();
  if (right.toLowerCase().startsWith(left.toLowerCase() + " - ")) right = right.slice(left.length + 3).trim();
  if (!left || !right) return null;
  return [left, right];
}

function sideScore(left, right) {
  let score = 0;
  if (TITLE_HINTS.test(left)) score += 2;
  if (TITLE_HINTS.test(right)) score -= 2;
  if (ARTIST_HINTS.test(right) && !ARTIST_HINTS.test(left)) score += 1;
  if (ARTIST_HINTS.test(left) && !ARTIST_HINTS.test(right)) score -= 1;
  return score;
}

export function buildTracks(raw, source) {
  const pairs = [], rows = [];
  for (const [sec, t0] of raw) {
    const text = clean(t0);
    if (!text || JUNK_REST.test(text)) continue;
    const pair = splitPair(text);
    rows.push([sec, text, pair]);
    if (pair) pairs.push(pair);
  }
  const order = pairs.length && pairs.reduce((s, [l, r]) => s + sideScore(l, r), 0) > 0 ? "title_first" : "artist_first";
  const tracks = [];
  for (const [sec, text, pair] of rows) {
    let artist, title;
    if (pair) {
      const own = sideScore(...pair);
      const flip = own >= 2 || (order === "title_first" && own > -2);
      [artist, title] = flip ? [pair[1], pair[0]] : pair;
      artist = artist.replaceAll("\t", " "); title = title.replaceAll("\t", " ");
    } else [artist, title] = ["", text];
    const tags = [];
    for (const m of title.matchAll(BRACKET)) if (!VERSION_IN_BRACKET.test(m[1])) tags.push(m[1].trim());
    title = title.replace(BRACKET, (all, inner) => VERSION_IN_BRACKET.test(inner) ? all : "").trim();
    let mashup = null;
    const mm = title.match(MASHUP);
    if (mm) { mashup = title.slice(mm.index + mm[0].length).trim(); title = title.slice(0, mm.index).trim(); }
    const isId = UNKNOWN.test(title) || UNKNOWN.test(artist) || /^id\s*-\s*id$/i.test(text);
    let conf = pair && (source === "chapters" || source === "description") ? "high" : "medium";
    if (isId || !pair) conf = "low";
    tracks.push({time: secToTs(sec), seconds: sec, artist, title, is_id: isId, tags,
      unreleased: tags.some(t => UNRELEASED_TAG.test(t)), mashup, source, confidence: conf});
  }
  return [tracks, order];
}

export function fromChapters(chapters) {
  const raw = (chapters || []).map(c => [c.start_time, c.title || ""]);
  const usable = raw.filter(r => splitPair(r[1]));
  return usable.length < Math.max(3, Math.floor(raw.length / 2)) ? [] : raw;
}

function fromTextTimestamps(text) {
  const raw = [];
  for (const line of lines(text)) {
    const m = line.match(TS_LINE);
    if (m && m[2].length > 2) raw.push([tsToSec(m[1]), m[2]]);
  }
  if (raw.length >= 3) return raw;
  const trail = [];
  for (const line of lines(text)) {
    const m = line.match(TS_TRAIL);
    if (m && SEP.test(m[1])) trail.push([tsToSec(m[2]), m[1]]);
  }
  return trail.length > raw.length ? trail : raw;
}

function fromTextUntimed(text) {
  const raw = []; let inList = false, blanks = 0;
  for (const line of lines(text)) {
    if (!inList) { if (TRACKLIST_HEADING.test(line) && line.length < 60) inList = true; continue; }
    if (!line.trim()) { blanks++; if (blanks >= 2 && raw.length) break; continue; }
    blanks = 0;
    const nm = line.match(NUMBERED_LINE);
    const body = nm ? nm[1] : line;
    if (/^https?:\/\/\S+$/.test(body.trim())) { if (raw.length) break; continue; }
    if (splitPair(body)) raw.push([null, body]);
    else if (raw.length) break;
  }
  return raw;
}

const NUMBERED_ANY = /^\s*(\d{1,3})(?:[.)]|\t| )\s*(.+?)\s*$/;
function fromTextNumbered(text) {
  let raw = [], expect = null;
  for (const line of lines(text)) {
    const m = line.match(NUMBERED_ANY);
    if (!m) continue;
    const n = parseInt(m[1], 10);
    if (expect === null && n > 2) continue;
    if (expect !== null && n !== expect) {
      if (raw.length >= 3) break;
      raw = [];
      if (n > 2) { expect = null; continue; }
    }
    if (splitPair(m[2]) || m[2].length > 3) raw.push([null, m[2]]);
    expect = n + 1;
  }
  return raw;
}

export function bestFromText(text) {
  const timed = fromTextTimestamps(text);
  if (timed.length >= 3) return timed;
  for (const fn of [fromTextUntimed, fromTextNumbered]) {
    const u = fn(text);
    if (u.length >= 3) return u;
  }
  return [];
}

// ---------- comment answers ----------
export const ASK = /\b(id|track ?id|what(?:'s| is)? (?:the |this )?(?:song|track|tune)|song name|track name|name of)\b|\?\s*$/i;
const QUOTED = /["“‘']([^"“”‘’']{3,60})["”’']/;
const SELF_ID = /\b(my (?:new |unreleased |upcoming )?(?:track|tune|record|edit|remix|song)|thx|thanks|thank you)\b.*\b(for (?:the )?(?:support|spin|playing|supporting))?/i;
export const TS_ANY = new RegExp(TS, "g");
const EMOJI = /:[a-z][a-z0-9_-]*:|[\u{1F000}-\u{1FAFF}☀-➿‍️]/giu;
const CHATTER = /\s+(?:thx|thanks|thank you|huge|need|what a|so good|banger|fire|love|<3|!).*$/is;
const LABEL = /^\s*(?:it'?s|this is|(?:the )?(?:track|song|tune|id)(?: is)?\s*[:-]?)\s+/i;

function tidyTitle(t) {
  t = t.replace(EMOJI, "").replace(CHATTER, "");
  return strip(t.replace(/\s+/g, " "), " -–:|.,");
}

export function nameIn(text) {
  text = text.replace(/@\S+/g, "").replace(EMOJI, " ");
  for (let line of lines(text)) {
    line = line.replace(new RegExp(String.raw`[\[(]?\s*${TS}\s*[\])]?`, "g"), "");
    line = strip(line, " -–:|").replace(LABEL, "").trim();
    if (!SEP.test(line) || /https?:\/\//.test(line)) continue;
    const pair = splitPair(line);
    if (pair) {
      let a = pair[0].replace(LABEL, "").trim();
      const t = tidyTitle(pair[1]);
      if (words(a).length > 3) a = a.replace(/^.*\b(?:at|is|by|from|of|spin|playing|played)\s+/i, "").trim();
      if (t && words(a).length <= 8 && words(t).length <= 14) return [a, t];
    }
  }
  const m = text.match(QUOTED);
  return m ? ["", tidyTitle(m[1])] : null;
}

export function commentAnswers(comments, duration) {
  const kids = {};
  for (const c of comments) if (c.parent !== "root") (kids[c.parent] ||= []).push(c);
  const found = [];
  for (const c of comments) {
    if (c.parent !== "root") continue;
    const stamps = c.text.match(TS_ANY) || [];
    if (stamps.length !== 1) continue;
    const at = tsToSec(stamps[0]);
    if (duration && at > duration) continue;
    let own = nameIn(c.text);
    if (own && (own[0] || SELF_ID.test(c.text))) {
      let artist = own[0] || c.author.replace(/^@|(official|oficial|music)$/gi, "").trim();
      let title = own[1];
      const c1 = title.match(/\s+(?:w\/|with|together with|feat\.?|ft\.?)\s+(.+)$/i);
      const c2 = c1 ? null : c.text.match(/["”’']\s*(?:w\/|with|together with)\s+([A-Z][\w.]*(?:\s[A-Z][\w.]*)*)/);
      if (c1) { title = title.slice(0, c1.index).trim(); artist = `${artist}, ${c1[1].trim()}`; }
      else if (c2) artist = `${artist}, ${c2[1].trim()}`;
      own = [artist, title];
      found.push({seconds: at, artist: own[0], title: own[1], votes: 1 + c.likes,
        by_artist: SELF_ID.test(c.text), evidence: c.text.slice(0, 160)});
    }
    if (ASK.test(c.text) || !own) {
      for (const k of kids[c.id] || []) {
        const ans = nameIn(k.text);
        if (ans && ans[0]) found.push({seconds: at, artist: ans[0], title: ans[1], votes: 1 + k.likes,
          by_artist: false, evidence: `${c.text.slice(0, 80)} -> ${k.text.slice(0, 80)}`});
      }
    }
  }
  const merged = [];
  for (const f of found.sort((a, b) => a.seconds - b.seconds)) {
    const key = f.title.toLowerCase().split(/[(\[]/)[0].replace(/[^a-z0-9]/g, "");
    const hit = merged.find(m => Math.abs(m.seconds - f.seconds) <= 180 && seqRatio(m._key, key) >= 0.8);
    if (hit) {
      hit.votes += f.votes; hit.by_artist ||= f.by_artist; hit.mentions += 1;
      if (!hit.artist && f.artist) hit.artist = f.artist;
    } else merged.push({...f, _key: key, mentions: 1});
  }
  for (const m of merged) { delete m._key; m.time = secToTs(m.seconds); }
  return merged;
}

export function applyAnswers(tracks, answers, duration) {
  const fills = {};
  tracks.forEach((t, i) => {
    if (!t.is_id || t.seconds == null) return;
    const end = i + 1 < tracks.length && tracks[i + 1].seconds ? tracks[i + 1].seconds : (duration || 1e9);
    const inside = answers.filter(a => t.seconds - 30 <= a.seconds && a.seconds < end);
    if (inside.length) fills[i] = inside.reduce((b, a) =>
      (a.by_artist > b.by_artist || (a.by_artist === b.by_artist && a.votes > b.votes)) ? a : b);
  });
  return fills;
}

// ---------- the whole pipeline, given a source's info + a way to get comments ----------
export async function extract(info, getComments) {
  const meta = {};
  for (const k of ["url", "title", "uploader", "duration", "thumbnail", "platform", "upload_date", "id"]) meta[k] = info[k] ?? null;
  meta.duration_ts = secToTs(info.duration);
  meta.via = info.via || "YouTube API";
  const desc = info.description || "";
  const links1001 = [...new Set(desc.match(TL1001) || [])].sort();

  let raw = fromChapters(info.chapters), source = "chapters";
  if (raw.length < 3) { raw = bestFromText(desc); source = "description"; }

  let note = null, answers = [], comments = null;
  const needComments = raw.length < 3 && meta.platform === "Youtube";
  if (needComments) {
    try {
      comments = await getComments();
      let best = [];
      for (const c of comments) if (c.parent === "root") { const cand = bestFromText(c.text); if (cand.length > best.length) best = cand; }
      if (best.length >= 3) { raw = best; source = "comments"; }
      else {
        answers = commentAnswers(comments, meta.duration);
        note = answers.length ? "No full tracklist yet. These are the tracks people named in the comments."
                              : "Checked the comments, no tracklist there yet.";
      }
    } catch (e) { note = `Could not read comments (${e.message}).`; }
  }

  let tracks = [], order = null;
  if (raw.length) [tracks, order] = buildTracks(raw, source);
  else if (answers.length) {
    [tracks] = buildTracks(answers.map(a => [a.seconds, a.artist ? `${a.artist} - ${a.title}` : a.title]), "answers");
    order = "artist_first"; source = "answers";
  }
  tracks.forEach((t, i) => {
    const nxt = i + 1 < tracks.length ? tracks[i + 1].seconds : meta.duration;
    if (t.seconds != null && nxt) t.length = Math.max(0, Math.floor(nxt - t.seconds));
  });
  return {set: meta, tracks, source: tracks.length ? source : null, order, links_1001: links1001, note,
    identified: tracks.filter(t => !t.is_id).length, _comments: comments};
}

// Fill ID slots from the replies. On the public site this runs inside the same request,
// so the page gets one answer instead of asking twice.
export async function fillFromComments(result, getComments) {
  const ids = result.tracks.some(t => t.is_id && t.seconds != null);
  if (!ids || result.set.platform !== "Youtube") return {filled: 0, comments_read: 0};
  const comments = result._comments || await getComments();
  const fills = applyAnswers(result.tracks, commentAnswers(comments, result.set.duration), result.set.duration);
  let n = 0;
  for (const [i, a] of Object.entries(fills)) {
    Object.assign(result.tracks[+i], {artist: a.artist, title: a.title, is_id: false, source: "community",
      confidence: "community", evidence: `${a.by_artist ? "Posted by the artist. " : ""}${a.evidence}`});
    n++;
  }
  result.identified = result.tracks.filter(t => !t.is_id).length;
  return {filled: n, comments_read: comments.length, fills};
}
