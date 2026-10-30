# STATUS — current state & next steps

_Snapshot for whoever picks this up next. Pair with [`AGENTS.md`](AGENTS.md) (how it works) and
[`CHANGELOG.md`](CHANGELOG.md) (history)._

Last updated: **2026-10-25**.

---

## Deployment status

- **Live server:** `x99` (moved off the caddy VPS), container `tiktokbot`, bot **Aerox Joget#4488**.
  Deployed from branch **`deploy/ig-fixes-lean`** (not `main`; the branch is local-only, not pushed).
- **TikTok:** ~55 accounts monitored anonymously. Videos are listed via `tiktokuser:<secUid>`
  (2026-10-30): the @handle profile page failed for 35/54 accounts at once ("Unable to extract
  secondary user ID"), the secUid listing for 0/54. The @handle listing is only the fallback now.
- **TikTok stories:** gallery-dl based poller (2026-10-25) — see AGENTS.md §8. Story subscriptions are
  opt-in per account (`/tt add type:stories|both`).
- **Instagram:** downloader-only (`ig_monitoring_enabled: false`). As of 2026-10-25 the burner's IG
  session is **dead** (Instagram redirects it to login), and anonymous IG profile access fails from any
  IP — so `/ig get` and any IG monitoring need fresh burner cookies first.
- **To deploy:** bundle → copy → `docker compose up -d --build` (see [AGENTS.md §9](AGENTS.md#9-deployment)).

## Superseded: Playwright TikTok-story watcher (`f58e45a`, on `main`)

Replaced on the deployed branch by the gallery-dl story poller, which needs no Chromium and fits
`mem_limit: 512m`. Both use `src/tiktok_story.py` and `tiktok_story_enabled` (the old one defaulted to
false) — do not merge `f58e45a` into the deployed branch.

## What works (verified locally / in production)

- **TikTok:** on-demand download + account monitoring, **native resolution** (h265 1080p), `tag:` pinging,
  thread targets. Runs anonymously (cookies optional).
- **Instagram:** `/ig get` on-demand, **post monitoring**, **story monitoring** (`/ig add type:posts|stories|both`),
  burner sharding, `tag:` pinging, thread targets. Requires burner cookies.

## Experimental / unverified — needs real-world validation

- **IG story fix (`c07011c`)** — not yet confirmed live. If stories still don't appear, work
  [AGENTS.md §7](AGENTS.md#7-instagram-stories-debugging) (H1 subscription type → H2 baseline → H3 code).
- **TikTok story poller (gallery-dl)** — listing and download verified from the server against a live
  story; relies on an undocumented endpoint and on gallery-dl internals, so watch for
  `stories: couldn't check` warnings after any gallery-dl upgrade.

## Roadmap / open items

1. **Deploy the 2 pending commits** — with the `mem_limit` bump if enabling TikTok stories.
2. **Verify IG story forwarding live** (AGENTS.md §7).
3. **Scale Instagram to ~45 accounts:** add an **ISP (static residential) proxy** and wire the 2nd burner:
   `IG_COOKIES=/bot/data/ig_cookies.txt,/bot/data/ig_cookies2.txt` + `IG_PROXIES=<proxy>`.
   `data/ig_cookies2.txt` is already uploaded. See AGENTS.md §3.
4. **`/tt add` still needs one @handle listing** (no secUid exists yet for a new account), so it is
   retried 3x but can still refuse a valid account; running it again usually works.
5. **Server migration** (VPS → local box): copy the whole `~/tiktokbot` folder **including `data/`**, then
   `docker compose up -d --build`. All state is in `data/tiktok.db`; nothing to re-add. Stop the old
   instance first (same bot token can't run twice).

## Key facts

- All monitoring state → `data/tiktok.db`. Cookies → `data/*.txt`. Secrets → `.env`. (`data/` + `.env`
  are gitignored.)
- Repo: `github.com/QDarkRex/discord-media-downloader`. Two slash groups: `/tt`, `/ig`.
- Open GitHub issues track the roadmap items above.
