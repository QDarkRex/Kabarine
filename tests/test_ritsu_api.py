import asyncio

import pytest

from src import ig_comments, instagram_post, ritsu_api


def test_with_source_adds_small_unembedded_link():
    assert instagram_post.with_source("**@a**", "https://x.com/a/status/1") == "**@a**\n-# <https://x.com/a/status/1>"
    assert instagram_post.with_source(None, "https://x.com/a/status/1") == "-# <https://x.com/a/status/1>"
    assert instagram_post.with_source("cap", None) == "cap"
    assert instagram_post.with_source("cap", "javascript:x") == "cap"


def test_media_pk_matches_known_shortcode():
    assert ig_comments.media_pk("B") == 1
    assert ig_comments.media_pk("BA") == 64


def test_invalid_code_and_missing_session_never_call_instagram():
    assert ig_comments.fetch_comments("x", "/c.txt")["status"] == "invalid_code"
    assert ig_comments.fetch_comments("DfrTXzKmOXu", None)["status"] == "no_session"


def test_hourly_cap(monkeypatch):
    monkeypatch.setattr(ig_comments, "_recent", [__import__("time").time()] * ig_comments.HOURLY_CAP)
    with pytest.raises(ig_comments.RateLimited):
        ig_comments._wait_turn()


@pytest.mark.parametrize("header,status", [(None, 401), ("Bearer wrong", 401), ("Bearer s3cret", 200)])
def test_api_requires_bearer(monkeypatch, header, status):
    from aiohttp.test_utils import TestClient, TestServer

    monkeypatch.setenv("KABARINE_API_SECRET", "s3cret")
    monkeypatch.setattr(ig_comments, "fetch_comments", lambda code, cookies, limit: {"status": "ok", "comments": [], "code": code})

    class Bot:
        ig_cookies = "/c.txt"

    async def run():
        client = TestClient(TestServer(ritsu_api.build_app(Bot())))
        await client.start_server()
        try:
            headers = {"Authorization": header} if header else {}
            response = await client.get("/ig/comments?code=DfrTXzKmOXu", headers=headers)
            return response.status
        finally:
            await client.close()

    assert asyncio.run(run()) == status
