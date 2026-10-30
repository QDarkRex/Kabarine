"""Tells a human when the bot stops forwarding — through Discord itself.

Every poller can fail quietly: TikTok/Instagram refuse a listing, an extractor
breaks after a site change, a download keeps failing. The bot then stays "online"
and posts nothing, and nobody notices until someone asks why a video never came.

Checks (every CHECK_EVERY seconds):
  1. stalled  — a poller hasn't finished a sweep in `health_stall_minutes`
  2. failing  — `health_fail_sweeps` sweeps in a row where at least
                `health_fail_ratio` of the accounts couldn't be read. This is what an
                extractor broken by a site change looks like, so the alert names the
                installed yt-dlp/gallery-dl versions and the newest ones on PyPI.
  3. forwards — `health_forward_failures` forwards in a row failed (download/upload)
  4. quiet    — nothing forwarded at all for `health_quiet_hours`
  5. disk     — less than `health_disk_free_gb` free on the disk holding DATA_PATH

Each check also deletes anything in DATA_PATH/downloads older than DOWNLOAD_MAX_AGE:
every download is temporary, and one interrupted mid-way (container restarted or
OOM-killed while compressing) would otherwise stay there forever.

An alert is sent once when a problem starts, repeated every `health_repeat_hours`
while it lasts, and followed by a recovery message when it clears. A short notice is
sent on every start too.

Messages go to `health_alert_channel_id` if set, otherwise as a DM to the owner of
this bot's Discord application.

Each check also writes DATA_PATH/health/heartbeat.json, so a watchdog outside the
container can tell when the bot itself is stopped or hung — which this module, sending
through the bot, can never report.
"""
import asyncio
import json
import os
import shutil
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from importlib import metadata

from src.log import get_logger

log = get_logger(__name__)

CHECK_EVERY = 300  # seconds between health checks
DOWNLOAD_MAX_AGE = 2 * 3600  # no download/compress runs this long (all are time-capped)
EXTRACTORS = ("yt-dlp", "gallery-dl")

# Alert wording, picked by `alert_language`.
TEXT = {
    "en": {
        "start": "✅ Media bot running — {summary}. yt-dlp {ytdlp}, gallery-dl {gdl}. Health checks are on.",
        "still": "\n_(still the case)_",
        "stall": "⚠️ **Media bot: poller `{name}` is stuck** — no sweep finished in {minutes} min. "
                 "Check `docker compose logs --since 1h`.",
        "stall_ok": "✅ Media bot: poller `{name}` is running again.",
        "fail": "🚨 **Media bot: `{name}` can't read most accounts** — {streak} sweeps in a row, "
                "last one {ok}/{total} OK.\nInstalled: {versions}. {hint}",
        "fail_newer": "Newer versions exist — bump them in `requirements.txt` and rebuild.",
        "fail_same": "These are the latest; TikTok/Instagram is probably rate-limiting the server's IP.",
        "latest": " (latest {v})",
        "fail_ok": "✅ Media bot: `{name}` is back to normal ({ok}/{total} accounts read).",
        "forward": "⚠️ **Media bot: {n} forwards in a row failed** (download/upload to Discord). "
                   "They will be retried, but check the logs.",
        "forward_ok": "✅ Media bot: forwards are working again.",
        "quiet": "⚠️ **Media bot: nothing forwarded for {hours} hours.** It looks alive but may be mute.",
        "quiet_ok": "✅ Media bot: items are flowing again.",
        "disk": "⚠️ **Media bot: the server disk is almost full** — {free:.1f} GB free of {total:.0f} GB. "
                "Downloads and the database will start failing.",
        "disk_ok": "✅ Media bot: disk space is fine again ({free:.1f} GB free).",
    },
    "id": {
        "start": "✅ Media bot aktif — {summary}. yt-dlp {ytdlp}, gallery-dl {gdl}. "
                 "Pemantauan kesehatan berjalan.",
        "still": "\n_(masih terjadi)_",
        "stall": "⚠️ **Media bot: poller `{name}` macet** — tidak ada putaran selesai selama {minutes} "
                 "menit. Cek `docker compose logs --since 1h`.",
        "stall_ok": "✅ Media bot: poller `{name}` jalan lagi.",
        "fail": "🚨 **Media bot: `{name}` gagal membaca sebagian besar akun** — {streak} putaran "
                "berturut-turut, terakhir {ok}/{total} berhasil.\nTerpasang: {versions}. {hint}",
        "fail_newer": "Ada versi lebih baru — naikkan versinya di `requirements.txt` lalu rebuild.",
        "fail_same": "Versi sudah yang terbaru; kemungkinan TikTok/IG sedang membatasi IP server.",
        "latest": " (terbaru {v})",
        "fail_ok": "✅ Media bot: `{name}` kembali normal ({ok}/{total} akun terbaca).",
        "forward": "⚠️ **Media bot: {n} kiriman berturut-turut gagal** (download/upload ke Discord). "
                   "Item-nya akan dicoba lagi, tapi cek log.",
        "forward_ok": "✅ Media bot: kiriman berhasil lagi.",
        "quiet": "⚠️ **Media bot: tidak ada post/story yang diteruskan selama {hours} jam.** "
                 "Bot tampak hidup tapi mungkin bisu.",
        "quiet_ok": "✅ Media bot: kiriman kembali mengalir.",
        "disk": "⚠️ **Media bot: disk server hampir penuh** — sisa {free:.1f} GB dari {total:.0f} GB. "
                "Download dan database akan mulai gagal.",
        "disk_ok": "✅ Media bot: ruang disk aman lagi (sisa {free:.1f} GB).",
    },
}


def sweep_downloads(path, older_than):
    """Delete entries in the downloads folder older than `older_than` seconds (0 = all).
    Returns how many were removed."""
    removed, cutoff = 0, time.time() - older_than
    try:
        names = os.listdir(path)
    except OSError:
        return 0
    for name in names:
        p = os.path.join(path, name)
        try:
            if os.path.getmtime(p) > cutoff:
                continue
            if os.path.isdir(p):
                shutil.rmtree(p)
            else:
                os.remove(p)
            removed += 1
        except OSError:
            pass
    return removed


def installed_versions():
    out = {}
    for name in EXTRACTORS:
        try:
            out[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            out[name] = "?"
    return out


def latest_versions(timeout=10):
    """Newest release of each extractor on PyPI ({} entries on failure — best effort)."""
    out = {}
    for name in EXTRACTORS:
        try:
            with urllib.request.urlopen(f"https://pypi.org/pypi/{name}/json", timeout=timeout) as r:
                out[name] = json.load(r)["info"]["version"]
        except Exception:
            pass
    return out


class Feed:
    """One poller's recent history."""

    def __init__(self, now):
        self.registered_at = now
        self.last_sweep_at = None
        self.bad_streak = 0          # consecutive sweeps over the failure ratio
        self.last_ok = self.last_total = 0


class HealthMonitor:
    def __init__(self, bot):
        self.bot = bot
        self.started_at = self._now()
        self.feeds: dict[str, Feed] = {}
        self.last_forward_at = None
        self.forward_fail_streak = 0
        self._active: dict[str, datetime] = {}   # alert key -> when it was last sent
        self._target = None
        self._task = None

    @staticmethod
    def _now():
        return datetime.now(timezone.utc)

    def _cfg(self, key, default):
        v = self.bot.cfg.get(key)
        return default if v is None else v

    def _t(self, key, **kw):
        lang = TEXT.get(self._cfg("alert_language", "en"), TEXT["en"])
        return lang[key].format(**kw)

    # --- signals, fed by the pollers and monitor.forward_new ----------------------

    def register(self, feed):
        """Called when a poller starts, so a poller that never completes a sweep is caught."""
        self.feeds.setdefault(feed, Feed(self._now()))

    def sweep(self, feed, ok, total):
        f = self.feeds.setdefault(feed, Feed(self._now()))
        f.last_sweep_at = self._now()
        f.last_ok, f.last_total = ok, total
        ratio = float(self._cfg("health_fail_ratio", 0.5))
        if total and (total - ok) / total >= ratio:
            f.bad_streak += 1
        else:
            f.bad_streak = 0

    def forwarded(self):
        self.last_forward_at = self._now()
        self.forward_fail_streak = 0

    def forward_failed(self):
        self.forward_fail_streak += 1

    # --- sending -----------------------------------------------------------------

    async def _resolve_target(self):
        if self._target is None:
            channel_id = self.bot.cfg.get("health_alert_channel_id")
            if channel_id:
                channel_id = int(channel_id)
                self._target = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
            else:
                info = await self.bot.application_info()
                self._target = info.team.owner if info.team and info.team.owner else info.owner
        return self._target

    async def send(self, text):
        if not self._cfg("health_alert_enabled", True):
            return
        try:
            await (await self._resolve_target()).send(text)
        except Exception as e:  # never let alerting take the bot down
            log.error("health alert could not be delivered (%s: %s); message was: %s",
                      type(e).__name__, e, text)

    async def _raise(self, key, text):
        """Send on first occurrence, then again every health_repeat_hours while it lasts."""
        repeat = timedelta(hours=float(self._cfg("health_repeat_hours", 6)))
        last = self._active.get(key)
        if last is None:
            await self.send(text)
        elif self._now() - last >= repeat:
            await self.send(text + self._t("still"))
        else:
            return
        self._active[key] = self._now()

    async def _clear(self, key, text):
        if self._active.pop(key, None) is not None:
            await self.send(text)

    # --- checks --------------------------------------------------------------------

    async def startup_report(self, summary):
        v = installed_versions()
        await self.send(self._t("start", summary=summary, ytdlp=v["yt-dlp"], gdl=v["gallery-dl"]))

    async def check(self):
        now = self._now()
        stall = timedelta(minutes=float(self._cfg("health_stall_minutes", 60)))
        fail_sweeps = int(self._cfg("health_fail_sweeps", 3))

        for name, f in sorted(self.feeds.items()):
            since = f.last_sweep_at or f.registered_at
            if now - since > stall:
                minutes = int((now - since).total_seconds() // 60)
                await self._raise(f"stall:{name}", self._t("stall", name=name, minutes=minutes))
            else:
                await self._clear(f"stall:{name}", self._t("stall_ok", name=name))

            if f.bad_streak >= fail_sweeps:
                await self._raise(f"fail:{name}", await self._failing_text(name, f))
            else:
                await self._clear(f"fail:{name}",
                                  self._t("fail_ok", name=name, ok=f.last_ok, total=f.last_total))

        limit = int(self._cfg("health_forward_failures", 5))
        if self.forward_fail_streak >= limit:
            await self._raise("forward", self._t("forward", n=self.forward_fail_streak))
        else:
            await self._clear("forward", self._t("forward_ok"))

        quiet = timedelta(hours=float(self._cfg("health_quiet_hours", 24)))
        since = self.last_forward_at or self.started_at
        if any(f.last_total for f in self.feeds.values()) and now - since > quiet:
            hours = int((now - since).total_seconds() // 3600)
            await self._raise("quiet", self._t("quiet", hours=hours))
        else:
            await self._clear("quiet", self._t("quiet_ok"))

        data = os.getenv("DATA_PATH", "./data")
        try:
            usage = shutil.disk_usage(data)
        except OSError:
            usage = None
        if usage:
            free, total = usage.free / 1024 ** 3, usage.total / 1024 ** 3
            if free < float(self._cfg("health_disk_free_gb", 5)):
                await self._raise("disk", self._t("disk", free=free, total=total))
            else:
                await self._clear("disk", self._t("disk_ok", free=free))

        n = sweep_downloads(os.path.join(data, "downloads"), DOWNLOAD_MAX_AGE)
        if n:
            log.info("removed %d stale item(s) from downloads/", n)

    async def _failing_text(self, name, f):
        have = installed_versions()
        latest = await asyncio.to_thread(latest_versions)
        parts = []
        for pkg in EXTRACTORS:
            newer = latest.get(pkg)
            parts.append(f"{pkg} {have[pkg]}"
                         + (self._t("latest", v=newer) if newer and newer != have[pkg] else ""))
        newer_exists = any(latest.get(p) and latest[p] != have[p] for p in EXTRACTORS)
        return self._t("fail", name=name, streak=f.bad_streak, ok=f.last_ok, total=f.last_total,
                       versions=", ".join(parts), hint=self._t("fail_newer" if newer_exists else "fail_same"))

    def write_heartbeat(self):
        path = os.path.join(os.getenv("DATA_PATH", "./data"), "health")
        try:
            os.makedirs(path, exist_ok=True)
            body = {"ts": int(time.time()), "alerts": sorted(self._active),
                    "feeds": {n: {"ok": f.last_ok, "total": f.last_total,
                                  "last_sweep": f.last_sweep_at.isoformat() if f.last_sweep_at else None}
                              for n, f in self.feeds.items()}}
            tmp = os.path.join(path, "heartbeat.json.tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(body, fh)
            os.replace(tmp, os.path.join(path, "heartbeat.json"))
        except OSError as e:
            log.warning("could not write heartbeat (%s)", e)

    def start(self):
        if self._task is None:
            self._task = asyncio.create_task(self._watch())

    async def _watch(self):
        self.write_heartbeat()
        while True:
            await asyncio.sleep(CHECK_EVERY)
            try:
                await self.check()
            except Exception as e:
                log.error("health check failed: %s: %s", type(e).__name__, e)
            self.write_heartbeat()
