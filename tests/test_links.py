"""Link parsing: /tt add taking a video link, and short links that land on a photo post."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src import discord_post, tiktok  # noqa: E402


def test_post_link():
    assert tiktok.post_link("https://www.tiktok.com/@IndahJKT48/video/7562?is_from_webapp=1") == \
        ("indahjkt48", "https://www.tiktok.com/@indahjkt48/video/7562")
    assert tiktok.post_link("https://www.tiktok.com/@jalur5/photo/7561?_r=1&_t=x") == \
        ("jalur5", "https://www.tiktok.com/@jalur5/video/7561")
    assert tiktok.post_link("indahjkt48") is None
    assert tiktok.post_link("https://vt.tiktok.com/ZSabc/") is None   # short link: no username
    assert tiktok.post_link("https://www.tiktok.com/video/7562") is None


def _route(error_text, monkeypatch):
    """Which path handle_url takes when yt-dlp fails with `error_text`."""
    seen = []

    def download(*a, **k):
        raise RuntimeError(error_text)

    async def photos(channel, url, *a, **k):
        seen.append(url)
    monkeypatch.setattr(discord_post.tiktok, "download", download)
    monkeypatch.setattr(discord_post, "_handle_photos", photos)
    try:
        asyncio.run(discord_post.handle_url(None, "https://vt.tiktok.com/ZSabc/", {}, None, "."))
    except RuntimeError:
        return "raised"
    return seen


def test_short_link_to_photo_post(monkeypatch):
    unsupported = ("ERROR: Unsupported URL: https://www.tiktok.com/@jalur5/photo/7561?_r=1&_t=ZS-9A")
    assert _route(unsupported, monkeypatch) == ["https://vt.tiktok.com/ZSabc/"]
    assert _route("ERROR: No video formats found!", monkeypatch) == ["https://vt.tiktok.com/ZSabc/"]
    assert _route("ERROR: Unsupported URL: https://example.com/x", monkeypatch) == "raised"


def test_tt_add_with_video_link(monkeypatch, tmp_path):
    """A pasted video link: ids come from that video page, the listing goes via secUid,
    and the @handle page is never tried."""
    import types

    from cogs import tracking
    from src import db
    from tests.fakes import FakeHealth

    path = str(tmp_path / "t.db")
    asyncio.run(db.init_db(path))
    bot = types.SimpleNamespace(health=FakeHealth(), db_path=path, cfg={}, cookies=None, proxy=None)
    calls, replies = [], []

    def resolve_ids(username, cookies, proxy, video_url=None):
        calls.append(("ids", username, video_url))
        return "6900", "MS4wSEC"

    def list_recent(username, count, cookies, proxy, sec_uid=None):
        calls.append(("list", username, sec_uid))
        return [{"id": "7562", "url": "u"}]
    monkeypatch.setattr(tracking.tiktok_story, "resolve_ids", resolve_ids)
    monkeypatch.setattr(tracking.tiktok, "list_recent", list_recent)

    async def defer(**k):
        pass

    async def send(text, **k):
        replies.append(text)
    interaction = types.SimpleNamespace(
        response=types.SimpleNamespace(defer=defer), followup=types.SimpleNamespace(send=send),
        channel=types.SimpleNamespace(id=5, mention="#c"), guild_id=9)
    cog = tracking.Tracking(bot)
    asyncio.run(tracking.Tracking.add.callback(
        cog, interaction, "https://www.tiktok.com/@IndahJKT48/video/7562?_r=1", None, None, None))
    assert calls == [("ids", "indahjkt48", "https://www.tiktok.com/@indahjkt48/video/7562"),
                     ("list", "indahjkt48", "MS4wSEC")], calls
    assert "@indahjkt48" in replies[0] and replies[0].startswith("✅")
    rows = asyncio.run(db.get_enabled(path, "tiktok", "post"))
    assert rows == [("indahjkt48", "5", None, None, "7562")], rows
