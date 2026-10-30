"""Download a TikTok and post it to a Discord channel, handling Discord's upload cap.

If the file is over the cap we try an ffmpeg re-encode to a lower bitrate; if it
still won't fit (or compression is disabled), we fall back to posting the link so
nothing is ever silently dropped.
"""
import asyncio
import os
import re
import shutil

import discord

from src import tiktok
from src.log import get_logger

log = get_logger(__name__)

# Posts with no video stream (e.g. photo slideshows) come down as audio — we don't
# repost those yet, so we link them instead of uploading an audio file.
_AUDIO_EXTS = {"m4a", "mp3", "aac", "opus", "ogg", "wav"}


def _size_mb(path):
    return os.path.getsize(path) / (1024 * 1024)


# yt-dlp's stand-in title for a TikTok with no description. On a story it read as
# "TikTok video #…", which made stories look like ordinary videos — drop it.
_PLACEHOLDER_TITLE = re.compile(r"TikTok video #\d+", re.IGNORECASE)


def _caption(info):
    uploader = info.get("uploader") or ""
    title = (info.get("title") or "").strip()
    if _PLACEHOLDER_TITLE.fullmatch(title):
        title = ""
    head = f"**@{uploader}**" if uploader else ""
    if title:
        if len(title) > 280:
            title = title[:277] + "..."
        head = f"{head}\n{title}" if head else title
    return head.strip()


async def _ffprobe_duration(path):
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=nw=1:nk=1", path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
        return float(out.decode().strip())
    except Exception:
        if proc and proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        return None


async def _compress(src_path, target_mb, timeout=120):
    """Re-encode src_path to roughly fit target_mb. Returns new path or None."""
    duration = await _ffprobe_duration(src_path)
    if not duration or duration <= 0:
        return None

    audio_kbps = 128
    # leave ~10% headroom for container overhead
    total_kbps = (target_mb * 8 * 1024) / duration * 0.90
    video_kbps = max(int(total_kbps - audio_kbps), 150)

    out_path = os.path.splitext(src_path)[0] + "_c.mp4"
    cmd = [
        "ffmpeg", "-y", "-i", src_path,
        "-c:v", "libx264", "-b:v", f"{video_kbps}k",
        "-maxrate", f"{int(video_kbps * 1.3)}k", "-bufsize", f"{video_kbps * 2}k",
        "-preset", "veryfast",
        "-c:a", "aac", "-b:a", f"{audio_kbps}k",
        "-movflags", "+faststart",
        out_path,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
    )
    try:
        _, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        log.warning("ffmpeg compress timed out after %ss", timeout)
        return None
    if proc.returncode != 0 or not os.path.exists(out_path):
        log.warning("ffmpeg compress failed: %s", (err.decode()[-400:] if err else ""))
        return None
    return out_path


def _cleanup(*paths):
    for p in paths:
        if p and os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass


async def send_video(channel, file_path, content, source_url, cfg):
    """Send file_path to channel, compressing/falling-back as needed. Returns True if a file was sent."""
    max_mb = float(cfg.get("max_upload_mb", 10))
    send_path = file_path
    compressed = None

    if _size_mb(file_path) > max_mb:
        if cfg.get("compress_oversize", True):
            log.info("video %.1fMB > %.1fMB cap, compressing", _size_mb(file_path), max_mb)
            compressed = await _compress(file_path, max_mb, float(cfg.get("compress_timeout", 120)))
            send_path = compressed if (compressed and _size_mb(compressed) <= max_mb) else None
        else:
            send_path = None

    if send_path:
        try:
            await channel.send(content=content or None, file=discord.File(send_path))
            _cleanup(compressed)
            return True
        except discord.HTTPException as e:
            log.warning("upload failed (%s); falling back to link", e)

    _cleanup(compressed)
    fallback = f"{content}\n{source_url}".strip() if content else source_url
    await channel.send(content=fallback)
    return False


def _is_video(info):
    ext = (info.get("ext") or "").lower()
    vcodec = (info.get("vcodec") or "").lower()   # "" = unknown -> trust the extension
    return ext not in _AUDIO_EXTS and vcodec != "none"


async def _handle_photos(channel, url, cfg, cookies, workdir, proxy):
    """Post every image of a TikTok photo/slideshow post (batched, <=10 per message)."""
    from src.instagram_post import post_media  # lazy: instagram_post imports this module
    try:
        res = await asyncio.to_thread(
            tiktok.download_photos, url, workdir, cookies, proxy,
            int(cfg.get("tiktok_download_timeout", 180)))
    except tiktok.LiveContentError:
        raise
    except Exception as e:
        log.warning("photo post download failed for %s (%s); posting link instead", url, e)
        await channel.send(content=url)
        return False
    scratch = []
    try:
        caption = _caption({"uploader": res.get("uploader"), "title": res.get("title")})
        return await post_media(channel, res["media"], caption, cfg, scratch,
                                link_fallback=res.get("webpage_url") or url)
    finally:
        shutil.rmtree(res["dir"], ignore_errors=True)
        _cleanup(*scratch)


async def handle_url(channel, url, cfg, cookies, workdir, proxy=None):
    """Download `url` and post it to `channel`. Cleans up the temp file afterwards.

    Videos go through yt-dlp. Photo/slideshow posts (which yt-dlp can't download) go
    through _handle_photos -- either up front for /photo/ links, or as a retry when a
    short link turns out to be a photo post.
    """
    if tiktok.is_photo_url(url):
        return await _handle_photos(channel, url, cfg, cookies, workdir, proxy)

    max_mb = float(cfg.get("max_upload_mb", 10))
    native = bool(cfg.get("tiktok_native", True))
    try:
        info = await asyncio.to_thread(
            tiktok.download, url, workdir, cookies, max_mb, proxy, native,
            int(cfg.get("tiktok_download_timeout", 180)), int(cfg.get("max_download_mb", 300)))
    except tiktok.LiveContentError:
        raise
    except Exception as e:
        msg = str(e).lower()
        # A photo post behind a short link: yt-dlp either finds "no video formats", or
        # follows the redirect to /photo/<id>?_r=1&_t=... and calls that URL unsupported.
        if "no video formats" in msg or ("unsupported url" in msg and tiktok.is_photo_url(msg)):
            return await _handle_photos(channel, url, cfg, cookies, workdir, proxy)
        raise

    path = info.get("path")
    try:
        if not _is_video(info):
            # audio-only result == a slideshow: post its images, not an audio file
            return await _handle_photos(channel, info.get("webpage_url") or url, cfg,
                                        cookies, workdir, proxy)
        return await send_video(channel, path, _caption(info), info["webpage_url"], cfg)
    finally:
        _cleanup(path)
