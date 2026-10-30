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
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from importlib import metadata

from src.log import get_logger

log = get_logger(__name__)

CHECK_EVERY = 300  # seconds between health checks
EXTRACTORS = ("yt-dlp", "gallery-dl")


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
            await self.send(f"{text}\n_(masih terjadi)_")
        else:
            return
        self._active[key] = self._now()

    async def _clear(self, key, text):
        if self._active.pop(key, None) is not None:
            await self.send(text)

    # --- checks --------------------------------------------------------------------

    async def startup_report(self, summary):
        v = installed_versions()
        await self.send(f"✅ Media bot aktif — {summary}. yt-dlp {v['yt-dlp']}, "
                        f"gallery-dl {v['gallery-dl']}. Pemantauan kesehatan berjalan.")

    async def check(self):
        now = self._now()
        stall = timedelta(minutes=float(self._cfg("health_stall_minutes", 60)))
        fail_sweeps = int(self._cfg("health_fail_sweeps", 3))

        for name, f in sorted(self.feeds.items()):
            since = f.last_sweep_at or f.registered_at
            if now - since > stall:
                minutes = int((now - since).total_seconds() // 60)
                await self._raise(f"stall:{name}",
                                  f"⚠️ **Media bot: poller `{name}` macet** — tidak ada putaran selesai "
                                  f"selama {minutes} menit. Cek `docker compose logs --since 1h`.")
            else:
                await self._clear(f"stall:{name}", f"✅ Media bot: poller `{name}` jalan lagi.")

            if f.bad_streak >= fail_sweeps:
                await self._raise(f"fail:{name}", await self._failing_text(name, f))
            else:
                await self._clear(f"fail:{name}",
                                  f"✅ Media bot: `{name}` kembali normal "
                                  f"({f.last_ok}/{f.last_total} akun terbaca).")

        limit = int(self._cfg("health_forward_failures", 5))
        if self.forward_fail_streak >= limit:
            await self._raise("forward",
                              f"⚠️ **Media bot: {self.forward_fail_streak} kiriman berturut-turut gagal** "
                              f"(download/upload ke Discord). Item-nya akan dicoba lagi, tapi cek log.")
        else:
            await self._clear("forward", "✅ Media bot: kiriman berhasil lagi.")

        quiet = timedelta(hours=float(self._cfg("health_quiet_hours", 24)))
        since = self.last_forward_at or self.started_at
        if any(f.last_total for f in self.feeds.values()) and now - since > quiet:
            hours = int((now - since).total_seconds() // 3600)
            await self._raise("quiet", f"⚠️ **Media bot: tidak ada post/story yang diteruskan selama "
                                       f"{hours} jam.** Bot tampak hidup tapi mungkin bisu.")
        else:
            await self._clear("quiet", "✅ Media bot: kiriman kembali mengalir.")

    async def _failing_text(self, name, f):
        have = installed_versions()
        latest = await asyncio.to_thread(latest_versions)
        parts = []
        for pkg in EXTRACTORS:
            newer = latest.get(pkg)
            parts.append(f"{pkg} {have[pkg]}" + (f" (terbaru {newer})" if newer and newer != have[pkg] else ""))
        hint = ("Ada versi lebih baru — naikkan versinya di `requirements.txt` lalu rebuild."
                if any(latest.get(p) and latest[p] != have[p] for p in EXTRACTORS)
                else "Versi sudah yang terbaru; kemungkinan TikTok/IG sedang membatasi IP server.")
        return (f"🚨 **Media bot: `{name}` gagal membaca sebagian besar akun** — "
                f"{f.bad_streak} putaran berturut-turut, terakhir {f.last_ok}/{f.last_total} berhasil.\n"
                f"Terpasang: {', '.join(parts)}. {hint}")

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
