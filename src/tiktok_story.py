"""TikTok STORY listing — what makes `/tt add type:stories` possible.

yt-dlp has no TikTok story extractor (still true in 2026.10.21), which is why story
monitoring was shelved in July. gallery-dl does have one (`TiktokStoriesExtractor`,
`tiktok.com/@USER/stories`), but from the server its first step — fetching the profile
page to look up the account's numeric id — is answered 403, with or without
`browser=chrome`. gallery-dl skips that page when the "username" in the URL is already
numeric (extractor/tiktok.py `_extract_id`), so the numeric id is resolved once through
yt-dlp, which does work from the server, cached in the DB, and the stories are then
listed from `tiktok.com/@<numeric id>/stories`.

Quirk: when an account has NO active story, gallery-dl's story cursor fails with
"Could not extract next cursor" (and, in 1.32.x, an AttributeError while raising it)
instead of returning an empty list. That is an empty-but-successful answer, so it is
read as []. Anything else is a real failure and raises.

Verified from the live server 2026-10-25, anonymously: an active story listed and then
downloaded through the normal yt-dlp path at 1280p; five other accounts correctly came
back "no story"; about one second per account.

No browser is involved, unlike the Playwright watcher reverted earlier, so there is no
Chromium memory cost.
"""
import json
import subprocess
import sys

import yt_dlp

from src.tiktok import _base_opts, list_recent

# gallery-dl -j message types that carry a story's metadata: 2 = Directory (emitted per
# story BEFORE its media is fetched), 3 = Url, 6 = Queue. Reading 2 matters: if gallery-dl
# then fails to fetch that story's media (seen live for @jkt48.levi), the story is still
# detected — only its id is needed, because yt-dlp does the actual download.
_ITEM_TYPES = (2, 3, 6)
_NO_STORY_MARKERS = ("Could not extract next cursor", "has no attribute 'exc'")


def resolve_user_id(username, cookies=None, proxy=None, video_url=None):
    """The account's numeric TikTok user id, read from one of its videos.

    Pass video_url when one is already at hand (saves listing the profile again).
    """
    if not video_url:
        recent = list_recent(username, 1, cookies, proxy)
        if not recent:
            raise RuntimeError(f"@{username} has no videos to read its user id from")
        video_url = recent[0]["url"]
    with yt_dlp.YoutubeDL(_base_opts(cookies, proxy)) as ydl:
        info = ydl.extract_info(video_url, download=False)
    user_id = str((info or {}).get("uploader_id") or "")
    if not user_id.isdigit():
        raise RuntimeError(f"@{username}: video metadata has no numeric user id")
    return user_id


def list_stories(user_id, username, cookies=None, proxy=None, timeout=90):
    """Active stories, most recent first, as [{"id", "url"}]. [] means none active.

    The url is the story's ordinary /video/ link, which the existing download path
    (yt-dlp, with the photo fallback) already handles. Raises on a real failure.
    """
    cmd = [sys.executable, "-m", "gallery_dl", "-v", "-j", "--no-download",
           "-o", "extractor.tiktok.retries=0"]   # the "no story" quirk would otherwise retry for minutes
    if cookies:
        cmd += ["--cookies", cookies]
    if proxy:
        cmd += ["--proxy", proxy]
    cmd.append(f"https://www.tiktok.com/@{user_id}/stories")
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

    try:
        messages = json.loads(proc.stdout or "[]")
    except ValueError:
        messages = []

    ids, errors = set(), []
    for msg in messages:
        if not isinstance(msg, list) or len(msg) < 2:
            continue
        if msg[0] == -1 and isinstance(msg[1], dict):
            errors.append(str(msg[1].get("message") or msg[1].get("error")))
        elif msg[0] in _ITEM_TYPES:
            meta = msg[2] if len(msg) > 2 and isinstance(msg[2], dict) else msg[1]
            if not isinstance(meta, dict):
                continue
            story_id = str(meta.get("id") or "")
            # A photo story yields one item per image with the same id (deduped by the set);
            # the account's own id must never be mistaken for a story.
            if story_id.isdigit() and story_id != str(user_id):
                ids.add(story_id)

    if ids:
        # TikTok ids are time-ordered, so sorting them sorts the stories by age.
        return [{"id": i, "url": f"https://www.tiktok.com/@{username}/video/{i}"}
                for i in sorted(ids, key=int, reverse=True)]
    if any(marker in proc.stderr for marker in _NO_STORY_MARKERS):
        return []
    reason = errors[0] if errors else next(
        (line for line in reversed(proc.stderr.splitlines()) if "[error]" in line),
        f"gallery-dl exited {proc.returncode} with no stories and no recognised reason")
    raise RuntimeError(reason[:200])
