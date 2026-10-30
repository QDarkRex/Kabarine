# AGENTS.md — developer handoff & context

Self-hosted Discord bot: forwards **TikTok** + **Instagram** media into Discord, both
on-demand and via account monitoring, plus on-demand **X** downloads. Python + discord.py,
one Docker container. Engines: **yt-dlp** (TikTok videos), **gallery-dl** (Instagram,
TikTok story listing) and the public **fxtwitter** API (X).

This file is the single source of truth for anyone (human or AI) picking up the code.
Run the tests (`pytest`, see [§10](#10-local-dev--testing-patterns)) before and after a change.

---

## 1. Architecture / file map

```
bot.py                 entry: load .env, resolve cookies/burners/proxies, load cogs, sync slash cmds
configs.yml            runtime settings (MOUNTED into container -> `docker compose restart` applies; no rebuild)
compose.yml            container `tiktokbot`, restart:unless-stopped, mem/cpu/pids caps, mounts ./data + configs.yml
Dockerfile             python:3.11-slim + ffmpeg + gosu; runs non-root (uid 1000 appuser)
src/
  config.py            DEFAULTS + load_config() (merges configs.yml over defaults)
  log.py               stdout logging
  db.py                aiosqlite; `subscriptions` table; migration from old `accounts` table
  tiktok.py            yt-dlp wrappers: find_links, normalize_username, list_recent, download, _video_format
  instagram.py         gallery-dl wrappers: find_links, normalize_username, list_recent, download,
                       list_stories, download_stories
  discord_post.py      TikTok posting: handle_url -> download -> send_video (ffmpeg-compress if > cap)
  instagram_post.py    IG posting: post_media (shared upload/compress/batch) + handle_url (download then post_media)
  monitor.py           shared POST forwarder: forward_new(...) diff recent vs last_seen, ping tag, forward oldest-first
  mentions.py          parse Role/Member/@everyone slash param -> (type,id); mention_string / announce() (pings)
  discord_utils.py     suppress_link_embeds (removes Discord's auto preview embed; needs Manage Messages)
  tiktok_story.py      TikTok story listing via gallery-dl + numeric-id/secUid resolution (yt-dlp)
  twitter.py           X/Twitter: find_links, fetch (fxtwitter API), download best mp4 / original photos
  health.py            HealthMonitor: owner DM alerts (stalled/failing pollers, failed forwards, silence)
                       + data/health/heartbeat.json for the outside watchdog
cogs/
  tracking.py          /tt group (add/remove/list/get) + TikTok video poller (~2.5 min) + story poller (~10 min)
  instagram.py         /ig group (get/add/remove/list) + IG poller (slow, per-burner sharded) + _forward_stories
  ondemand.py          TikTok paste auto-detect listener
  twitter.py           /x get + X paste auto-detect (posts with media only)
tests/                 pytest suite (fake Discord, temp SQLite, canned extractor output; no network)
watchdog/              tiny stdlib-only container that alerts when a bot's heartbeat goes stale
```

Three slash groups: `/tt`, `/ig`, `/x`. The token lives in the gitignored `.env`.

---

## 2. Data model (`src/db.py`)

Table `subscriptions`, PK `(platform, username, channel_id, content_type)`:

| col | meaning |
|---|---|
| platform | `tiktok` \| `instagram` |
| username | account handle (normalized, no `@`) |
| channel_id | Discord channel OR thread id (string) |
| guild_id | Discord guild id |
| content_type | `post` \| `story` (story = Instagram only) |
| mention_type | `role` \| `user` \| `everyone` \| NULL |
| mention_id | role/user id (NULL for everyone/none) |
| last_seen_id | newest item id already forwarded for this sub (the dedup marker) |
| enabled | 1/0 |

`init_db()` migrates the legacy TikTok-only `accounts` table into `subscriptions` (INSERT OR IGNORE,
idempotent). `add_subscription` is INSERT OR REPLACE, so re-adding an account updates its mention in place.
All monitoring state lives in `data/tiktok.db` → **migrating servers = copy the `~/tiktokbot` folder incl `data/`.**

---

## 3. Environment (`.env`)

```
BOT_TOKEN=...                     # Discord bot token
DATA_PATH=./data                  # leave as-is
TIKTOK_COOKIES=                   # optional (TikTok works anonymously for public accounts)
TIKTOK_PROXY=                     # optional
IG_COOKIES=/bot/data/ig_cookies.txt              # REQUIRED for IG (login wall). Comma-separate for
                                                 #   multiple burners: c1.txt,c2.txt
IG_PROXIES=                       # comma-separated, paired to burners by position (or single IG_PROXY)
```

**Instagram needs a logged-in (burner) cookies.txt** — the profile/post/story endpoints redirect to the
login page when logged out. Monitoring is **sharded across burners**: each account is pinned to a burner by
`md5(username) % n`, and each burner runs its own poller (own cookie/proxy/backoff). Currently running
**1 burner, no proxy** (playtest, ~10 accounts). `data/ig_cookies2.txt` is present for a future 2nd burner.

---

## 4. configs.yml keys

TikTok poller: `sweep_target_seconds` (150), `min_request_spacing` (1.5), `request_jitter` (0.5),
`playlist_scan_count` (5).
IG poller (per burner): `ig_sweep_target_seconds` (900), `ig_min_request_spacing` (20),
`ig_request_jitter` (5), `ig_playlist_scan_count` (3).
On-demand: `auto_detect_links` (true), `suppress_link_embeds` (true).
IG download: `download_timeout` (180), `max_files_per_post` (20).
Video: `tiktok_native` (true = highest res even if h265; false = force h264/inline), `max_upload_mb` (50),
`compress_oversize` (true), `compress_timeout` (120).

---

## 5. How monitoring works

- **TikTok posts** (`cogs/tracking.py`): poller sweeps all tracked accounts, `tiktok.list_recent` (flat
  playlist, cheap), `monitor.forward_new` diffs vs `last_seen_id`, forwards new videos oldest-first via
  `discord_post.handle_url`. Native res: `_video_format(native=True)` picks highest res (TikTok 1080p is
  often h265-only; forcing h264 capped it at 720p — that was the fix in commit "fetch native resolution").
- **IG posts** (`cogs/instagram.py`): per-burner poller, `instagram.list_recent` (gallery-dl `-j` on
  `instagram.com/<user>/posts/` — the bare profile only *queues*, so `/posts/` is required), dedup by
  `post_shortcode`, forward via `monitor.forward_new` -> `instagram_post.handle_url`.
- **Tagging**: `/tt add` / `/ig add` accept `tag:` (role/person); `monitor.forward_new` and `_forward_stories`
  call `mentions.announce()` before each forwarded item.

---

## 6. How IG stories work (the feature under investigation)

The poller pulls the **whole current set** and works out which items are new. (An old note here said
`stories/<user>/<media_id>/` returns 404. Re-tested 2026-10-30 on gallery-dl 1.32.18 with the burner:
that link downloads exactly that one item, so pasted/`/ig get` story links go through the normal
`instagram.download` path — see `is_story_url`. `/stories/me/` is refused: it's the burner's whole tray.)
The poller:

- `instagram.list_stories(user)` — `gallery-dl -j --no-download` on `instagram.com/stories/<user>/`,
  returns `[{"id": media_id}, ...]` newest-first (from the `media_id` field). Cheap check.
- `instagram.download_stories(user)` — downloads the whole current set with `--write-metadata`; returns
  `{"dir", "items": [{"id", "path"}, ...]}` newest-first. `id` is read from the sidecar JSON
  `media_id`/`id`, with filename digits as a fallback.
- `cogs/instagram.Instagram._forward_stories(username, subs, cookies, proxy)`:
  1. `list_stories` → `current_ids` (newest-first).
  2. Per subscription: if `last_seen is None` → seed baseline = newest, continue (never dumps existing).
     Else use `current_ids` only as the cheap "something is newer" gate.
  3. If any new, `download_stories` once and compute `new_items` from the downloaded set itself.
  4. For each downloaded new item oldest-first: `post_media([path], ...)`, advance `last_seen`.
- `/ig add ... type:stories|both` creates a `content_type='story'` subscription (baseline = newest current
  story id, or NULL if none active). Poller checks both post and story subs per account.

Verified locally against natgeo/cristiano/nasa: list ids == download ids, forwards all-new oldest-first,
0 re-sends once caught up, None baseline seeds without dumping.

---

## 7. Instagram stories debugging

**Historical symptom:** IG story monitoring "not working" — stories don't appear in Discord.

Ranked hypotheses, **most likely first** — check in this order:

### H1 — Subscription is posts-only (most likely, not a code bug)
`/ig add` defaults to `type:posts`. If the user didn't pass `type:stories` or `type:both`, no story
subscription row exists, so nothing is polled.
- **Check:** `/ig list` shows `(posts)` vs `(stories)` per row. Or SQL:
  `SELECT username, content_type FROM subscriptions WHERE platform='instagram';`
- **Fix:** re-run `/ig add username:<acct> channel:#chan type:stories` (or `both`).

### H2 — Baseline means only NEW stories after add-time forward (expected behavior)
On add, `last_seen_id` is seeded to the newest current story id, so stories that already existed are NOT
forwarded — only ones posted *after* subscribing. If the account posted nothing new since, nothing forwards.
- **Verify test:** set a story sub's `last_seen_id` to an old value and watch the next sweep forward current
  stories: `UPDATE subscriptions SET last_seen_id='1' WHERE platform='instagram' AND content_type='story' AND username='<acct>';`
- Also: stories expire in 24h; the account may simply have had none during the poll window, or restricts
  visibility (close-friends/hidden), or is private and the burner doesn't follow it.

### H3 — id mismatch between list_stories and download_stories (fixed)
Original bug: `_forward_stories` computed `new_ids` from `list_stories` (`media_id` field) but then looked up
paths from `download_stories` by filename-derived IDs. If those ID sources differed, the story was skipped
and the marker advanced past it.

Current fix:
- `download_stories()` prefers the sidecar `.json` `media_id`/`id`, falling back to filename digits only if
  metadata is missing.
- `_forward_stories()` uses `list_stories()` only as the cheap "something is newer" gate, then computes
  `new_items` from `download_stories()["items"]` itself and posts those directly.
- If the cheap list says something is new but the downloaded set has no matching newer item, the bot logs
  both ID sets and leaves `last_seen_id` unchanged for retry/diagnosis.

### H4 — Story listing/downloading failing on the server (cookies/rate-limit)
- **Check:** `docker compose logs tiktokbot | grep -iE 'story|couldn.t list'`. The poller logs
  `ig poll: story check failed @X (...)` on exceptions. If cookies are stale, both posts and stories fail.
- Confirm the burner cookie in `data/ig_cookies.txt` is still valid (re-export if needed).

### H5 — Poll timing
Story subs are checked on the same ~15-min IG sweep. Not instant after `/ig add`. Confirm enough time passed.

**Quick triage command:** `docker compose logs --since 2h tiktokbot | grep -iE 'story|instagram poller|couldn'`
should show the story poller running and whether forwards/failures occurred.

---

## 8. Known limitations / decisions

- **TikTok stories: monitored through gallery-dl, not yt-dlp** (`/tt add type:stories|both`, 2026-10-25).
  yt-dlp still has no TikTok story extractor; gallery-dl's `TiktokStoriesExtractor` lists them. From the
  server its profile-page lookup is answered **403**, so `src/tiktok_story.py` resolves each account's
  **numeric user id once via yt-dlp** (cached in the `tiktok_user_ids` table) and lists
  `tiktok.com/@<numeric id>/stories`, which gallery-dl serves without touching the profile page. "No active
  story" makes gallery-dl's cursor crash ("Could not extract next cursor") — read as an empty list. Story
  markers are compared **numerically** (`monitor.forward_new(by_id=True)`), because the marked story expires
  after 24h and the walk-until-marker logic used for videos would then re-send every story still up.
  Anonymous; no browser. Depends on an undocumented TikTok endpoint and gallery-dl internals — which is
  why yt-dlp and gallery-dl are **pinned** in `requirements.txt`; upgrade deliberately (the health alert
  names newer versions when most accounts start failing). The earlier Playwright watcher (`f58e45a`) was
  reverted (`7271560`) and is superseded.
- **Native video codec trade-off:** TikTok native 1080p and IG native reels are often **h265 / VP9**, which
  may not inline-preview on some Discord clients (shows as a downloadable attachment). Chosen deliberately for
  true native quality. `tiktok_native: false` reverts TikTok to h264 (inline, up to 720p). IG uses gallery-dl
  default (`videos=merged` is NOT set → DASH → native VP9 merged via yt-dlp+ffmpeg).
- **Instagram is polled slowly on purpose** (~15–30 min) — IG flags aggressive automation. Sharding across
  burners with a **separate IP (proxy) each** is the way to scale (same IP for 2 burners = co-ban risk). ISP
  (static residential) proxies recommended. Measured IG listing ≈ 77 KB/check.
- **Discord specifics:** file attachments are stored uncompressed (only the inline preview is downscaled);
  suppress-embeds needs Manage Messages; ≤10 attachments per message.

---

## 9. Deployment

See [docs/deploy.md](docs/deploy.md). Container `tiktokbot`, `data/` bind-mounted.
- **Code change** → rebuild: `docker compose up -d --build`.
- **configs.yml-only change** → `docker compose restart`. Keep your server's `configs.yml` when updating
  the code (it holds your settings; missing keys fall back to `src/config.py` DEFAULTS).
- `data/` is owned by the container's uid 1000, so the SSH user can't write into it directly — place files
  (e.g. cookies) via a throwaway root container:
  `docker run --rm -v $PWD/data:/data -v ~/file:/f:ro --entrypoint cp <image> /f /data/file`.
- Health check: the bot DMs its owner "✅ Media bot running" on start; the logs show `tiktok poller alive`,
  `tiktok story poller alive`, `... is online`, `synced 3 slash command(s)`.

---

## 10. Local dev / testing patterns

- `python -m venv .venv`; `.venv/Scripts/pip install -r requirements.txt pytest`.
- Tests: `.venv/Scripts/python -m pytest -q tests` — no network, no Discord; each file also runs on its own
  (`python tests/test_stories.py`). Pollers take a `bot` with a `health` attribute — use
  `tests/fakes.FakeHealth`.
- Wrappers are subprocess-based and testable directly, e.g.:
  `python -c "from src import instagram; print(instagram.list_stories('natgeo', cookies='data/ig_cookies.txt'))"`
- gallery-dl is invoked via `python -m gallery_dl` (PATH-independent). ffmpeg/ffprobe are needed for
  IG DASH merge + oversize compression (present in the image; install locally to test video paths).
- Story forwarder can be unit-tested with a fake channel (record `send()` calls) + a temp sqlite db; craft an
  old `last_seen` to force-forward, then a current one to assert 0 re-sends.

---

## 11. gotchas cheat-sheet
- **TikTok photo posts**: yt-dlp can't download them (`/photo/` = Unsupported URL, `/video/<id>`
  of a photo post = "No video formats found"). `tiktok.download_photos()` pulls the image list
  from yt-dlp's `TikTokIE._extract_web_data_and_status` (PRIVATE method — re-test after any
  yt-dlp upgrade; tikwm API is the fallback). Profile listing also works via
  `tiktokuser:<secUid>` when `@user` listing fails with "Unable to extract secondary user ID".
- **LIVE links must never reach the downloaders.** A live room never ends: yt-dlp would
  stream it forever, the `asyncio.to_thread` worker can't be cancelled from outside, the
  bot sits on "typing..." and the file fills the disk. Guards, in order:
  `tiktok.is_downloadable_url()` / `instagram.is_downloadable_url()` (whitelist: only
  single `/video/`,`/photo/`,`/p/`,`/reel/`,`/tv/` + TikTok short links) → an `is_live`
  probe inside `tiktok.download()` → a progress hook enforcing `tiktok_download_timeout`
  and `max_download_mb` from INSIDE the thread. Never widen the paste listeners back to
  raw `find_links()` — a pasted **profile** link also made gallery-dl crawl the whole
  account until it timed out.
- **IG `/posts/` lists PINNED posts first** (not newest). `list_recent` fetches `count+3` and sorts by
  `date` so `recent[0]` is the true newest — `monitor.forward_new` depends on that ordering. Don't revert to
  raw grid order or forwarding silently stops (a pinned post at index 0 == last_seen → breaks immediately).
- gallery-dl bare IG profile only *queues* → use `/posts/` and `/stories/`.
- gallery-dl `--directory` (flat) NOT `--destination` (adds `instagram/<user>/` subdirs).
- IG single-post/story links need cookies (login redirect when logged out).
- TikTok 1080p is h265-only; h264 caps at 720p.
- gallery-dl warns "lowered video quality" for non-Chrome UA → a desktop Chrome UA is pinned.

---

## 12. Health alerts & watchdog

- `src/health.py` (`bot.health`): pollers call `register(feed)` / `sweep(feed, ok, total)`;
  `monitor.forward_new` and the IG story forwarder call `forwarded()` / `forward_failed()`. Every 5 min it
  checks: stalled poller (`health_stall_minutes`), `health_fail_sweeps` sweeps in a row with ≥
  `health_fail_ratio` unreadable accounts (alert names installed vs PyPI yt-dlp/gallery-dl), a streak of
  `health_forward_failures` failed forwards, and nothing forwarded for `health_quiet_hours`. One alert per
  problem, repeated every `health_repeat_hours`, plus recovery. Wording: `alert_language` (en/id).
  Also: less than `health_disk_free_gb` free on DATA_PATH's disk, and every check deletes anything in
  `data/downloads` older than 2 h (`sweep_downloads`; bot start wipes it completely). Downloads are temp
  files; 1.3 GB of interrupted ones (a 68-min TikTok, a 4K X clip, 48-byte `_c.mp4` ffmpeg stubs) piled up
  unnoticed Jul-Oct 2026 before this existed.
- Each check writes `data/health/heartbeat.json`. `watchdog/` runs separately and alerts when that file
  goes stale — the only way to hear about a stopped/hung container, since the bot can't report its own death.
- Neither can see the whole host going down; that needs a check from another machine.
