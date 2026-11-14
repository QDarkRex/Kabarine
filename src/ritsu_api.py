"""Tiny internal HTTP API for Ritsu (read-only, bearer-protected, tailnet-only port).

GET /healthz
GET /ig/comments?code=<shortcode>&limit=<n>   -> public comments via the IG burner
GET /tiktok/latest?user=<username>            -> newest videos (no download), 20/hour

Disabled unless KABARINE_API_SECRET is set. The compose file publishes the port on
the Tailscale address only.
"""
import asyncio
import hmac
import logging
import os

from aiohttp import web

import time

from src import ig_comments, tiktok

log = logging.getLogger("kabarine.ritsu_api")


def _authorized(request):
    secret = os.getenv("KABARINE_API_SECRET", "")
    supplied = request.headers.get("Authorization", "")
    return bool(secret) and hmac.compare_digest(supplied.encode(), f"Bearer {secret}".encode())


def build_app(bot):
    async def healthz(_request):
        return web.json_response({"status": "ok"})

    async def comments(request):
        if not _authorized(request):
            return web.json_response({"status": "unauthorized"}, status=401)
        code = request.query.get("code", "")
        try:
            limit = max(1, min(int(request.query.get("limit", "45")), 60))
        except ValueError:
            limit = 45
        try:
            result = await asyncio.to_thread(ig_comments.fetch_comments, code, bot.ig_cookies, limit)
        except ig_comments.RateLimited:
            return web.json_response({"status": "rate_limited"}, status=429)
        except Exception as exc:  # gallery-dl raises on login walls, 404, rate limits
            log.warning("ig comments failed for %s (%s)", code, type(exc).__name__)
            return web.json_response({"status": "unavailable", "reason": type(exc).__name__}, status=502)
        return web.json_response(result, status=200 if result.get("status") == "ok" else 400)

    async def tiktok_latest(request):
        """Newest videos of one profile (no download) so a missed post can be told apart
        from an account that simply hasn't posted."""
        if not _authorized(request):
            return web.json_response({"status": "unauthorized"}, status=401)
        user = tiktok.normalize_username(request.query.get("user", ""))
        if not tiktok.is_valid_username(user):
            return web.json_response({"status": "invalid_user"}, status=400)
        if not _tiktok_slot():
            return web.json_response({"status": "rate_limited"}, status=429)
        sec_uid = None
        try:
            import aiosqlite
            async with aiosqlite.connect(bot.db_path) as db:
                cur = await db.execute("SELECT sec_uid FROM tiktok_user_ids WHERE username = ?", (user,))
                row = await cur.fetchone()
                sec_uid = row[0] if row and row[0] else None
        except Exception:  # listing still works without it, just less reliably
            sec_uid = None
        try:
            items = await asyncio.to_thread(tiktok.list_recent, user, 6, bot.cookies, bot.proxy, sec_uid)
        except Exception as exc:
            log.warning("tiktok listing failed for %s (%s)", user, type(exc).__name__)
            return web.json_response({"status": "unavailable", "reason": type(exc).__name__}, status=502)
        for item in items:
            item["posted_at"] = _tiktok_time(item.get("id"))
        items.sort(key=lambda item: item.get("posted_at") or "", reverse=True)  # pinned posts come first
        return web.json_response({"status": "ok", "user": user, "videos": items})

    app = web.Application()
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/ig/comments", comments)
    app.router.add_get("/tiktok/latest", tiktok_latest)
    return app


_tiktok_calls = []


def _tiktok_slot(hourly_cap=20):
    now = time.time()
    _tiktok_calls[:] = [t for t in _tiktok_calls if now - t < 3600]
    if len(_tiktok_calls) >= hourly_cap:
        return False
    _tiktok_calls.append(now)
    return True


def _tiktok_time(item_id):
    try:
        seconds = int(item_id) >> 32
    except (TypeError, ValueError):
        return None
    if not 1_400_000_000 < seconds < 2_200_000_000:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seconds))


async def start(bot, port=8460):
    if not os.getenv("KABARINE_API_SECRET"):
        log.info("ritsu API disabled (KABARINE_API_SECRET unset)")
        return None
    runner = web.AppRunner(build_app(bot), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info("ritsu API listening on :%d", port)
    return runner
