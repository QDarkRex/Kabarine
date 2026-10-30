# Deploying

The bot runs as one Docker container (`tiktokbot`) next to whatever else the host runs.
Everything it remembers lives in `data/tiktok.db`, so the `data/` folder *is* the deployment.

## First install

```bash
git clone https://github.com/QDarkRex/discord-media-downloader.git ~/tiktokbot
cd ~/tiktokbot
cp .env.example .env && chmod 600 .env
nano .env                          # BOT_TOKEN; optionally IG_COOKIES / proxies
nano configs.yml                   # at least max_upload_mb for your server's boost level
docker compose up -d --build       # first build takes a minute or two
docker compose logs -f             # "is online", "synced 3 slash command(s)"
```

Cookie files go in `data/` (for example `data/ig_cookies.txt`, then
`IG_COOKIES=/bot/data/ig_cookies.txt` in `.env`). The container owns `data/` as uid 1000. If
your SSH user can't write there, copy the file in through a throwaway container:

```bash
docker run --rm -v "$PWD/data:/data" -v ~/ig_cookies.txt:/f:ro --entrypoint cp tiktokbot-tiktokbot /f /data/ig_cookies.txt
```

## Updating

```bash
cd ~/tiktokbot
git pull                           # your configs.yml edits: commit them locally or `git stash` first
docker compose up -d --build
```

- Changed only `configs.yml`? Run `docker compose restart`. No rebuild needed.
- Changed `.env`? Run `docker compose up -d`. `restart` does **not** re-read `.env`.

## Moving to another host

Stop the old one first: the same bot token can't run in two places. Then copy the whole
folder, **including `data/`**, and start it:

```bash
docker compose down                                   # on the old host
rsync -a ~/tiktokbot/ newhost:~/tiktokbot/
ssh newhost 'cd ~/tiktokbot && docker compose up -d --build'
```

## Knowing it's alive

- On every start the bot DMs its application owner (or `health_alert_channel_id`) a ✅ line.
  It sends ⚠️/🚨 when something breaks, then ✅ when it recovers.
- For "the container died", run the [watchdog](../watchdog/) next to it.
- Neither can tell you that the whole host is down. For that, you need a check from another
  machine.
