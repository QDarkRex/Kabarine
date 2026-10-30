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


def test_instagram_story_links():
    from src import instagram
    ok = ["https://www.instagram.com/stories/jkt48.aralie/4018716417561479192/?utm_source=ig_story_item_share&igsh=x",
          "https://instagram.com/stories/jkt48.aralie/",
          "https://www.instagram.com/stories/highlights/17912345678901234/",
          "https://www.instagram.com/s/aGlnaGxpZ2h0OjE3OTEy?story_media_id=123&igsh=x",
          "https://www.instagram.com/p/DAbc123/",
          "https://www.instagram.com/reel/DAbc123/?igsh=x"]
    no = ["https://www.instagram.com/stories/me/",            # the burner's whole story tray
          "https://www.instagram.com/stories/highlights/",
          "https://www.instagram.com/jkt48.aralie/",          # profile: would crawl everything
          "https://www.instagram.com/jkt48.aralie/live/"]
    assert [u for u in ok if not instagram.is_downloadable_url(u)] == []
    assert [u for u in no if instagram.is_downloadable_url(u)] == []
    assert instagram.is_story_url(ok[0]) and not instagram.is_story_url(ok[4])


def test_instagram_story_caption_and_no_mirror(monkeypatch, tmp_path):
    """A story link posts as '@user — story' and never asks the /p/ mirrors."""
    from src import instagram_post
    img = tmp_path / "4018716417561479192.jpg"
    img.write_bytes(b"x" * 2048)
    sent = []

    def download(url, *a, **k):
        return {"dir": None, "media": [str(img)], "username": "jkt48.aralie", "caption": ""}

    def mirror(*a, **k):
        raise AssertionError("mirror must not be used for stories")

    async def post_media(channel, media, caption, cfg, scratch, link_fallback=None):
        sent.append((media, caption))
        return True
    monkeypatch.setattr(instagram_post.instagram, "download", download)
    monkeypatch.setattr(instagram_post.instagram, "download_via_proxy", mirror)
    monkeypatch.setattr(instagram_post, "post_media", post_media)
    url = "https://www.instagram.com/stories/jkt48.aralie/4018716417561479192/"
    assert asyncio.run(instagram_post.handle_url(None, url, {}, None, str(tmp_path)))
    assert sent == [([str(img)], "**@jkt48.aralie** — story")]


def test_instagram_expired_story(monkeypatch, tmp_path):
    """Nothing came back (story expired): no mirror attempt, and the hint says why."""
    import types

    from src import instagram_post
    texts = []

    async def send(content=None, **k):
        texts.append(content)

    def mirror(*a, **k):
        raise AssertionError("mirror must not be used for stories")
    monkeypatch.setattr(instagram_post.instagram, "download",
                        lambda *a, **k: {"dir": None, "media": [], "username": "", "caption": ""})
    monkeypatch.setattr(instagram_post.instagram, "download_via_proxy", mirror)
    url = "https://www.instagram.com/stories/jkt48.aralie/4018716417561479192/"
    assert asyncio.run(instagram_post.handle_url(types.SimpleNamespace(send=send), url, {},
                                                 None, str(tmp_path))) is False
    assert len(texts) == 1 and "24 h" in texts[0] and url in texts[0]


def test_failed_compress_leaves_no_partial_file(monkeypatch, tmp_path):
    """ffmpeg exits non-zero after writing part of the output: the _c.mp4 must go."""
    src = tmp_path / "7680535585908443925.mp4"
    src.write_bytes(b"x")
    out = tmp_path / "7680535585908443925_c.mp4"

    async def duration(path):
        return 4116.0

    class Proc:
        returncode = 1

        async def communicate(self):
            out.write_bytes(b"\x00" * 48)          # exactly what was left on the server
            return b"", b"Killed"

    async def spawn(*a, **k):
        return Proc()
    monkeypatch.setattr(discord_post, "_ffprobe_duration", duration)
    monkeypatch.setattr(discord_post.asyncio, "create_subprocess_exec", spawn)
    assert asyncio.run(discord_post._compress(str(src), 50)) is None
    assert not out.exists() and src.exists()
