// JavaScript twin of youtube_api.py. Same output shapes as the Python sources.
import {TS_ANY, ASK} from "./tracklist.js";

const API = "https://www.googleapis.com/youtube/v3/";
const MAX_THREAD_PAGES = 3;
const MAX_REPLY_FETCHES = 30;
const YT_ID = /(?:youtube\.com\/(?:watch\?(?:.*&)?v=|live\/|shorts\/|embed\/)|youtu\.be\/)([A-Za-z0-9_-]{11})/;

export const youtubeId = url => (String(url || "").match(YT_ID) || [])[1] || null;

function isoDuration(s) {
  const m = String(s || "").match(/^P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$/);
  if (!m) return null;
  const [d, h, mi, se] = m.slice(1).map(x => parseInt(x || "0", 10));
  return d * 86400 + h * 3600 + mi * 60 + se;
}

export class YouTube {
  constructor(key) { this.key = key; this.units = 0; }

  async get(endpoint, params) {
    this.units++;
    const r = await fetch(API + endpoint + "?" + new URLSearchParams({...params, key: this.key}));
    const j = await r.json().catch(() => ({}));
    if (r.ok) return j;
    const reason = ((j.error || {}).errors || [{}])[0].reason || "";
    if (reason === "commentsDisabled") return {items: []};
    if (reason === "quotaExceeded" || reason === "dailyLimitExceeded")
      throw new Error("YouTube API daily quota used up, resets at midnight Pacific");
    throw new Error(`YouTube API: ${(j.error || {}).message || reason || r.status}`);
  }

  async info(url) {
    const vid = youtubeId(url);
    const items = (await this.get("videos", {part: "snippet,contentDetails", id: vid})).items || [];
    if (!items.length) throw new Error("That video is private, removed, or not a YouTube video");
    const sn = items[0].snippet, cd = items[0].contentDetails, th = sn.thumbnails || {};
    return {url: `https://www.youtube.com/watch?v=${vid}`, title: sn.title, uploader: sn.channelTitle,
      duration: isoDuration(cd.duration), thumbnail: (th.maxres || th.high || th.medium || {}).url || null,
      platform: "Youtube", upload_date: (sn.publishedAt || "").slice(0, 10).replaceAll("-", ""), id: vid,
      description: sn.description || "", chapters: null};
  }

  async comments(url) {
    const vid = youtubeId(url);
    const out = [], needReplies = [];
    let token = null;
    for (let page = 0; page < MAX_THREAD_PAGES; page++) {
      const params = {part: "snippet,replies", videoId: vid, maxResults: 100, order: "relevance", textFormat: "plainText"};
      if (token) params.pageToken = token;
      const res = await this.get("commentThreads", params);
      for (const th of res.items || []) {
        const top = th.snippet.topLevelComment;
        out.push(norm(top, "root"));
        const got = (th.replies || {}).comments || [];
        for (const r of got) out.push(norm(r, top.id));
        const text = top.snippet.textOriginal || "";
        TS_ANY.lastIndex = 0;
        if ((th.snippet.totalReplyCount || 0) > got.length && text.match(TS_ANY) && ASK.test(text))
          needReplies.push([top.id, new Set(got.map(r => r.id))]);
      }
      token = res.nextPageToken;
      if (!token) break;
    }
    for (const [parent, have] of needReplies.slice(0, MAX_REPLY_FETCHES)) {
      const res = await this.get("comments", {part: "snippet", parentId: parent, maxResults: 100, textFormat: "plainText"});
      for (const r of res.items || []) if (!have.has(r.id)) out.push(norm(r, parent));
    }
    return out;
  }
}

function norm(c, parent) {
  const sn = c.snippet;
  return {id: c.id, parent, text: sn.textOriginal || sn.textDisplay || "", likes: sn.likeCount || 0,
    author: sn.authorDisplayName || "", is_uploader: (sn.authorChannelId || {}).value === sn.channelId};
}
