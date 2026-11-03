// POST /api/set {url} -> tracklist, with ID slots already filled from comment replies.
import {extract, fillFromComments} from "../_lib/tracklist.js";
import {YouTube, youtubeId} from "../_lib/youtube.js";
import {json, cached} from "../_lib/http.js";

export async function onRequestPost({request, env}) {
  const {url} = await request.json().catch(() => ({}));
  if (!url || !String(url).startsWith("http")) return json({error: "Paste a full link to the set."}, 400);
  const vid = youtubeId(url);
  if (!vid) return json({error: "Only YouTube links are supported on the public site for now"}, 400);
  if (!env.YOUTUBE_API_KEY) return json({error: "The site is missing its YouTube API key"}, 500);
  try {
    const data = await cached(`set:${vid}`, 6 * 3600, async () => {
      const yt = new YouTube(env.YOUTUBE_API_KEY);
      const watch = `https://www.youtube.com/watch?v=${vid}`;
      let comments = null;
      const getComments = async () => (comments ||= await yt.comments(watch));
      const r = await extract(await yt.info(watch), getComments);
      const fill = await fillFromComments(r, getComments);
      delete r._comments;
      r.answered = {filled: fill.filled, comments_read: fill.comments_read};
      r.units = yt.units;
      return r;
    });
    return json(data);
  } catch (e) {
    return json({error: e.message}, 500);
  }
}
