# discord-media-downloader

A small, self-hosted Discord bot that brings TikTok and Instagram media into your server:

- **TikTok** — monitor accounts and auto-forward their new videos to a channel/thread,
  **plus** paste-a-link on-demand downloads. Watermark-free, H.264 so Discord plays inline.
- **Instagram** — on-demand downloads (`/ig get`) of any post / reel / **carousel** (photos
  *and* videos) at **native resolution**, **plus** monitoring of new **posts and stories**.
- **Tag on new content** — optionally ping a role or person when a tracked account posts.

Built with Python + [discord.py](https://github.com/Rapptz/discord.py), packaged as a single
Docker container. Engines: [yt-dlp](https://github.com/yt-dlp/yt-dlp) (TikTok) and
[gallery-dl](https://github.com/mikf/gallery-dl) (Instagram).

## Features

- **`/tt` group** — `add` / `remove` / `list` / `get <url>`. `add` takes an optional `tag`
  (role/person to ping) and can target a thread. A paced scheduler sweeps all tracked
  accounts evenly with adaptive backoff and optional proxy support.
- **`/ig` group** — `get <url>` (on-demand), plus `add` / `remove` / `list` to forward an
  account's new **posts and/or stories** (`type:` posts | stories | both), with the same
  `tag` + thread options. Monitoring is sharded across one or more burner logins
  (`IG_COOKIES`), each with its own proxy, and polled slowly to respect Instagram.
- **Auto-detect** — paste a TikTok or Instagram link in any channel the bot can see and it
  downloads it automatically, then suppresses Discord's redundant preview embed
  (toggles in `configs.yml`).
- **Upload-cap aware** — oversized videos are ffmpeg-compressed to fit Discord's limit; if a
  file still won't fit, the bot posts the source link instead, so nothing is silently dropped.
  Set `max_upload_mb` to match your server's boost level (10 / 50 / 100).
- **Hardened** — non-root container, RAM/CPU/PID caps, subprocess timeouts, optional proxy.

## Quick start

```bash
cp .env.example .env      # paste your BOT_TOKEN
docker compose up -d --build
docker compose logs -f    # look for "is online" and "synced N slash command(s)"
```

In the Discord Developer Portal: enable the **Message Content Intent**, and invite the bot
with the `bot` + `applications.commands` scopes.

## Configuration

`configs.yml` is mounted into the container, so settings-only changes need just
`docker compose restart`. Code or dependency changes need `docker compose up -d --build`.

| Setting | What it does |
| --- | --- |
| `max_upload_mb` | Discord upload cap. 10 (none), 50 (Level 2 boost), 100 (Level 3). |
| `auto_detect_links` | Auto-download TikTok/Instagram links pasted in chat. |
| `sweep_target_seconds` | How often each tracked TikTok account is re-checked. |
| `compress_oversize` | ffmpeg-compress videos over the cap before falling back to a link. |
| `download_timeout` / `max_files_per_post` | Instagram download guardrails. |

### Cookies (optional)

- **TikTok** runs fine anonymously. `TIKTOK_COOKIES` is optional, for reliability at scale.
- **Instagram** requires login to view individual posts — a direct post/reel link redirects to
  the login page when logged out, so `/ig` needs **`IG_COOKIES`** set to a Netscape `cookies.txt`
  exported from a logged-in (ideally throwaway) account. Cookies also unlock original photo
  resolution (anonymous display tops out around 1080px wide).

See [`DEPLOY.md`](DEPLOY.md) for deploying to a server.

## Notes

Personal, self-hosted use. Respect the source platforms' terms and the rights of creators
whose content you download.
