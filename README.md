# Afterhours

Track ID and show finder for house music fans. Paste a set link, get every song with previews, follow the artists, see their shows near you.

## Two versions, one codebase

- **Public site** (`site/` + `functions/`): Cloudflare Pages. A self-updating feed of house sets with tracklists, charts, and paste-a-link lookups through the official YouTube API. See [DEPLOY.md](DEPLOY.md).
- **Local app** (`server.py`): everything above plus the audio features (Which version?, fingerprint scanning), SoundCloud links, and shows. Runs on your Mac only.

```
site/index.html          the page, shared by both versions
site/data/               the baked feed (rebuilt by .github/workflows/feed.yml)
functions/               Cloudflare Pages Functions, JS twins of the Python parsers
build_feed.py            feed builder, run by the GitHub Action
channels.json            channels the feed follows
tracklist.py             tracklist + comment-answer parser (twin: functions/_lib/tracklist.js)
tests/parity.mjs         proves the two parsers agree
```

## Run

```
.venv/bin/python server.py     # http://localhost:5240
```

First-time setup: `python3.12 -m venv .venv && .venv/bin/pip install yt-dlp imageio-ffmpeg numpy scipy`

## Local mode vs public mode

| | Local (default) | Public (`"public_mode": true`) |
|---|---|---|
| YouTube data | yt-dlp, or the official API if `youtube_api_key` is set | Official YouTube Data API only |
| SoundCloud / Mixcloud links | yes (yt-dlp) | not yet |
| Which version? and audio scanning | yes | off, nothing downloads audio |
| Comment IDs, previews, shows, follows | yes | yes |

Public mode is what a hosted site may run. YouTube API key: Google Cloud console, enable "YouTube Data API v3", create an API key, add `"youtube_api_key"` to `config.json`. A set costs about 5 to 35 of the 10,000 free daily units.

## IDs named in the comments

`tracklist.comment_answers` reads replies, not just top comments: "13:40 ID???" answered with "Miro (AR) - watching me", or a producer posting "1:09:00 is my unreleased track 'Give Me The Rythm'". Answers near the same time are merged (fuzzy, so Rythm and Rhythm count once) and weighted by likes, and the artist's own comment wins. They fill a tracklist's ID slots, or become the tracklist when nobody posted one. On Cloonee B2B Prospa at Destino it named 6 tracks from replies alone, all matching the full tracklist.

## How track ID works

`tracklist.py` reads the set with yt-dlp, then looks for the tracklist in chapters, timestamped description lines, untimed or numbered lists, and finally the top YouTube comments. Lines are split into artist and title, with a set-wide vote on order (some uploaders write Title - Artist). Each track is matched on Deezer for cover art and a 30 second preview.

## Shows

Needs at least one free key in `config.json` (gitignored):

```json
{"edmtrain_key": "...", "ticketmaster_key": "..."}
```

- EDMTrain: https://edmtrain.com/api-documentation (read their API terms before going public, they likely require crediting EDMTrain where their data appears)
- Ticketmaster Discovery: https://developer.ticketmaster.com/

## Fingerprinting

For sets with no written tracklist, or to fill the IDs in one that has gaps. `fingerprint.py` downloads the audio, cuts 12 second samples with a bundled ffmpeg (imageio-ffmpeg, no Homebrew needed), sends each to a recognizer, and merges back-to-back hits into tracks. Songs heard in only one sample get a "heard once" flag. The audio is deleted when the job ends.

Add one provider to `config.json` and restart:

- AudD: `{"audd_token": "..."}` from https://dashboard.audd.io
- ACRCloud: `{"acr_host": "...", "acr_key": "...", "acr_secret": "..."}` from https://console.acrcloud.com

Every sample counts as one lookup against your plan on either service, so check their pricing first. A one hour set is about 60 lookups at the default 60 second interval. Filling IDs costs 2 lookups per ID. `{"fingerprint_mock": true}` runs the pipeline with fake hits for testing.

## Which version?

Tracklists name the base song, but DJs often play a remix, edit, or bootleg of it. "Which version?" on any timed track:

1. Pulls a 30 to 90 second slice from the middle of that track in the set (skipping the mix in and mix out)
2. Searches Deezer, YouTube, and SoundCloud for every version of the song (originals, extended mixes, remixes, edits, bootlegs)
3. Fingerprints the slice locally (`remix_match.py`, spectrogram peak pairs) at 33 tempo settings, key-locked and vinyl-style, so a record played 2% slow still lines up
4. Ranks each version by how many fingerprint points line up, and how much of the slice they cover

The coverage check is what separates the right record from a rework built on the same vocal. If nothing covers the slice, the verdict is "built on X, but not a version that is online," which usually means an unreleased edit.

Free, no API keys. About 15 seconds per track the first time, faster once the set audio is cached (kept 3 hours). Tested on 28 tracks (3 from a disco house mix, all 25 from Cloonee B2B Prospa at Destino). Thresholds in `versions.py` will need more tuning as more sets get checked.

## Not built yet

- Accounts and sync. Follows and crates live in the browser's localStorage for now
- Real Spotify links (currently search links) and playlist export
