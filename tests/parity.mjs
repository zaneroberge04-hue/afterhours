// Runs the JS parser on the saved fixtures and compares with what Python produced.
//   node tests/parity.mjs
import {readdirSync, readFileSync} from "node:fs";
import {extract, commentAnswers, applyAnswers, buildTracks, bestFromText} from "../functions/_lib/tracklist.js";

const dir = new URL("./fixtures/", import.meta.url);
let bad = 0, n = 0;
for (const s of JSON.parse(readFileSync(new URL("_texts.json", dir)))) {
  n++;
  const [tracks, order] = buildTracks(bestFromText(s.text), "description");
  if (JSON.stringify([tracks, order]) !== JSON.stringify([s.tracks, s.order])) {
    bad++; console.log("DIFF text sample:", s.text.slice(0, 60));
    console.log("  js:", JSON.stringify(tracks).slice(0, 300)); console.log("  py:", JSON.stringify(s.tracks).slice(0, 300));
  }
}
for (const f of readdirSync(dir).filter(f => f.endsWith(".json") && !f.startsWith("_"))) {
  const {info, comments, expected} = JSON.parse(readFileSync(new URL(f, dir)));
  const r = await extract(info, async () => comments);
  delete r._comments;
  const answers = commentAnswers(comments, info.duration);
  const fills = applyAnswers(r.tracks, answers, info.duration);
  for (const [name, got, want] of [["result", r, expected.result], ["answers", answers, expected.answers],
                                   ["fills", fills, expected.fills]]) {
    n++;
    if (JSON.stringify(got) === JSON.stringify(want)) continue;
    bad++;
    console.log(`DIFF ${f} ${name}`);
    const ga = got.tracks || got, wa = want.tracks || want;
    if (Array.isArray(ga)) {
      for (let i = 0; i < Math.max(ga.length, wa.length); i++)
        if (JSON.stringify(ga[i]) !== JSON.stringify(wa[i])) { console.log("  js:", JSON.stringify(ga[i])); console.log("  py:", JSON.stringify(wa[i])); break; }
      if (got.tracks) for (const k of Object.keys(want)) if (k !== "tracks" && JSON.stringify(got[k]) !== JSON.stringify(want[k]))
        console.log(`  field ${k}: js=${JSON.stringify(got[k])} py=${JSON.stringify(want[k])}`);
    } else { console.log("  js:", JSON.stringify(got).slice(0, 300)); console.log("  py:", JSON.stringify(want).slice(0, 300)); }
  }
  console.log(`${f}: ${r.source} ${r.tracks.length} tracks, ${answers.length} answers`);
}
console.log(bad ? `${bad} of ${n} checks differ` : `all ${n} checks match`);
process.exit(bad ? 1 : 0);
