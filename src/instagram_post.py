"""Download an Instagram post and send its media to a Discord channel.

A post can be a single photo, a single video/reel, or a carousel (many of either).
We upload every item. Videos over Discord's upload cap get an ffmpeg re-encode
(reusing the helpers in discord_post); if a video still won't fit (or compression is
off) we post its source link instead, so nothing is ever silently dropped. Oversized
images (rare) fall back to a link too.

Kept separate from discord_post.py (the TikTok path) on purpose — the TikTok flow is
single-video; Instagram is multi-file — but it imports the shared compress helpers so
there's no duplicated ffmpeg code.
"""
import asyncio
import os
import shutil

import discord

from src import instagram
from src.discord_post import _compress, _size_mb  # shared ffmpeg helpers
from src.log import get_logger

log = get_logger(__name__)

# Discord allows up to 10 attachments per message.
_MAX_ATTACHMENTS = 10


def _caption(info):
    username = (info.get("username") or "").strip()
    caption = (info.get("caption") or "").strip()
    head = f"**@{username}**" if username else ""
    if caption:
        if len(caption) > 280:
            caption = caption[:277] + "..."
        head = f"{head}\n{caption}" if head else caption
    return head.strip()


async def _prepare(path, max_mb, cfg, scratch):
    """Return a path that fits under max_mb, or None if it can't be made to fit.

    Videos are compressed if oversized; images can't be compressed here, so an
    oversized image returns None (caller links it).
    """
    if _size_mb(path) <= max_mb:
        return path
    if not instagram.is_video(path):
        return None  # oversized image — nothing to do but link it
    if not cfg.get("compress_oversize", True):
        return None
    log.info("video %.1fMB > %.1fMB cap, compressing", _size_mb(path), max_mb)
    out = await _compress(path, max_mb, float(cfg.get("compress_timeout", 120)))
    if out:
        scratch.append(out)
        if _size_mb(out) <= max_mb:
            return out
    return None


async def _send_batch(channel, paths, content):
    """Send up to 10 files in one message; on failure, retry one-per-message."""
    try:
        await channel.send(content=content or None,
                           files=[discord.File(p) for p in paths])
        return
    except discord.HTTPException as e:
        log.warning("batch upload failed (%s); retrying one-by-one", e)
    first = True
    for p in paths:
        try:
            await channel.send(content=(content if first else None) or None,
                               file=discord.File(p))
        except discord.HTTPException as e:
            log.warning("single upload failed for %s (%s)", os.path.basename(p), e)
        first = False


async def post_media(channel, media, caption, cfg, scratch, link_fallback=None):
    """Send already-downloaded media files to `channel`, compressing oversize videos
    and batching (<=10 per message, caption on the first). Returns True if anything was
    uploaded. `scratch` collects temp compressed files for the caller to clean up.
    Items too big to upload are linked via `link_fallback` (a URL) if given, else noted.
    """
    max_mb = float(cfg.get("max_upload_mb", 10))
    sendable, linked = [], []
    for p in media:
        ready = await _prepare(p, max_mb, cfg, scratch)
        (sendable if ready else linked).append(ready or p)

    uploaded = False
    first = True
    for i in range(0, len(sendable), _MAX_ATTACHMENTS):
        await _send_batch(channel, sendable[i:i + _MAX_ATTACHMENTS], caption if first else None)
        uploaded = True
        first = False

    if linked:
        note = (caption + "\n" if (caption and not uploaded) else "")
        if link_fallback:
            await channel.send(content=f"{note}{link_fallback}\n"
                               f"_({len(linked)} item(s) too large to upload — see above)_".strip())
        elif not uploaded:
            await channel.send(content=f"{note}_({len(linked)} item(s) too large to upload)_".strip())
    return uploaded


async def handle_url(channel, url, cfg, cookies, workdir, proxy=None):
    """Download `url` and post all of its media to `channel`. Returns True if any
    file was uploaded. Cleans up the temp download dir afterwards."""
    info = await asyncio.to_thread(
        instagram.download, url, workdir, cookies, proxy,
        int(cfg.get("download_timeout", 180)), int(cfg.get("max_files_per_post", 20)),
    )
    dirs = [info.get("dir")]  # temp download dirs to clean up (gallery-dl + any fallback)
    scratch = []  # compressed temp files to clean up
    try:
        media = info.get("media") or []
        caption = _caption(info)

        if not media:
            # gallery-dl got nothing (usually the login wall / stale cookies). Try the
            # cookie-free public mirror before giving up — PUBLIC posts only.
            if cfg.get("ig_proxy_fallback", True):
                px = await asyncio.to_thread(
                    instagram.download_via_proxy, url, workdir,
                    int(cfg.get("download_timeout", 180)))
                dirs.append(px.get("dir"))
                if px.get("media"):
                    log.info("cookie-free mirror delivered %d file(s) for %s",
                             len(px["media"]), url)
                    return await post_media(channel, px["media"], caption, cfg, scratch,
                                            link_fallback=url)

            log.warning("no media for %s (rc=%s) %s", url, info.get("returncode"), info.get("stderr"))
            hint = ("\n_(no media returned — the post may be private, the link expired, "
                    "or the bot needs valid Instagram cookies)_")
            await channel.send(content=f"{caption}\n{url}".strip() + hint if caption
                               else f"{url}{hint}")
            return False

        return await post_media(channel, media, caption, cfg, scratch, link_fallback=url)
    finally:
        for work_dir in dirs:
            if work_dir and os.path.isdir(work_dir):
                shutil.rmtree(work_dir, ignore_errors=True)
        for p in scratch:
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass
