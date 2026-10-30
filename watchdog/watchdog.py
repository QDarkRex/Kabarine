"""Outside watchdog: alerts when a bot stops writing its heartbeat file.

A bot's own health alerts travel through the bot, so they go silent exactly when the
bot is gone — container stopped, crash loop, event loop hung. This runs in its own
tiny container, reads each bot's DATA_PATH/health/heartbeat.json (mounted read-only)
and sends a Discord message when one goes stale, plus a recovery message.

Standard library only. Configuration (environment):
  WATCH              name=path pairs, comma-separated, e.g.
                     "media-bot=/watch/media/heartbeat.json,tweetcord=/watch/tc/heartbeat.json"
  STALE_MINUTES      heartbeat older than this = down (default 15; bots write every 5)
  CHECK_SECONDS      how often to look (default 60)
  REPEAT_HOURS       re-send an ongoing alert this often (default 6)
  LANGUAGE           en | id (default en)
  Where to send — the first one that is set:
  DISCORD_WEBHOOK_URL               post through a channel webhook
  BOT_TOKEN + ALERT_CHANNEL_ID      post in a channel as that bot
  BOT_TOKEN [+ ALERT_USER_ID]       DM that user (default: the bot application's owner)

It cannot report the whole server being down — nothing on it runs then. That needs a
check from another machine.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

API = "https://discord.com/api/v10"

TEXT = {
    "en": {
        "start": "👀 Watchdog running — watching: {names}.",
        "down": "🚨 **{name} looks down** — no heartbeat for {minutes} min. Container stopped or hung? "
                "Check `docker ps` and its logs.",
        "missing": "🚨 **{name} looks down** — its heartbeat file has not appeared ({minutes} min since "
                   "the watchdog started).",
        "still": "\n_(still the case)_",
        "up": "✅ {name} is alive again.",
    },
    "id": {
        "start": "👀 Watchdog aktif — memantau: {names}.",
        "down": "🚨 **{name} sepertinya mati** — tidak ada heartbeat selama {minutes} menit. Container "
                "berhenti atau macet? Cek `docker ps` dan log-nya.",
        "missing": "🚨 **{name} sepertinya mati** — file heartbeat-nya belum muncul ({minutes} menit "
                   "sejak watchdog jalan).",
        "still": "\n_(masih terjadi)_",
        "up": "✅ {name} hidup lagi.",
    },
}


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def parse_watch(value):
    out = {}
    for part in (value or "").split(","):
        if "=" in part:
            name, path = part.split("=", 1)
            out[name.strip()] = path.strip()
    return out


def heartbeat_age(path, now):
    """Seconds since the heartbeat was written, or None if it can't be read."""
    try:
        with open(path, encoding="utf-8") as f:
            return now - float(json.load(f)["ts"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


class Sender:
    def __init__(self, env):
        self.env = env
        self._dm_channel = None

    def _call(self, method, url, body=None, bot=True):
        headers = {"Content-Type": "application/json", "User-Agent": "bot-watchdog (stdlib, 1)"}
        if bot:
            headers["Authorization"] = "Bot " + self.env["BOT_TOKEN"]
        req = urllib.request.Request(url, method=method, headers=headers,
                                     data=json.dumps(body).encode() if body is not None else None)
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}

    def _target(self):
        if self.env.get("ALERT_CHANNEL_ID"):
            return self.env["ALERT_CHANNEL_ID"]
        if self._dm_channel is None:
            user = self.env.get("ALERT_USER_ID")
            if not user:
                app = self._call("GET", API + "/oauth2/applications/@me")
                user = (app.get("team") or {}).get("owner_user_id") or app["owner"]["id"]
            self._dm_channel = self._call("POST", API + "/users/@me/channels", {"recipient_id": user})["id"]
        return self._dm_channel

    def send(self, text):
        body = {"content": text, "allowed_mentions": {"parse": []}}
        try:
            if self.env.get("DISCORD_WEBHOOK_URL"):
                self._call("POST", self.env["DISCORD_WEBHOOK_URL"], body, bot=False)
            else:
                self._call("POST", f"{API}/channels/{self._target()}/messages", body)
            log("sent: " + text.splitlines()[0])
        except (urllib.error.URLError, OSError, KeyError, ValueError) as e:
            log(f"could not send ({type(e).__name__}: {e}); message was: {text}")


class Watchdog:
    def __init__(self, env, send, now=time.time):
        self.watch = parse_watch(env.get("WATCH"))
        self.stale = float(env.get("STALE_MINUTES", 15)) * 60
        self.repeat = float(env.get("REPEAT_HOURS", 6)) * 3600
        self.text = TEXT.get(env.get("LANGUAGE", "en"), TEXT["en"])
        self.send = send
        self.now = now
        self.started = now()
        self.down_since_alert = {}  # name -> when the last alert went out

    def check(self):
        now = self.now()
        for name, path in self.watch.items():
            age = heartbeat_age(path, now)
            down = (now - self.started > self.stale) if age is None else age > self.stale
            last = self.down_since_alert.get(name)
            if down:
                if last is None or now - last >= self.repeat:
                    minutes = int(((now - self.started) if age is None else age) // 60)
                    key = "missing" if age is None else "down"
                    text = self.text[key].format(name=name, minutes=minutes)
                    self.send(text + (self.text["still"] if last is not None else ""))
                    self.down_since_alert[name] = now
            elif last is not None:
                del self.down_since_alert[name]
                self.send(self.text["up"].format(name=name))


def main():
    env = dict(os.environ)
    if not parse_watch(env.get("WATCH")):
        sys.exit("WATCH is empty — set name=path pairs, see watchdog.py")
    if not env.get("DISCORD_WEBHOOK_URL") and not env.get("BOT_TOKEN"):
        sys.exit("set DISCORD_WEBHOOK_URL, or BOT_TOKEN (+ ALERT_CHANNEL_ID / ALERT_USER_ID)")
    sender = Sender(env)
    dog = Watchdog(env, sender.send)
    log(f"watching {dog.watch} (stale after {dog.stale / 60:.0f} min)")
    sender.send(dog.text["start"].format(names=", ".join(dog.watch)))
    interval = float(env.get("CHECK_SECONDS", 60))
    while True:
        time.sleep(interval)
        try:
            dog.check()
        except Exception as e:  # keep watching whatever happens
            log(f"check failed: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
