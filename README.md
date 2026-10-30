# 📮 Kabarine

[![tests](https://github.com/QDarkRex/Kabarine/actions/workflows/tests.yml/badge.svg)](https://github.com/QDarkRex/Kabarine/actions/workflows/tests.yml)

> *Kabar* is Indonesian for "news", and Erine is the author's oshi (favourite idol). So: the
> bot that brings you news from the accounts you care about, named after the one account the
> author cares about most.

**A self-hosted Discord bot that delivers TikTok, Instagram and X posts to your server —
including TikTok & Instagram stories.** It watches accounts you choose and reposts their new
videos, photos and stories into a channel (pinging a role if you like), and turns any pasted
link into the actual media, at native resolution, without watermarks.

> ⚠️ **Vibe-coded, proudly and a little nervously.** Almost every line here was written by an
> AI while a human described what they wanted in Indonesian, usually around 2 AM, then
> pressed Enter with great confidence. It has been running 24/7 on a real server for months,
> it has a test suite, and the tests pass, which surprised everyone involved. Still: read the
> code before you trust it with your server. Pull requests from people who actually know what
> they're doing are very welcome.

## What it does

| | On demand | Auto-forward new content |
| --- | --- | --- |
| **TikTok** | `/tt get <url>` or just paste a link. Videos, photo/slideshow posts | `/tt add` → new **videos** and/or **stories**, each labelled "🎬 TikTok Post" / "📖 TikTok Story" |
| **Instagram** | `/ig get <url>` or paste. Posts, reels, carousels (photos *and* videos), story & highlight links | `/ig add` → new **posts** and/or **stories** (needs a burner login) |
| **X / Twitter** | `/x get <url>` or paste. Original-size photos, best-quality video | — (try [Tweetcord](https://github.com/Yuuzi261/Tweetcord) for that) |

Also:

- **Pings** — every subscription can ping a role, a person or `@everyone`, and can post into
  a thread.
- **Upload-cap aware** — videos over your server's limit are ffmpeg-compressed to fit; if that
  still fails, the link is posted instead. Nothing is dropped silently.
- **Tells you when it breaks** — DMs you when a poller stalls, when most accounts become
  unreadable (usually an extractor that needs updating, and the message says which), when
  forwards keep failing, or when nothing has been posted for a day. A separate
  [watchdog](watchdog/) tells you if the bot itself dies.
- **Polite by design** — accounts are polled on a paced schedule with jitter and adaptive
  backoff. Instagram is polled slowly on purpose, and can be spread across several burner
  logins, each with its own proxy.
- **Hardened container** — non-root, RAM/CPU/PID caps, timeouts on every download, and
  live-stream/profile links are refused instead of downloaded forever.

## Quick start

You need Docker and a Discord bot token.

1. In the [Discord Developer Portal](https://discord.com/developers/applications), create
   an application, add a bot, turn on the **Message Content Intent**, and copy the token.
2. Invite it with the `bot` + `applications.commands` scopes. Give it Send Messages, Attach
   Files, Embed Links and, if you want link previews tidied up, Manage Messages.
3. Run it:

```bash
git clone https://github.com/QDarkRex/Kabarine.git kabarine
cd kabarine
cp .env.example .env        # paste BOT_TOKEN
docker compose up -d --build
docker compose logs -f      # wait for "is online" and "synced 3 slash command(s)"
```

You'll get a DM from the bot saying it's running. Then, in your server:

```
/tt add username:khaby.lame type:both tag:@TikTok
/ig get url:https://www.instagram.com/p/...
/x get url:https://x.com/.../status/...
```

`/tt add` also accepts a link to one of the account's videos, which is more reliable than the
bare username.

## Configuration

**`configs.yml`** holds the settings. It's mounted into the container, so changing it only
needs `docker compose restart`. Anything you leave out uses the default in
[`src/config.py`](src/config.py). The ones you're most likely to touch:

| Setting | Default | What it does |
| --- | --- | --- |
| `max_upload_mb` | `10` | Your server's upload cap: 10 (no boost), 50 (Level 2), 100 (Level 3) |
| `auto_detect_links` / `x_auto_detect_links` | `true` | Download links people paste in chat |
| `sweep_target_seconds` | `150` | How often each TikTok account is checked for new videos |
| `tiktok_story_sweep_seconds` | `600` | How often stories are checked (they last 24 h anyway) |
| `ig_monitoring_enabled` | `true` | `false` = Instagram downloads only, no auto-forwarding |
| `tiktok_native` | `true` | Native resolution even when it's H.265 (may not preview inline everywhere) |
| `alert_language` | `en` | Wording of the health DMs: `en` or `id` |
| `health_alert_channel_id` | — | Post health alerts in a channel instead of DMing the bot's owner |

**`.env`** holds secrets and logins. See [`.env.example`](.env.example).

- `BOT_TOKEN` is required.
- `IG_COOKIES` is needed for Instagram. Instagram shows almost nothing to logged-out
  visitors, so export a `cookies.txt` from a **throwaway** account. Several files can be
  comma-separated to spread the load, and `IG_PROXIES` pairs one proxy with each.
- `TIKTOK_COOKIES` and `TIKTOK_PROXY` are optional. TikTok works anonymously.

Deploying to a server, updating and moving hosts: [docs/deploy.md](docs/deploy.md). Asking a
friend to host it: [docs/hosting-on-a-friends-server.md](docs/hosting-on-a-friends-server.md).

## Good to know

- **Sites change, extractors catch up.** yt-dlp and gallery-dl are pinned to versions that
  were verified working. When TikTok or Instagram changes something, the bot DMs you and
  names the newer versions. Bump them in `requirements.txt` and run
  `docker compose up -d --build`.
- **Instagram is the fragile one.** It flags automated access quickly, especially from
  datacenter IPs. Use a burner account, keep polling slow, and add a residential proxy if you
  watch many accounts.
- **H.265 / VP9.** Native-quality TikTok and Instagram video is often in these codecs, and some
  Discord clients show it as a download instead of playing it inline. Set `tiktok_native: false`
  if that bothers you.
- **Respect creators.** Only archive what you're allowed to, and don't use this to repost
  other people's work as your own.

## Development

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt pytest     # bin/ instead of Scripts/ on Linux/macOS
.venv/Scripts/python -m pytest -q tests
```

The tests use a fake Discord, a temporary SQLite database and canned extractor output, so
they need no network or token. [AGENTS.md](AGENTS.md) explains the architecture and the hard
lessons behind it (why live links are refused, why Instagram's pinned posts broke forwarding,
how TikTok stories are found at all). It's written for humans and AI agents alike, because
both of them work on this code.

## Credits: who we borrowed from

Kabarine isn't a GitHub fork of anything. It was written from scratch, but it stands on a
lot of other people's work:

- **[yt-dlp](https://github.com/yt-dlp/yt-dlp)** downloads TikTok videos and photo posts and
  reads account listings.
- **[gallery-dl](https://github.com/mikf/gallery-dl)** handles Instagram posts and stories, and
  the TikTok story listing.
- **[discord.py](https://github.com/Rapptz/discord.py)** connects to Discord.
- **[FxTwitter](https://github.com/FixTweet/FxTwitter)** has a public API that makes X
  downloads possible without logging in.
- **[tikwm](https://www.tikwm.com/)** is the fallback for TikTok photo posts.
- **InstaFix-style mirrors** (`kkinstagram`, `ddinstagram`, `vxinstagram`) are a last-resort,
  cookie-free fallback for public Instagram posts.
- **[Tweetcord](https://github.com/Yuuzi261/Tweetcord) by Yuuzi261.** Our private fork of it
  forwards X posts on the same server. The health-alert design was born there after a
  nine-day silent outage, and was then ported here.

## License

[MIT](LICENSE). The bundled tools keep their own licenses: yt-dlp is Unlicense, gallery-dl is
GPL-2.0, discord.py is MIT.
