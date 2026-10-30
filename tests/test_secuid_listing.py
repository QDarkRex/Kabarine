"""Tests for listing via secUid: DB column migration + id cache, list_recent URL/link
building, resolve_ids, and the poller's secUid -> @handle fallback order."""
import asyncio
import os
import sys
import tempfile
import types

import aiosqlite

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src import db, tiktok, tiktok_story  # noqa: E402
from tests.fakes import FakeHealth  # noqa: E402
from cogs import tracking  # noqa: E402

failures = []


def check(name, cond, extra=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{('   ' + str(extra)) if not cond else ''}")
    if not cond:
        failures.append(name)


async def db_tests():
    print("db: sec_uid column + id cache")
    path = os.path.join(tempfile.mkdtemp(), "o.db")
    async with aiosqlite.connect(path) as d:   # table as deployed 2026-10-25 (no sec_uid)
        await d.execute("CREATE TABLE tiktok_user_ids (username TEXT PRIMARY KEY, user_id TEXT NOT NULL)")
        await d.execute("INSERT INTO tiktok_user_ids VALUES ('levi', '730')")
        await d.commit()
    await db.init_db(path)
    check("old table gains sec_uid, old row kept", await db.get_ids(path, "levi") == ("730", None),
          await db.get_ids(path, "levi"))
    await db.init_db(path)   # second start must not fail on an existing column
    await db.set_ids(path, "levi", "730", "MS4w-levi")
    check("set_ids stores sec_uid", await db.get_ids(path, "levi") == ("730", "MS4w-levi"))
    await db.set_user_id(path, "levi", "730")
    check("set_user_id (story path) keeps sec_uid", await db.get_ids(path, "levi") == ("730", "MS4w-levi"))
    check("unknown account -> (None, None)", await db.get_ids(path, "x") == (None, None))


class FakeYDL:
    calls = []

    def __init__(self, opts):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download=False):
        FakeYDL.calls.append(url)
        if "/video/" in url:
            return {"uploader_id": "7302014721846166533", "channel_id": "MS4wLjABAAAA-levi"}
        return {"entries": [{"id": "7702", "url": "https://www.tiktok.com/@MS4wLjABAAAA-levi/video/7702"},
                            {"id": "7701", "url": "https://www.tiktok.com/@MS4wLjABAAAA-levi/video/7701"}]}


def listing_tests():
    print("list_recent + resolve_ids")
    real = tiktok.yt_dlp.YoutubeDL
    tiktok.yt_dlp.YoutubeDL = FakeYDL
    tiktok_story.yt_dlp.YoutubeDL = FakeYDL
    try:
        FakeYDL.calls.clear()
        out = tiktok.list_recent("jkt48.levi", 5, sec_uid="MS4wLjABAAAA-levi")
        check("secUid -> tiktokuser: URL", FakeYDL.calls[-1] == "tiktokuser:MS4wLjABAAAA-levi", FakeYDL.calls)
        check("links rebuilt with the @handle, ids kept in order",
              out == [{"id": "7702", "url": "https://www.tiktok.com/@jkt48.levi/video/7702"},
                      {"id": "7701", "url": "https://www.tiktok.com/@jkt48.levi/video/7701"}], out)
        tiktok.list_recent("jkt48.levi", 5)
        check("no secUid -> unchanged @handle URL", FakeYDL.calls[-1] == "https://www.tiktok.com/@jkt48.levi")
        ids = tiktok_story.resolve_ids("jkt48.levi", video_url="https://www.tiktok.com/@jkt48.levi/video/1")
        check("resolve_ids returns (numeric id, secUid)", ids == ("7302014721846166533", "MS4wLjABAAAA-levi"), ids)
        check("resolve_user_id still returns the numeric id",
              tiktok_story.resolve_user_id("u", video_url="https://x/video/1") == "7302014721846166533")
    finally:
        tiktok.yt_dlp.YoutubeDL = real
        tiktok_story.yt_dlp.YoutubeDL = real


async def poller_tests():
    print("poller: secUid first, @handle fallback")
    path = os.path.join(tempfile.mkdtemp(), "p.db")
    await db.init_db(path)
    bot = types.SimpleNamespace(health=FakeHealth(), db_path=path, cfg={}, cookies=None, proxy=None)
    cog = tracking.Tracking.__new__(tracking.Tracking)
    cog.bot = bot
    cog._workdir = lambda: "/tmp"
    seen = []

    async def fake_forward(*a, **k):
        seen.append(("forward", len(a[4])))
    real_fwd, real_list = tracking.monitor.forward_new, tracking.tiktok.list_recent
    tracking.monitor.forward_new = fake_forward
    calls = []

    def fake_list(username, count, cookies, proxy, sec_uid=None):
        calls.append(sec_uid)
        if fake_list.fail_sec and sec_uid:
            raise RuntimeError("tiktokuser down")
        return [{"id": "1", "url": "u"}]
    tracking.tiktok.list_recent = fake_list
    try:
        await db.set_ids(path, "a", "1", "SEC-A")
        fake_list.fail_sec = False
        ok = await cog._check_account("a", [])
        check("cached secUid used, single request", ok and calls == ["SEC-A"], calls)

        calls.clear()
        fake_list.fail_sec = True
        ok = await cog._check_account("a", [])
        check("secUid listing fails -> @handle tried next", ok and calls == ["SEC-A", None], calls)

        calls.clear()
        real_res = tracking.tiktok_story.resolve_ids
        tracking.tiktok_story.resolve_ids = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no video"))
        ok = await cog._check_account("b", [])
        tracking.tiktok_story.resolve_ids = real_res
        check("ids unresolvable -> straight to @handle", ok and calls == [None], calls)
    finally:
        tracking.monitor.forward_new, tracking.tiktok.list_recent = real_fwd, real_list


def _run(fn):
    failures.clear()
    result = fn()
    if asyncio.iscoroutine(result):
        asyncio.run(result)
    assert not failures, failures


def test_db():
    _run(db_tests)


def test_listing():
    _run(listing_tests)


def test_poller():
    _run(poller_tests)


if __name__ == "__main__":
    for fn in (db_tests, listing_tests, poller_tests):
        result = fn()
        if asyncio.iscoroutine(result):
            asyncio.run(result)
    print("\nRESULT:", "ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}")
