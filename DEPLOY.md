# Deploying Afterhours

The public site is **static files plus a few Cloudflare Pages Functions**. No server
runs anywhere. A GitHub Action rebuilds the feed every 6 hours and commits it, and
Cloudflare redeploys on every push. Visitors cost nothing, and nothing downloads audio.

| Piece | Where it runs | What it does |
|---|---|---|
| `site/` | Cloudflare Pages (static) | The page, plus the baked feed in `site/data/` |
| `functions/api/` | Pages Functions, or `worker.js` on a Worker project | Paste-a-link lookups, Deezer matches, artist pictures |
| `build_feed.py` | GitHub Actions, every 6 hours | Reads new sets from `channels.json`, re-reads recent ones for new IDs |

The local app (`server.py`, version finder, audio scan) is unchanged and never deployed.

---

## One-time setup (about 15 minutes, all in the browser)

### 1. Push this folder to the `afterhours` repo
Claude does this with your go-ahead. `config.json` (your key), `cache/` and `.venv/` are
gitignored and stay on your Mac.

### 2. Give the GitHub Action the YouTube key
github.com/zaneroberge04-hue/afterhours → **Settings → Secrets and variables → Actions →
New repository secret**. Name `YOUTUBE_API_KEY`, value: your key.

Then **Actions → feed → Run workflow** once to check it works.

### 3. Create the Cloudflare Pages project
dash.cloudflare.com → **Workers & Pages → Create → Pages → Connect to Git** → pick
`afterhours`. Build settings:

| Setting | Value |
|---|---|
| Framework preset | None |
| Build command | *(leave empty)* |
| Build output directory | `site` |
| Root directory | *(leave empty)* |

Cloudflare finds `functions/` on its own.

### 4. Give the site the YouTube key
In the new project: **Settings → Variables and Secrets → Add**. Type **Secret**, name
`YOUTUBE_API_KEY`, value: your key. Add it for **Production** (and Preview, if you want
preview links to work). Then **Deployments → Retry deployment** so it picks the key up.

### 5. Your domain
**Custom domains → Set up a custom domain** → `afterhours.zaneroberge.org`. Cloudflare
adds the DNS record itself.

---

## Running the public version on your Mac

```bash
cd afterhours
echo 'YOUTUBE_API_KEY=your-key' > .dev.vars      # gitignored
npx wrangler pages dev site                       # http://localhost:8788
```

## Quota

The free YouTube quota is 10,000 units a day. A feed run uses roughly 30 to 150 units,
four runs a day. A pasted link costs 1 to 35 units and is cached at Cloudflare's edge for
6 hours, so the same set is fetched at most four times a day however many people open it.

## Changing what the feed follows

Edit `channels.json`. Channel ids start with `UC`. The next run picks the change up.

## Keeping the two parsers in step

`tracklist.py` (feed, local app) and `functions/_lib/tracklist.js` (paste-a-link) are
twins. After changing either:

```bash
.venv/bin/python tests/make_fixtures.py   # needs the key in config.json
node tests/parity.mjs                     # must print "all checks match"
```
