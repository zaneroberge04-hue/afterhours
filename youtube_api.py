"""Official YouTube Data API v3 source, for the public site.

Same output shapes as tracklist.YtdlpSource. Costs per set, against the free
10,000 units a day: 1 (video) + up to 3 (comment pages) + 1 per ID question whose
replies did not all come back with the thread, capped at 30. So about 300+ sets a day.

Key: Google Cloud console, enable "YouTube Data API v3", create an API key,
put it in config.json as "youtube_api_key".
"""

import json
import re
import urllib.error
import urllib.parse
import urllib.request

import tracklist

API = "https://www.googleapis.com/youtube/v3/"
MAX_THREAD_PAGES = 3
MAX_REPLY_FETCHES = 30


def iso_duration(s):
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", s or "")
    if not m:
        return None
    d, h, mi, se = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + se


class Source:
    name = "YouTube API"

    def __init__(self, key):
        self.key = key

    def get(self, endpoint, **params):
        url = API + endpoint + "?" + urllib.parse.urlencode({**params, "key": self.key})
        try:
            with urllib.request.urlopen(url, timeout=15) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            try:
                err = json.loads(e.read().decode())["error"]
                reason = (err.get("errors") or [{}])[0].get("reason", "")
            except Exception:
                raise RuntimeError(f"YouTube API error {e.code}")
            if reason == "commentsDisabled":
                return {"items": []}
            if reason in ("quotaExceeded", "dailyLimitExceeded"):
                raise RuntimeError("YouTube API daily quota used up, resets at midnight Pacific")
            raise RuntimeError(f"YouTube API: {err.get('message', reason)}")

    def info(self, url):
        vid = tracklist.youtube_id(url)
        items = self.get("videos", part="snippet,contentDetails", id=vid).get("items") or []
        if not items:
            raise RuntimeError("That video is private, removed, or not a YouTube video")
        sn, cd = items[0]["snippet"], items[0]["contentDetails"]
        thumbs = sn.get("thumbnails") or {}
        thumb = (thumbs.get("maxres") or thumbs.get("high") or thumbs.get("medium") or {}).get("url")
        return {"url": f"https://www.youtube.com/watch?v={vid}", "title": sn.get("title"),
                "uploader": sn.get("channelTitle"), "duration": iso_duration(cd.get("duration")),
                "thumbnail": thumb, "platform": "Youtube",
                "upload_date": (sn.get("publishedAt") or "")[:10].replace("-", ""), "id": vid,
                "description": sn.get("description") or "",
                "chapters": None}   # the API has no chapters field; they come from description timestamps

    def comments(self, url):
        vid = tracklist.youtube_id(url)
        out, token, pages, needs_replies = [], None, 0, []
        while pages < MAX_THREAD_PAGES:
            params = {"part": "snippet,replies", "videoId": vid, "maxResults": 100, "order": "relevance",
                      "textFormat": "plainText"}
            if token:
                params["pageToken"] = token
            res = self.get("commentThreads", **params)
            for th in res.get("items") or []:
                top = th["snippet"]["topLevelComment"]
                out.append(self._norm(top, "root"))
                got = (th.get("replies") or {}).get("comments") or []
                out += [self._norm(r, top["id"]) for r in got]
                text = top["snippet"].get("textOriginal") or ""
                # The thread only carries a few replies; fetch the rest where someone asked for an ID
                if th["snippet"].get("totalReplyCount", 0) > len(got) and tracklist.TS_ANY.search(text) \
                        and tracklist.ASK.search(text):
                    needs_replies.append((top["id"], {r["id"] for r in got}))
            token, pages = res.get("nextPageToken"), pages + 1
            if not token:
                break
        for parent, have in needs_replies[:MAX_REPLY_FETCHES]:
            res = self.get("comments", part="snippet", parentId=parent, maxResults=100, textFormat="plainText")
            out += [self._norm(r, parent) for r in res.get("items") or [] if r["id"] not in have]
        return out

    @staticmethod
    def _norm(c, parent):
        sn = c["snippet"]
        return {"id": c["id"], "parent": parent, "text": sn.get("textOriginal") or sn.get("textDisplay") or "",
                "likes": sn.get("likeCount") or 0, "author": sn.get("authorDisplayName") or "",
                "is_uploader": sn.get("authorChannelId", {}).get("value") == sn.get("channelId")}
