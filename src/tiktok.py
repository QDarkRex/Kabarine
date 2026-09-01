"""Thin wrappers around yt-dlp for TikTok.

Two jobs:
  * list_recent(username)  -> cheap, flat listing of recent video ids (for monitoring)
  * download(url)          -> full, watermark-free MP4 download (for posting)

yt-dlp pulls the raw file from TikTok's CDN, which has no watermark (the watermark
only exists as an overlay in TikTok's own player), so no extra work is needed.
"""
import os
import re
import time

import yt_dlp

# Matches tiktok.com links incl. short forms (vm./vt./m.) used by the share button.
_URL_RE = re.compile(r"https?://(?:www\.|vm\.|vt\.|m\.)?tiktok\.com/[^\s<>()]+", re.IGNORECASE)


def find_links(text):
    """Return all TikTok URLs found in a string (used for chat auto-detect)."""
    return _URL_RE.findall(text or "")


class LiveContentError(RuntimeError):
    """The link is a LIVE room (or the download ran away).

    A live stream never ends, so yt-dlp would keep pulling it forever: the bot looks
    stuck on "typing...", the worker thread leaks (a thread started by
    asyncio.to_thread cannot be cancelled from outside), and the growing file fills
    the disk. We refuse such links instead of trying.
    """


_LIVE_RE = re.compile(r"tiktok\.com/(?:@[^/\s]+/)?live(?:[/?#]|$)", re.IGNORECASE)
_POST_RE = re.compile(r"tiktok\.com/(?:@[^/\s]+/)?(?:video|photo)/\d+", re.IGNORECASE)
_SHORT_RE = re.compile(r"https?://(?:vm|vt|m)\.tiktok\.com/|tiktok\.com/t/", re.IGNORECASE)


def is_live_url(url):
    """True for TikTok LIVE room links."""
    return bool(_LIVE_RE.search(url or ""))


def is_downloadable_url(url):
    """True only for links that are ONE finished post we can actually download.

    Rejects live rooms, bare profiles, /tag/ and /search links. Short share links
    (vm./vt./tiktok.com/t/) pass because their real target is only known after
    resolving -- download() still refuses if it turns out to be live.
    """
    u = (url or "").strip()
    if not u or is_live_url(u):
        return False
    return bool(_POST_RE.search(u) or _SHORT_RE.search(u))


def _is_live_info(info):
    info = info or {}
    return (bool(info.get("is_live"))
            or (info.get("live_status") or "") in ("is_live", "is_upcoming", "post_live"))


def normalize_username(name):
    """'@Foo ' -> 'foo'."""
    return (name or "").strip().lstrip("@").strip().lower()


class _QuietLogger:
    """Swallow yt-dlp's own stdout/stderr chatter — we surface failures via raised
    exceptions, so its ERROR prints would just be duplicate noise in the logs."""

    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


def _base_opts(cookies=None, proxy=None):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 30,   # don't hang forever on a dead connection
        "logger": _QuietLogger(),
    }
    if cookies:
        opts["cookiefile"] = cookies
    if proxy:
        # Route through a proxy so TikTok sees the proxy IP, not the host's.
        opts["proxy"] = proxy
    return opts


def list_recent(username, count=5, cookies=None, proxy=None):
    """Flat-list a profile's recent videos WITHOUT downloading.

    Returns a list of {"id", "url"} dicts, most-recent first. May raise on
    network / extraction errors — callers handle that.
    """
    username = normalize_username(username)
    url = f"https://www.tiktok.com/@{username}"
    opts = _base_opts(cookies, proxy)
    opts.update({
        "extract_flat": True,
        "playlist_items": f"1:{count}",
        "skip_download": True,
    })
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    entries = (info or {}).get("entries") or []
    out = []
    for e in entries:
        vid = e.get("id")
        if not vid:
            continue
        link = e.get("url") or f"https://www.tiktok.com/@{username}/video/{vid}"
        out.append({"id": str(vid), "url": link})
    return out


def _video_format(cap_mb, native=True):
    """Build a yt-dlp format selector.

    TikTok offers the same clip as h264 and h265 (bytevc1), plus a watermarked
    variant whose format_id is literally "download" (always excluded by id).

    native=True  -> highest resolution regardless of codec (format_sort tie-breaks
                    to h264 when a codec choice exists at the top resolution). On
                    TikTok the 1080p rendition is often h265-only, so this is what
                    delivers native quality — but h265 may not inline-preview on
                    every Discord client (same trade-off as Instagram's native VP9).
    native=False -> force h264 so Discord always plays it inline, even though that
                    can cap TikTok at 720p.
    """
    if native:
        return "b[format_id!=download]/b"
    cap = int(cap_mb)
    return (
        f"b[vcodec^=h264][filesize<{cap}M][format_id!=download]"  # best h264 that fits, no watermark
        f"/b[vcodec^=h264][format_id!=download]"                  # else best h264 (compress later)
        f"/b[filesize<{cap}M][format_id!=download]"               # else best non-watermark that fits
        f"/b[format_id!=download]"                                # else best non-watermark
        f"/b"                                                     # absolute fallback
    )


def download(url, dest_dir, cookies=None, max_mb=10, proxy=None, native=True,
             timeout=180, max_download_mb=300):
    """Download one video (watermark-free). Returns metadata incl. local 'path'.

    With native=True picks the highest-resolution rendition (see _video_format). For
    photo/slideshow posts (no video stream) yt-dlp yields audio only — callers detect
    that via the returned 'ext'/'vcodec' and fall back to posting the link.

    Raises LiveContentError if the link is a live stream, or if the download blows
    past `timeout` seconds / `max_download_mb`. The progress hook enforces both from
    INSIDE the worker thread -- the only way to actually stop yt-dlp here.
    """
    os.makedirs(dest_dir, exist_ok=True)
    opts = _base_opts(cookies, proxy)
    opts.update({
        "format": _video_format(max_mb, native),
        "format_sort": ["res", "vcodec:h264"],  # highest res, tie-break to h264
        "outtmpl": os.path.join(dest_dir, "%(id)s.%(ext)s"),
        "restrictfilenames": True,
        "overwrites": True,
    })
    deadline = time.monotonic() + float(timeout)
    limit_bytes = int(max_download_mb) * 1024 * 1024

    def _guard(d):
        if time.monotonic() > deadline:
            raise LiveContentError(
                "download still running after %ss - endless stream?" % timeout)
        if (d.get("downloaded_bytes") or 0) > limit_bytes:
            raise LiveContentError("download exceeded %s MB" % max_download_mb)

    opts["progress_hooks"] = [_guard]

    with yt_dlp.YoutubeDL(opts) as ydl:
        # Probe first: a live room must never reach the downloader.
        if _is_live_info(ydl.extract_info(url, download=False)):
            raise LiveContentError("this link is a live stream, not a video")
        info = ydl.extract_info(url, download=True)
        path = ydl.prepare_filename(info)

    return {
        "path": path,
        "id": str(info.get("id") or ""),
        "title": info.get("title") or "",
        "uploader": info.get("uploader") or info.get("uploader_id") or "",
        "webpage_url": info.get("webpage_url") or url,
        "duration": info.get("duration"),
        "ext": (info.get("ext") or "").lower(),
        "vcodec": (info.get("vcodec") or "none"),
    }


if __name__ == "__main__":
    # Local smoke test:  python -m src.tiktok <tiktok_url>
    import json
    import sys

    try:  # make emoji-laden titles printable on Windows' cp1252 console
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    if len(sys.argv) < 2:
        print("usage: python -m src.tiktok <tiktok_url>")
        raise SystemExit(1)
    res = download(sys.argv[1], os.path.join("data", "downloads"))
    res["size_mb"] = round(os.path.getsize(res["path"]) / (1024 * 1024), 2)
    print(json.dumps(res, indent=2, ensure_ascii=False))
