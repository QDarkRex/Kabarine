# Changelog

Reverse-chronological summary of what shipped. See `git log` for full detail and
[`STATUS.md`](STATUS.md) for what is currently deployed vs. pending.

## 2026-10-30 — deployed from `deploy/ig-fixes-lean`

- **X / Twitter downloader** — `/x get <url>` and pasted links (only posts with media;
  `x_auto_detect_links`). Uses the public fxtwitter API: no login, original-resolution photos,
  highest-bitrate mp4. Posted through the shared `post_media` (batching, compression, link
  fallback).
- **TikTok items are always labelled** — a "🎬 TikTok Post" / "📖 TikTok Story" line goes above
  every forwarded item (`monitor.forward_new(headline=...)`), with the subscription's ping in
  front when it has one. yt-dlp's placeholder title "TikTok video #<id>" is no longer used as a
  caption — it made stories look like videos. Instagram is unchanged.
- **Removed subscriptions came back on restart (fix)** — `init_db` re-ran the old `accounts`
  migration on every start, and `INSERT OR IGNORE` re-created everything deleted since. It now
  runs once and renames the table to `accounts_migrated_backup`.

## 2026-10-25 — deployed from `deploy/ig-fixes-lean`

- **TikTok story auto-forward** — `/tt add type:stories|both`. yt-dlp can't list stories, but gallery-dl's
  TikTok stories extractor can; from the server its profile-page lookup is 403'd, so the numeric user id is
  resolved once via yt-dlp (cached in `tiktok_user_ids`) and `tiktok.com/@<id>/stories` is listed instead.
  Stories are downloaded through the existing yt-dlp path. Own slower poller (`tiktok_story_sweep_seconds`,
  default 600). Story markers compare ids numerically so an expired story can't trigger re-sends.
  Supersedes the Playwright watcher (`f58e45a`).
- `/tt remove` gained `type:` (default: videos and stories); `/tt list` shows each subscription's type and
  is split into several messages — with ~57 accounts it exceeded Discord's 2000-character limit.

## Unreleased on the server (pushed to `main`, not yet deployed)

- **TikTok photo/slideshow posts** — previously nothing was posted: yt-dlp rejects
  `/photo/<id>` as "Unsupported URL" and fails `/video/<id>` of a photo post with "No video
  formats found" (so monitoring silently skipped them too). `tiktok.download_photos()` now
  reads `imagePost.images` from yt-dlp's own TikTok extractor (`_extract_web_data_and_status`
  — private API, may break on a yt-dlp upgrade) with the public tikwm API as fallback, and
  downloads the full-res JPEGs. `discord_post.handle_url` routes `/photo/` links there up
  front and retries any "no video formats" failure (short links, monitored posts) as photos;
  images are posted via `post_media` (<=10 per message). Unknown codec is no longer treated
  as audio-only.

- **Live/profile links hung the bot (fix)** — both paste listeners fed *every* matched
  TikTok/IG URL straight to the downloader. A **live** link made yt-dlp stream the room
  forever (no overall timeout existed; `socket_timeout` never fires while data flows), so
  the bot stayed on "typing...", leaked the worker thread and filled the disk; a pasted
  **profile** link made gallery-dl crawl the entire account until the 180 s timeout. Now:
  `is_downloadable_url()` whitelists single posts only, `tiktok.download()` probes
  `is_live` before downloading, and a progress hook enforces `tiktok_download_timeout`
  (180 s) + `max_download_mb` (300) from inside the worker thread. `/tt get` and `/ig get`
  explain the refusal; the passive paste listener stays silent.

- **IG post forwarding broken by pinned posts (fix)** — `instagram.list_recent` returned `/posts/` in
  Instagram's grid order, which puts **pinned posts first**. Since `monitor.forward_new` treats `recent[0]`
  as the newest and stops scanning at the last-seen marker, a pinned post at index 0 made the poller think
  nothing was ever new — so **no IG posts were forwarded** for any account with a pin (most of them). Fix:
  `list_recent` now fetches `count + 3` posts (Instagram allows ≤3 pins) and sorts by actual post `date`,
  so it returns genuinely newest-first. After deploy, existing subs whose baseline was a pinned id do a
  one-time catch-up of up to `ig_playlist_scan_count` posts, then settle.
- **Experimental TikTok story watcher** (`f58e45a`) — yt-dlp can't list TikTok stories, so
  `src/tiktok_story.py` uses a logged-in Playwright/Chromium session to open a profile, click the
  avatar/story entry, and sniff `tiktok.com/@user/video/<id>` URLs from network responses. Opt-in
  (`tiktok_story_enabled`, default off) + `TIKTOK_STORY_COOKIES`. Separate slow poller in
  `cogs/tracking.py`. **Needs a `mem_limit` bump before enabling — see STATUS.md.**
- **Instagram story ID-mismatch fix** (`c07011c`) — `download_stories` now reads the item id from the
  metadata sidecar (`media_id`/`id`, filename-digits fallback), and `_forward_stories` computes new items
  from the downloaded set itself instead of intersecting list-vs-download ids (which could silently skip &
  advance past items).

## Deployed

- **`AGENTS.md`** handoff doc (`a211e5b`).
- **TikTok native resolution** (`bee6f9e`) — pick highest-res rendition, not h264-capped 720p (TikTok 1080p
  is often h265-only). `tiktok_native` config; h265 may not inline-preview (accepted trade-off).
- **Instagram story monitoring** (`40517d8`) — `list_stories`/`download_stories`; `/ig add type:posts|stories|both`;
  poller sweeps post + story subs; shared `instagram_post.post_media()`.
- **Auto-suppress link-preview embeds** — after reposting a pasted TikTok/IG link, strip Discord's redundant
  preview embed (`suppress_link_embeds`, needs Manage Messages).
- **Multi-burner IG sharding** (`89fc8bd`, `fe1b2ae`) — `IG_COOKIES` accepts several cookie files;
  `IG_PROXIES` pairs a proxy per burner; accounts sharded by `md5(username)`; one poller per burner.
- **Phase 1: IG post monitoring + tagging** (`f821bb7`) — unified platform-aware `subscriptions` table
  (migrated from the old TikTok-only `accounts` table); `/ig` monitoring group; `tag:` (role/person/@everyone)
  + thread targets on both `/tt` and `/ig`; shared `monitor.forward_new`.
- **Instagram on-demand `/ig`** — gallery-dl engine; native-resolution posts/reels/carousels; merged into the
  TikTok bot as one container.
- **TikTok bot base** — yt-dlp engine; `/tt` account monitoring (paced scheduler, adaptive backoff) + paste
  auto-detect; ffmpeg compression for oversize; non-root, resource-capped Docker.
