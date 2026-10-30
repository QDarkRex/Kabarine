"""Unit tests for TikTok story forwarding: list_stories parsing, forward_new(by_id) and
the poller's empty-read baseline. Real SQLite, fake Discord/handle_url, fake gallery-dl."""
import asyncio
import json
import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src import db, monitor, tiktok_story  # noqa: E402
from tests.fakes import FakeHealth  # noqa: E402

failures = []


def check(name, cond, extra=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{('   ' + str(extra)) if not cond else ''}")
    if not cond:
        failures.append(name)


# ---------- list_stories parsing (gallery-dl replaced by canned output) ----------
def fake_run(stdout, stderr="", rc=0):
    def run(cmd, capture_output, text, timeout):
        fake_run.cmd = cmd
        return types.SimpleNamespace(stdout=stdout, stderr=stderr, returncode=rc)
    return run


def parse_tests():
    print("list_stories parsing")
    real = tiktok_story.subprocess.run
    try:
        items = [[2, {"id": "7700527014012390676", "author": {"id": "123"}}],
                 [3, "https://cdn/a.mp4", {"id": "7700527014012390676", "createTime": 1}],
                 [3, "https://cdn/b.jpg", {"id": "7700600000000000000"}],
                 [3, "https://cdn/c.jpg", {"id": "7700600000000000000"}]]   # photo story: 2 images, 1 id
        tiktok_story.subprocess.run = fake_run(json.dumps(items))
        out = tiktok_story.list_stories("123", "christyjkt48")
        check("items -> newest first, photo images deduped",
              [o["id"] for o in out] == ["7700600000000000000", "7700527014012390676"], out)
        check("url is the normal /video/ link",
              out[1]["url"] == "https://www.tiktok.com/@christyjkt48/video/7700527014012390676", out)
        check("numeric id in URL, retries off",
              fake_run.cmd[-1].endswith("/@123/stories") and "extractor.tiktok.retries=0" in fake_run.cmd)

        # media fetch failed after the per-story Directory message (seen live for @jkt48.levi)
        tiktok_story.subprocess.run = fake_run(json.dumps([[2, {"id": "7700470386475011329"}]]),
                                               "[tiktok][error] .../video/7700470386475011329: Failed")
        check("Directory-only story still detected when its media fetch fails",
              [o["id"] for o in tiktok_story.list_stories("123", "u")] == ["7700470386475011329"])
        tiktok_story.subprocess.run = fake_run(json.dumps([[2, {"id": "123"}]]), "Could not extract next cursor")
        check("the account's own id is never a story", tiktok_story.list_stories("123", "u") == [])

        tiktok_story.subprocess.run = fake_run("[]", "[tiktok][error] x\nAttributeError: 'TiktokStoryTimeCursor' "
                                                     "object has no attribute 'exc'\nCould not extract next cursor")
        check("no-story quirk -> []", tiktok_story.list_stories("123", "u") == [])

        tiktok_story.subprocess.run = fake_run(json.dumps([[-1, {"error": "HttpError",
                                                                  "message": "'403 Forbidden' for url"}]]))
        try:
            tiktok_story.list_stories("123", "u")
            check("403 raises", False, "no exception")
        except RuntimeError as e:
            check("403 raises with reason", "403" in str(e), e)

        tiktok_story.subprocess.run = fake_run("", "", 1)
        try:
            tiktok_story.list_stories("123", "u")
            check("silent failure raises", False, "no exception")
        except RuntimeError:
            check("silent failure raises (not mistaken for 'no story')", True)
    finally:
        tiktok_story.subprocess.run = real


# ---------- forward_new(by_id) against a real DB ----------
class FakeChannel:
    def __init__(self, cid):
        self.id = cid


async def forward_tests():
    print("forward_new by_id (stories) + post isolation")
    path = os.path.join(tempfile.mkdtemp(), "t.db")
    await db.init_db(path)
    bot = types.SimpleNamespace(health=FakeHealth(), db_path=path, cfg={}, get_channel=lambda cid: FakeChannel(cid))
    sent = []

    async def handle_url(channel, url, cfg, cookies, workdir, proxy):
        sent.append(url.rsplit("/", 1)[-1])

    U, CH = "christyjkt48", "111"
    await db.add_subscription(path, "tiktok", U, CH, "g", "post", None, None, "7600000000000000000")
    await db.add_subscription(path, "tiktok", U, CH, "g", "story", None, None, "0")

    async def subs(kind):
        return [(c, mt, mi, ls) for u, c, mt, mi, ls in await db.get_enabled(path, "tiktok", kind)]

    async def marker(kind):
        return (await subs(kind))[0][3]

    def s(*ids):
        return [{"id": i, "url": f"https://www.tiktok.com/@{U}/video/{i}"} for i in ids]

    async def fwd(stories):
        await monitor.forward_new(bot, "tiktok", U, await subs("story"), stories, handle_url,
                                  "/tmp", None, None, "TikTok", content_type="story",
                                  noun="story", by_id=True)

    await fwd(s("7700000000000000002", "7700000000000000001"))
    check("baseline 0: both forwarded oldest-first",
          sent == ["7700000000000000001", "7700000000000000002"], sent)
    check("marker -> newest", await marker("story") == "7700000000000000002", await marker("story"))
    check("post marker untouched", await marker("post") == "7600000000000000000", await marker("post"))

    sent.clear()
    await fwd(s("7700000000000000002", "7700000000000000001"))
    check("same stories again: nothing re-sent", sent == [], sent)

    sent.clear()
    await fwd(s("7700000000000000003"))   # the marked story (…002) has EXPIRED and left the list
    check("marked story expired: only the genuinely new one sent", sent == ["7700000000000000003"], sent)

    sent.clear()
    await fwd([])
    check("no stories: nothing", sent == [], sent)

    # walk-mode (posts) behaviour is unchanged
    sent.clear()
    posts = [{"id": "7600000000000000009", "url": "u/7600000000000000009"},
             {"id": "7600000000000000000", "url": "u/7600000000000000000"}]
    await monitor.forward_new(bot, "tiktok", U, await subs("post"), posts, handle_url,
                              "/tmp", None, None, "TikTok")
    check("posts (default mode) still forward by walking", sent == ["7600000000000000009"], sent)
    check("post marker advanced, story marker untouched",
          await marker("post") == "7600000000000000009" and await marker("story") == "7700000000000000003")

    # None baseline with stories present -> seed only
    await db.add_subscription(path, "tiktok", "nob", "222", "g", "story", None, None, None)
    sent.clear()
    nob = [(c, mt, mi, ls) for u, c, mt, mi, ls in await db.get_enabled(path, "tiktok", "story") if u == "nob"]
    await monitor.forward_new(bot, "tiktok", "nob", nob, s("7700000000000000005"), handle_url,
                              "/tmp", None, None, "TikTok", content_type="story", noun="story", by_id=True)
    check("None baseline + stories up: seeded, nothing dumped", sent == [], sent)

    # remove_subscription: content_type=None removes both kinds
    n = await db.remove_subscription(path, "tiktok", U, None, None)
    check("remove type=both deletes video + story rows", n == 2, n)
    await db.set_user_id(path, "x", "123")
    check("user id cache round-trip", await db.get_user_id(path, "x") == "123")


async def poller_baseline_test():
    print("story poller: empty read seeds '0'")
    from cogs import tracking   # noqa: E402  (real cog module, discord installed in venv)
    path = os.path.join(tempfile.mkdtemp(), "p.db")
    await db.init_db(path)
    await db.add_subscription(path, "tiktok", "u", "333", "g", "story", None, None, None)
    await db.set_user_id(path, "u", "999")
    bot = types.SimpleNamespace(health=FakeHealth(), db_path=path, cfg={}, cookies=None, proxy=None,
                                get_channel=lambda cid: FakeChannel(cid))
    cog = tracking.Tracking.__new__(tracking.Tracking)
    cog.bot = bot
    real = tracking.tiktok_story.list_stories
    tracking.tiktok_story.list_stories = lambda *a, **k: []
    try:
        subs = [(c, mt, mi, ls) for u, c, mt, mi, ls in await db.get_enabled(path, "tiktok", "story")]
        await cog._check_stories("u", subs)
    finally:
        tracking.tiktok_story.list_stories = real
    got = (await db.get_enabled(path, "tiktok", "story"))[0][4]
    check("None -> '0' after a successful empty read", got == "0", got)


def _run(fn):
    failures.clear()
    result = fn()
    if asyncio.iscoroutine(result):
        asyncio.run(result)
    assert not failures, failures


def test_parse():
    _run(parse_tests)


def test_forward():
    _run(forward_tests)


def test_poller_baseline():
    _run(poller_baseline_test)


if __name__ == "__main__":
    for fn in (parse_tests, forward_tests, poller_baseline_test):
        result = fn()
        if asyncio.iscoroutine(result):
            asyncio.run(result)
    print("\nRESULT:", "ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}")
