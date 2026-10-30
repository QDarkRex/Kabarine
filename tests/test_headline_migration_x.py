"""Tests for: TikTok headline+ping, caption placeholder, one-time accounts migration,
X link parsing and best-variant choice. Real SQLite, fake Discord."""
import asyncio
import os
import sys
import tempfile
import types

import aiosqlite

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src import db, monitor, twitter  # noqa: E402
from tests.fakes import FakeHealth  # noqa: E402
from src.discord_post import _caption  # noqa: E402

failures = []


def check(name, cond, extra=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{('   ' + str(extra)) if not cond else ''}")
    if not cond:
        failures.append(name)


class Chan:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, allowed_mentions=None, **kw):
        self.sent.append((content, allowed_mentions))


async def headline_tests():
    print("headline + ping")
    path = os.path.join(tempfile.mkdtemp(), "h.db")
    await db.init_db(path)
    chans = {}
    bot = types.SimpleNamespace(health=FakeHealth(), db_path=path, cfg={},
                                get_channel=lambda cid: chans.setdefault(cid, Chan()))
    fetched = []

    async def handle_url(channel, url, cfg, cookies, workdir, proxy):
        fetched.append(url)

    await db.add_subscription(path, "tiktok", "u", "1", "g", "story", None, None, "0")
    await db.add_subscription(path, "tiktok", "v", "2", "g", "story", "role", "555", "0")
    item = [{"id": "7700000000000000001", "url": "x/7700000000000000001"}]

    async def run(user, chan, mt, mid, headline):
        await monitor.forward_new(bot, "tiktok", user, [(chan, mt, mid, "0")], item, handle_url,
                                  "/tmp", None, None, "TikTok", content_type="story",
                                  noun="story", by_id=True, headline=headline)

    await run("u", "1", None, None, "📖 **TikTok Story** · **@u**")
    c = chans[1].sent
    check("no ping: headline still posted", len(c) == 1 and c[0][0] == "📖 **TikTok Story** · **@u**", c)
    check("no ping: nothing can mention", c[0][1].roles is False and c[0][1].everyone is False)

    await run("v", "2", "role", "555", "📖 **TikTok Story** · **@v**")
    c = chans[2].sent
    check("role ping: '<@&id> headline'", c and c[0][0] == "<@&555> 📖 **TikTok Story** · **@v**", c)
    check("role ping: roles allowed", c[0][1].roles is True)

    # IG / default path: no headline and no ping -> no announcement line at all (unchanged)
    await db.add_subscription(path, "instagram", "w", "3", "g", "post", None, None, "a")
    await monitor.forward_new(bot, "instagram", "w", [("3", None, None, "a")],
                              [{"id": "b", "url": "ig/b"}, {"id": "a", "url": "ig/a"}],
                              handle_url, "/tmp", None, None, "Instagram")
    check("default path unchanged: no announce without ping", chans[3].sent == [], chans[3].sent)
    check("media still forwarded in every case", fetched == ["x/7700000000000000001"] * 2 + ["ig/b"], fetched)


def caption_tests():
    print("caption placeholder")
    check("story placeholder dropped",
          _caption({"uploader": "jkt48.levi", "title": "TikTok video #7700470386475011329"}) == "**@jkt48.levi**")
    check("real description kept",
          _caption({"uploader": "a", "title": "halo semua"}) == "**@a**\nhalo semua")


async def migration_tests():
    print("accounts migration runs once")
    path = os.path.join(tempfile.mkdtemp(), "m.db")
    async with aiosqlite.connect(path) as d:
        await d.execute("CREATE TABLE accounts (username TEXT, channel_id TEXT, guild_id TEXT, "
                        "last_seen_id TEXT, enabled INTEGER)")
        await d.executemany("INSERT INTO accounts VALUES (?,?,?,?,1)",
                            [("gendisjkt48", "1", "g", "765"), ("keep", "2", "g", "766")])
        await d.commit()
    await db.init_db(path)
    rows = await db.get_enabled(path, "tiktok", "post")
    check("first start migrates both", sorted(r[0] for r in rows) == ["gendisjkt48", "keep"], rows)
    await db.remove_subscription(path, "tiktok", "gendisjkt48", None, None)
    await db.init_db(path)   # restart
    rows = await db.get_enabled(path, "tiktok", "post")
    check("removed account stays removed after a restart", [r[0] for r in rows] == ["keep"], rows)
    async with aiosqlite.connect(path) as d:
        names = {r[0] async for r in await d.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    check("old table kept as backup, original name gone",
          "accounts_migrated_backup" in names and "accounts" not in names, names)


def x_tests():
    print("X link parsing")
    text = ("lihat https://x.com/AR_LanaJKT48/status/2111711956981977089?s=20 dan "
            "https://twitter.com/i/web/status/111 juga https://fxtwitter.com/a_b/status/222 "
            "https://mobile.twitter.com/c/status/333 lagi https://x.com/AR_LanaJKT48/status/2111711956981977089 "
            "profil https://x.com/AR_LanaJKT48 dan https://vxtwitter.com/d/status/444/photo/1")
    ids = [t for _, t in twitter.find_links(text)]
    check("all status forms found, duplicates collapsed, profile ignored",
          ids == ["2111711956981977089", "111", "222", "333", "444"], ids)
    check("tweet_id", twitter.tweet_id("https://x.com/u/status/9?t=1") == "9")
    check("non-status -> None", twitter.tweet_id("https://x.com/home") is None)
    item = {"url": "low", "variants": [{"url": "a.m3u8", "content_type": "application/x-mpegURL"},
                                       {"url": "lo.mp4", "content_type": "video/mp4", "bitrate": 632000},
                                       {"url": "hi.mp4", "content_type": "video/mp4", "bitrate": 2176000}]}
    check("highest-bitrate mp4 chosen", twitter._best_video_url(item) == "hi.mp4")
    check("no variants -> url", twitter._best_video_url({"url": "only.mp4"}) == "only.mp4")
    check("caption shape", twitter.caption({"author": "x", "text": "hi"}) == "**@x**\nhi")


def _run(fn):
    failures.clear()
    result = fn()
    if asyncio.iscoroutine(result):
        asyncio.run(result)
    assert not failures, failures


def test_headline():
    _run(headline_tests)


def test_caption():
    _run(caption_tests)


def test_migration():
    _run(migration_tests)


def test_x():
    _run(x_tests)


if __name__ == "__main__":
    for fn in (headline_tests, caption_tests, migration_tests, x_tests):
        result = fn()
        if asyncio.iscoroutine(result):
            asyncio.run(result)
    print("\nRESULT:", "ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}")
