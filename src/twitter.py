"""X / Twitter media for on-demand downloads (`/x get` and pasted links).

Engine: the public fxtwitter API (`api.fxtwitter.com/status/<id>`) — the same service
Tweetcord uses for its embeds. It needs no login, so there is no burner account to get
flagged, and it hands out the ORIGINAL files: photos as `pbs.twimg.com/...?name=orig`,
videos as a list of mp4 variants (we take the highest bitrate). Verified from the server
2026-10-30: 1284x1712 photos and a native 668x1188 video.

Only the tweet's own media is fetched (not a quoted tweet's). A tweet without media
returns no files; callers decide what to do with that.
"""
import os
import re
import tempfile

import requests

# x.com / twitter.com plus the common embed-fixer mirrors people paste.
_URL_RE = re.compile(
    r"https?://(?:www\.|mobile\.)?(?:x|twitter|fxtwitter|vxtwitter|fixupx|fixvx)\.com/"
    r"(?:i/web/|i/|[A-Za-z0-9_]{1,15}/)?status(?:es)?/(\d+)[^\s<>()]*",
    re.IGNORECASE,
)
_API = "https://api.fxtwitter.com/status/{}"
_UA = {"User-Agent": "discord-media-downloader (+https://github.com/QDarkRex/discord-media-downloader)"}
_EXT = {"photo": ".jpg", "video": ".mp4", "gif": ".mp4"}


def find_links(text):
    """[(url, tweet_id), ...] for every distinct status link in `text`, in order."""
    out, seen = [], set()
    for m in _URL_RE.finditer(text or ""):
        if m.group(1) not in seen:
            seen.add(m.group(1))
            out.append((m.group(0), m.group(1)))
    return out


def tweet_id(url):
    m = _URL_RE.search(url or "")
    return m.group(1) if m else None


def _best_video_url(item):
    variants = [v for v in (item.get("variants") or [])
                if "mp4" in (v.get("content_type") or v.get("url") or "")]
    if variants:
        return max(variants, key=lambda v: v.get("bitrate") or 0)["url"]
    return item.get("url")


def fetch(tid, timeout=30):
    """The fxtwitter `tweet` object. Raises on a missing/protected/deleted tweet."""
    r = requests.get(_API.format(tid), headers=_UA, timeout=timeout)
    data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code != 200 or data.get("code") != 200 or not data.get("tweet"):
        raise RuntimeError(f"tweet {tid} unavailable ({data.get('message') or r.status_code})")
    return data["tweet"]


def download(tid, workdir, timeout=180, max_files=20, max_mb=300):
    """Download a tweet's media into a fresh temp dir.

    Returns {"dir", "media": [paths], "author", "name", "text", "url"}. The caller
    removes "dir". Each file is capped at max_mb so a huge video can't fill the disk.
    """
    tweet = fetch(tid)
    os.makedirs(workdir, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="x_", dir=workdir)
    media, cap = [], max_mb * 1024 * 1024
    for i, item in enumerate(((tweet.get("media") or {}).get("all") or [])[:max_files]):
        kind = item.get("type")
        url = _best_video_url(item) if kind in ("video", "gif") else item.get("url")
        if not url:
            continue
        path = os.path.join(tmp, f"{tid}_{i + 1}{_EXT.get(kind, '')}")
        with requests.get(url, headers=_UA, stream=True, timeout=timeout) as r:
            r.raise_for_status()
            size = 0
            with open(path, "wb") as f:
                for chunk in r.iter_content(1 << 16):
                    size += len(chunk)
                    if size > cap:
                        raise RuntimeError(f"tweet {tid} item {i + 1} is over {max_mb} MB")
                    f.write(chunk)
        media.append(path)
    author = tweet.get("author") or {}
    return {"dir": tmp, "media": media, "author": author.get("screen_name") or "",
            "name": author.get("name") or "", "text": tweet.get("text") or "",
            "url": tweet.get("url") or f"https://x.com/i/status/{tid}"}


def caption(info):
    """'**@user**\\n<text>' — same shape as the TikTok/Instagram captions."""
    head = f"**@{info['author']}**" if info.get("author") else ""
    text = (info.get("text") or "").strip()
    if len(text) > 280:
        text = text[:277] + "..."
    return f"{head}\n{text}".strip() if text else head
