"""Thin wrapper around gallery-dl for Instagram.

Why gallery-dl (not yt-dlp): Instagram walls almost everything behind a login,
and gallery-dl is the most reliable tool for it. It pulls **original/native
resolution** photos AND videos, and it understands carousels (a single post that
holds multiple images/videos) — downloading every item in one go.

One job here:
  * download(url) -> grab every media file in the post into a fresh temp dir and
    return their paths plus a little metadata (username/caption) for the caption.

Cookies: Instagram blocks most content for logged-out clients. Point IG_COOKIES at
a Netscape-format cookies.txt exported from a logged-in (ideally burner) account to
make downloads reliable. Without cookies only the occasional public post works, and
Instagram rate-limits an anonymous client quickly.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

import requests

# Matches instagram.com post/reel/tv/story/profile links (incl. share-suffixed ones).
_URL_RE = re.compile(r"https?://(?:www\.)?instagram\.com/[^\s<>()]+", re.IGNORECASE)

# What we treat as postable media (everything else gallery-dl writes — .json sidecars,
# .txt — is metadata we read then ignore). Note: .m4a is intentionally absent — see the
# `videos=merged` note below; we never want to post a bare audio stream.
_MEDIA_EXTS = {".mp4", ".mov", ".webm", ".mkv", ".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic"}
_VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv"}

# Instagram serves *lower*-quality video to non-Chrome clients (gallery-dl warns about
# this), so pin a desktop-Chrome UA to get native-resolution video.
_CHROME_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def find_links(text):
    """Return all Instagram URLs found in a string (used for chat auto-detect)."""
    return _URL_RE.findall(text or "")


# Only a single post/reel is downloadable. A LIVE room never ends, and a bare profile
# link makes gallery-dl walk the WHOLE account until the timeout fires -- both looked
# like "the bot hangs forever" in practice.
_POST_URL_RE = re.compile(
    r"instagram\.com/(?:[^/\s]+/)?(?:p|reel|reels|tv)/[A-Za-z0-9_-]+", re.IGNORECASE)
_IG_LIVE_RE = re.compile(r"instagram\.com/(?:[^/\s]+/)?live(?:[/?#]|$)", re.IGNORECASE)
# Story links gallery-dl fetches directly: one item (/stories/<user>/<id>/), a user's
# current set (/stories/<user>/) or a highlight (/stories/highlights/<id>/, or the
# /s/aGlnaGxpZ2h0... share link). Never /stories/me/ — that's the burner's whole tray.
_STORY_URL_RE = re.compile(
    r"instagram\.com/(?:stories/(?:highlights/\d+|(?!(?:me|highlights)(?:[/?#]|$))[A-Za-z0-9._]+(?:/\d+)?)"
    r"|s/aGlnaGxpZ2h0)", re.IGNORECASE)


def is_story_url(url):
    return bool(_STORY_URL_RE.search(url or ""))


def is_downloadable_url(url):
    """True for single Instagram post/reel/tv links and story/highlight links (not
    live/profile)."""
    u = (url or "").strip()
    if not u or _IG_LIVE_RE.search(u):
        return False
    return bool(_POST_URL_RE.search(u) or is_story_url(u))


def normalize_username(name):
    """'@Foo/ ' -> 'foo'."""
    return (name or "").strip().lstrip("@").strip().strip("/").lower()


# Instagram handles: 1-30 letters, digits, '_' or '.'.
_USERNAME_RE = re.compile(r"[a-z0-9._]{1,30}")


def is_valid_username(name):
    """False for anything that can't be an Instagram handle — e.g. a Discord mention
    picked from the @ menu (see tiktok.is_valid_username)."""
    return bool(_USERNAME_RE.fullmatch(normalize_username(name)))


def list_recent(username, count=3, cookies=None, proxy=None, timeout=60):
    """List an account's most-recent POSTS without downloading media (for monitoring).

    Uses `gallery-dl -j` (metadata-only dump) and de-dupes carousel items down to one
    entry per post. Returns [{"id": shortcode, "url": post_url}, ...], **genuinely
    newest-first** (sorted by post date). Needs cookies (Instagram requires login to
    view a profile). May raise on network/rate-limit errors — callers handle that.

    NOTE: Instagram's `/posts/` grid puts **pinned posts first**, which are usually NOT
    the newest ones. The monitor's dedup assumes recent[0] is the true newest and stops
    at the last-seen marker, so returning grid order breaks forwarding (a pinned post at
    index 0 makes it think nothing is new). We therefore (a) fetch a few extra posts so
    pins don't crowd real ones out of the window, and (b) sort the result by post date.
    """
    username = normalize_username(username)
    # Instagram allows up to 3 pinned posts; fetch that many extra so the newest `count`
    # *real* posts still make it into the window even when pins sit at the front.
    fetch = int(count) + 3
    # /posts/ (not the bare profile, which only *queues* the posts page): -j here
    # dumps per-item metadata carrying post_shortcode.
    url = f"https://www.instagram.com/{username}/posts/"
    cmd = [sys.executable, "-m", "gallery_dl", "-j", "--no-download",
           "-o", f"extractor.instagram.max-posts={fetch}",
           "-o", f"extractor.instagram.user-agent={_CHROME_UA}"]
    if cookies:
        cmd += ["--cookies", cookies]
    if proxy:
        cmd += ["--proxy", proxy]
    cmd += ["--", url]

    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    try:
        data = json.loads(proc.stdout or "[]")
    except ValueError:
        raise RuntimeError((proc.stderr or "gallery-dl produced no JSON").strip()[-300:])

    out, seen = [], set()
    for entry in data:
        # gallery-dl -j yields [type, url/data, metadata_dict] tuples; the trailing
        # dict (when present) carries post_shortcode/post_url.
        meta = None
        if isinstance(entry, list) and entry and isinstance(entry[-1], dict):
            meta = entry[-1]
        elif isinstance(entry, dict):
            meta = entry
        if not meta:
            continue
        code = meta.get("post_shortcode") or meta.get("shortcode")
        if not code or code in seen:
            continue
        seen.add(code)
        # `date` is "YYYY-MM-DD HH:MM:SS" (fixed width → lexical sort == chronological).
        # Missing date sorts last under reverse=True (empty string < any real date).
        out.append({"id": str(code),
                    "url": meta.get("post_url") or f"https://www.instagram.com/p/{code}/",
                    "_date": str(meta.get("date") or meta.get("post_date") or "")})
    # Newest-first by actual post date, then drop the pinned-post padding.
    out.sort(key=lambda p: p["_date"], reverse=True)
    for p in out:
        del p["_date"]
    return out[:int(count)]


def is_video(path):
    return os.path.splitext(path)[1].lower() in _VIDEO_EXTS


def _read_metadata(work_dir):
    """gallery-dl --write-metadata drops a <file>.json next to each media file.
    Read the first one we find for the post's username + caption."""
    for name in sorted(os.listdir(work_dir)):
        if not name.lower().endswith(".json"):
            continue
        try:
            with open(os.path.join(work_dir, name), "r", encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, ValueError):
            continue
        username = meta.get("username") or meta.get("owner_username") or ""
        caption = meta.get("description") or meta.get("caption") or ""
        return {"username": username, "caption": caption}
    return {"username": "", "caption": ""}


def download(url, dest_dir, cookies=None, proxy=None, timeout=180, max_files=20):
    """Download every media file in an Instagram post into a fresh temp dir.

    Returns a dict:
      {"dir": <temp dir to clean up>, "media": [paths...], "username": str,
       "caption": str, "webpage_url": url}

    `media` is empty if Instagram returned nothing (private post, bad/expired link,
    or — most commonly — no/invalid cookies). Callers surface that to the user.
    """
    os.makedirs(dest_dir, exist_ok=True)
    work = tempfile.mkdtemp(prefix="ig_", dir=dest_dir)

    # --directory (NOT --destination): write files flat into `work` with no
    #   extractor subfolders, so the os.listdir below actually finds them.
    # Default video handling (videos=true): for posts exposing a DASH manifest this
    #   pulls the full *original*-resolution video and merges audio+video into one
    #   MP4 via yt-dlp+ffmpeg (both in the image) — native res (e.g. 1080x1440).
    #   The `merged` option would instead grab a lower-res progressive stream
    #   (e.g. 720x960), so we deliberately do NOT set it.
    # user-agent: a desktop-Chrome UA, or Instagram serves lower-quality video.
    # Invoke via `python -m gallery_dl` (not the bare `gallery-dl` script) so it
    #   resolves from the same environment regardless of PATH.
    cmd = [sys.executable, "-m", "gallery_dl", "--quiet", "--no-part",
           "--directory", work, "--write-metadata",
           "-o", f"extractor.instagram.user-agent={_CHROME_UA}"]
    if cookies:
        cmd += ["--cookies", cookies]
    if proxy:
        # Route through a proxy so Instagram sees the proxy IP, not the host's.
        cmd += ["--proxy", proxy]
    cmd += ["--", url]

    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout,
    )

    media = sorted(
        os.path.join(work, n) for n in os.listdir(work)
        if os.path.splitext(n)[1].lower() in _MEDIA_EXTS
    )
    meta = _read_metadata(work)

    return {
        "dir": work,
        "media": media[:max_files],
        "username": meta["username"],
        "caption": meta["caption"],
        "webpage_url": url,
        "returncode": proc.returncode,
        "stderr": (proc.stderr or "").strip()[-400:],
    }


# --- cookie-free fallback for PUBLIC posts -----------------------------------
# Instagram walls posts behind a login, so gallery-dl returns nothing without valid
# cookies. As a best-effort fallback we ask an "InstaFix"-style public mirror, which
# keeps its own session: on a crawler/bot User-Agent it 302-redirects `/p/<code>/`
# straight to the real Instagram CDN file. Works for PUBLIC posts only; a carousel
# comes back as its first item; private posts still can't be fetched.
_SHORTCODE_RE = re.compile(r"instagram\.com/(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)", re.IGNORECASE)
_BOT_UA = "TelegramBot (like TwitterBot)"
_PROXY_HOSTS = ("kkinstagram.com", "ddinstagram.com", "vxinstagram.com")
_CT_EXT = {
    "video/mp4": ".mp4", "video/webm": ".webm", "video/quicktime": ".mov",
    "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/heic": ".heic",
}


def _fetch_proxy_media(host, code, work, timeout):
    """GET one mirror and, if it hands back an actual media file, save it. Returns a
    list with the saved path, or [] if this host didn't yield media (down, HTML 'open
    in app' page, private post, unknown content-type, or a suspiciously tiny body)."""
    url = f"https://{host}/p/{code}/"
    with requests.get(url, headers={"User-Agent": _BOT_UA}, timeout=timeout,
                      stream=True, allow_redirects=True) as r:
        ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        ext = _CT_EXT.get(ctype)
        if r.status_code != 200 or not ext:
            return []
        path = os.path.join(work, f"{code}{ext}")
        with open(path, "wb") as f:
            for chunk in r.iter_content(65536):
                f.write(chunk)
    if os.path.getsize(path) < 1024:   # too small to be real media — treat as an error page
        os.remove(path)
        return []
    return [path]


def download_via_proxy(url, dest_dir, timeout=60):
    """Cookie-free fallback download for a PUBLIC Instagram post.

    Returns the same shape as `download()` ({"dir","media","username","caption",
    "webpage_url"}); `media` is empty if no mirror could deliver the file.
    """
    m = _SHORTCODE_RE.search(url or "")
    if not m:
        return {"dir": None, "media": [], "username": "", "caption": "", "webpage_url": url}
    code = m.group(1)
    os.makedirs(dest_dir, exist_ok=True)
    work = tempfile.mkdtemp(prefix="ig_px_", dir=dest_dir)

    media = []
    for host in _PROXY_HOSTS:
        try:
            media = _fetch_proxy_media(host, code, work, timeout)
        except Exception:
            media = []
        if media:
            break
    return {"dir": work, "media": media, "username": "", "caption": "",
            "webpage_url": url, "via": "proxy"}


_DIGITS_RE = re.compile(r"(\d{6,})")


def list_stories(username, cookies=None, proxy=None, timeout=60):
    """List an account's CURRENT story items without downloading (cheap check).

    Returns [{"id": media_id}, ...] newest-first (id is the numeric media pk, which
    is time-ordered). Empty if the account has no active story (or isn't viewable).
    Stories require login and the burner must be able to see them.
    """
    username = normalize_username(username)
    url = f"https://www.instagram.com/stories/{username}/"
    cmd = [sys.executable, "-m", "gallery_dl", "-j", "--no-download",
           "-o", f"extractor.instagram.user-agent={_CHROME_UA}"]
    if cookies:
        cmd += ["--cookies", cookies]
    if proxy:
        cmd += ["--proxy", proxy]
    cmd += ["--", url]

    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    try:
        data = json.loads(proc.stdout or "[]")
    except ValueError:
        raise RuntimeError((proc.stderr or "gallery-dl produced no JSON").strip()[-300:])

    ids, seen = [], set()
    for entry in data:
        meta = entry[-1] if isinstance(entry, list) and entry and isinstance(entry[-1], dict) else None
        if not meta:
            continue
        mid = meta.get("media_id")
        if not mid or str(mid) in seen:
            continue
        seen.add(str(mid))
        ids.append({"id": str(mid)})
    ids.sort(key=lambda x: int(x["id"]), reverse=True)   # newest-first
    return ids


def download_stories(username, dest_dir, cookies=None, proxy=None, timeout=180, max_items=30):
    """Download an account's CURRENT story items (the whole set — Instagram has no
    per-item story URL). Returns {"dir", "username", "items": [{"id", "path"}, ...]}
    newest-first. Callers pick which items are new and clean up "dir" afterwards.
    """
    username = normalize_username(username)
    os.makedirs(dest_dir, exist_ok=True)
    work = tempfile.mkdtemp(prefix="ig_story_", dir=dest_dir)
    url = f"https://www.instagram.com/stories/{username}/"
    cmd = [sys.executable, "-m", "gallery_dl", "--quiet", "--no-part",
           "--directory", work, "--write-metadata",
           "-o", f"extractor.instagram.user-agent={_CHROME_UA}"]
    if cookies:
        cmd += ["--cookies", cookies]
    if proxy:
        cmd += ["--proxy", proxy]
    cmd += ["--", url]

    subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

    items = []
    for name in os.listdir(work):
        if os.path.splitext(name)[1].lower() not in _MEDIA_EXTS:
            continue
        story_id = None
        sidecar = os.path.join(work, name + ".json")
        try:
            with open(sidecar, "r", encoding="utf-8") as f:
                meta = json.load(f)
            story_id = meta.get("media_id") or meta.get("id")
        except (OSError, ValueError):
            pass
        if not story_id:
            # Fallback: gallery-dl normally names story media by its media id.
            m = _DIGITS_RE.match(name)
            story_id = m.group(1) if m else None
        if not story_id:
            continue
        items.append({"id": str(story_id), "path": os.path.join(work, name)})
    items.sort(key=lambda x: int(x["id"]), reverse=True)   # newest-first
    return {"dir": work, "username": username, "items": items[:max_items]}


if __name__ == "__main__":
    # Local smoke test:  python -m src.instagram <instagram_url> [cookies.txt]
    import sys

    try:  # make emoji-laden captions printable on Windows' cp1252 console
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    if len(sys.argv) < 2:
        print("usage: python -m src.instagram <instagram_url> [cookies.txt]")
        raise SystemExit(1)
    cookies = sys.argv[2] if len(sys.argv) > 2 else None
    res = download(sys.argv[1], os.path.join("data", "downloads"), cookies=cookies)
    res["sizes_mb"] = [round(os.path.getsize(p) / (1024 * 1024), 2) for p in res["media"]]
    print(json.dumps({k: v for k, v in res.items() if k != "dir"}, indent=2, ensure_ascii=False))
