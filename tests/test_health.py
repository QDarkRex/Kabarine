"""State-machine tests for src/health.py with a fake clock and a fake send. Nothing
reaches Discord or PyPI."""
import asyncio
import json
import os
import sys
import tempfile
import types
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src import health  # noqa: E402
from src.config import DEFAULTS  # noqa: E402

T0 = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)


def make(**cfg):
    clock = [T0]
    bot = types.SimpleNamespace(cfg={**DEFAULTS, "alert_language": "id", **cfg})
    h = health.HealthMonitor(bot)
    h._now = lambda: clock[0]
    h.started_at = T0
    sent = []

    async def fake_send(text):
        sent.append(text)
    h.send = fake_send

    def advance(**kw):
        clock[0] += timedelta(**kw)
    return h, sent, advance


def run(coro):
    return asyncio.run(coro)


def test_stalled_poller_alerts_once_then_recovers():
    h, sent, advance = make()
    h.register("tiktok-video")
    advance(minutes=30)
    run(h.check())
    assert sent == []
    advance(minutes=31)
    run(h.check())
    run(h.check())                       # still stalled: no repeat inside repeat_hours
    assert len(sent) == 1 and "macet" in sent[0]
    h.sweep("tiktok-video", 54, 54)
    run(h.check())
    assert len(sent) == 2 and "jalan lagi" in sent[1]


def test_failing_sweeps_need_a_streak_and_name_versions(monkeypatch):
    h, sent, advance = make()
    monkeypatch.setattr(health, "latest_versions", lambda timeout=10: {"yt-dlp": "2099.1.1"})
    h.sweep("tiktok-story", 10, 54)      # 1 bad sweep
    h.sweep("tiktok-story", 54, 54)      # good sweep resets the streak
    h.sweep("tiktok-story", 10, 54)
    h.sweep("tiktok-story", 20, 54)
    run(h.check())
    assert sent == []                    # only 2 in a row
    h.sweep("tiktok-story", 0, 54)
    run(h.check())
    assert len(sent) == 1
    assert "0/54" in sent[0] and "terbaru 2099.1.1" in sent[0] and "requirements.txt" in sent[0]
    h.sweep("tiktok-story", 50, 54)
    run(h.check())
    assert "kembali normal" in sent[-1] and "50/54" in sent[-1]


def test_half_failed_counts_as_bad_and_empty_sweep_does_not():
    h, _sent, _advance = make()
    h.sweep("x", 27, 54)
    assert h.feeds["x"].bad_streak == 1
    h.sweep("x", 0, 0)                   # no subscriptions: not a failure
    assert h.feeds["x"].bad_streak == 0


def test_repeat_after_repeat_hours():
    h, sent, advance = make()
    h.register("tiktok-video")
    advance(minutes=61)
    run(h.check())
    advance(hours=5)
    run(h.check())
    assert len(sent) == 1
    advance(hours=1, minutes=1)
    run(h.check())
    assert len(sent) == 2 and "masih terjadi" in sent[1]


def test_forward_failure_streak():
    h, sent, _advance = make()
    for _ in range(4):
        h.forward_failed()
    run(h.check())
    assert sent == []
    h.forward_failed()
    run(h.check())
    assert len(sent) == 1 and "5 kiriman" in sent[0]
    h.forwarded()
    run(h.check())
    assert "berhasil lagi" in sent[-1]


def test_quiet_only_when_something_is_watched():
    h, sent, advance = make()
    h.sweep("tiktok-video", 0, 0)        # nothing subscribed
    advance(hours=25)
    h.sweep("tiktok-video", 0, 0)
    run(h.check())
    assert sent == []
    h.sweep("tiktok-video", 54, 54)
    run(h.check())
    assert len(sent) == 1 and "25 jam" in sent[0]
    h.forwarded()
    run(h.check())
    assert "mengalir" in sent[-1]


def test_heartbeat_file(monkeypatch):
    h, _sent, _advance = make()
    data = tempfile.mkdtemp()
    monkeypatch.setenv("DATA_PATH", data)
    h.sweep("tiktok-video", 50, 54)
    h.write_heartbeat()
    body = json.load(open(os.path.join(data, "health", "heartbeat.json"), encoding="utf-8"))
    assert body["feeds"]["tiktok-video"]["ok"] == 50 and body["ts"] > 0


def test_send_never_raises():
    bot = types.SimpleNamespace(cfg=dict(DEFAULTS))
    h = health.HealthMonitor(bot)

    async def boom():
        raise RuntimeError("discord down")
    h._resolve_target = boom
    run(h.send("x"))                     # logged, not raised


def test_disabled_sends_nothing():
    bot = types.SimpleNamespace(cfg={**DEFAULTS, "health_alert_enabled": False})
    h = health.HealthMonitor(bot)

    async def must_not_resolve():
        raise AssertionError("resolved a target while disabled")
    h._resolve_target = must_not_resolve
    run(h.send("x"))


def test_story_sweep_reports_failures(monkeypatch, tmp_path):
    """The story poller counts unreadable accounts and reports the sweep."""
    from cogs import tracking
    from src import db
    from tests.fakes import FakeHealth

    path = str(tmp_path / "t.db")
    run(db.init_db(path))
    for user in ("ok1", "ok2", "bad"):
        run(db.add_subscription(path, "tiktok", user, "1", "9", "story", None, None, "0"))
        run(db.set_ids(path, user, "1" + str(len(user)), None))
    bot = types.SimpleNamespace(health=FakeHealth(), db_path=path, cookies=None, proxy=None,
                                cfg={"tiktok_story_sweep_seconds": 0, "tiktok_story_min_spacing": 0,
                                     "request_jitter": 0})

    def list_stories(user_id, username, *a, **k):
        if username == "bad":
            raise RuntimeError("403")
        return []
    monkeypatch.setattr(tracking.tiktok_story, "list_stories", list_stories)

    async def no_sleep(_):
        pass
    monkeypatch.setattr(tracking.asyncio, "sleep", no_sleep)
    run(tracking.Tracking(bot)._story_sweep())
    assert bot.health.sweeps == [("tiktok-story", 2, 3)]


def test_english_wording():
    h, sent, advance = make(alert_language="en")
    h.register("tiktok-video")
    advance(minutes=61)
    run(h.check())
    advance(hours=6, minutes=1)
    run(h.check())
    assert "is stuck" in sent[0] and "61 min" in sent[0] and "still the case" in sent[1]
    h, sent, advance = make(alert_language="xx")   # unknown language falls back to English
    for _ in range(5):
        h.forward_failed()
    run(h.check())
    assert "5 forwards in a row failed" in sent[0]


def test_disk_alert_and_recovery(monkeypatch):
    h, sent, _advance = make(health_disk_free_gb=5)
    usage = [types.SimpleNamespace(total=100 * 1024 ** 3, used=0, free=2 * 1024 ** 3)]
    monkeypatch.setattr(health.shutil, "disk_usage", lambda p: usage[0])
    run(h.check())
    assert len(sent) == 1 and "hampir penuh" in sent[0] and "2.0 GB" in sent[0]
    run(h.check())
    assert len(sent) == 1                       # no repeat inside health_repeat_hours
    usage[0] = types.SimpleNamespace(total=100 * 1024 ** 3, used=0, free=40 * 1024 ** 3)
    run(h.check())
    assert sent[-1].startswith("✅") and "40.0 GB" in sent[-1]


def test_sweep_downloads_removes_only_old_entries(tmp_path):
    import time as _time
    d = tmp_path / "downloads"
    (d / "x_old").mkdir(parents=True)
    (d / "x_old" / "a.mp4").write_bytes(b"x")
    (d / "old_c.mp4").write_bytes(b"x")
    (d / "fresh.mp4").write_bytes(b"x")
    past = _time.time() - 3 * 3600
    for p in (d / "x_old", d / "old_c.mp4"):
        os.utime(p, (past, past))
    assert health.sweep_downloads(str(d), health.DOWNLOAD_MAX_AGE) == 2
    assert sorted(os.listdir(d)) == ["fresh.mp4"]
    assert health.sweep_downloads(str(d), 0) == 1 and os.listdir(d) == []
    assert health.sweep_downloads(str(tmp_path / "missing"), 0) == 0
