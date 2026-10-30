# Watchdog

The bot DMs you when it breaks, but only while it's still alive to do so. If the container
stops, crash-loops or hangs, it goes quiet, and quiet looks exactly like "nothing new
happened".

This watchdog fixes that. It's a separate, tiny container (standard library only, 64 MB cap,
read-only filesystem) that reads the heartbeat file the bot writes every 5 minutes
(`data/health/heartbeat.json`). If a heartbeat is older than `STALE_MINUTES`, it sends a
Discord message. When the heartbeat comes back, it sends another.

```bash
cp -r watchdog ~/bot-watchdog && cd ~/bot-watchdog
cp .env.example .env && chmod 600 .env
nano .env            # BOT_TOKEN (any bot's) or DISCORD_WEBHOOK_URL
nano compose.yml     # point the volume at your bot's data/health folder
docker compose up -d
```

To watch several bots, mount each bot's `data/health` folder and list them all in `WATCH`.
Any bot that writes `{"ts": <unix time>}` to a file every few minutes can be watched this way.
[Tweetcord](https://github.com/Yuuzi261/Tweetcord), for example, can be patched to do it.

The watchdog can't see the whole host going down, because then nothing on that host is
running. For that, you need a check from another machine.
