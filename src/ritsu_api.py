"""Tiny internal HTTP API for Ritsu (read-only, bearer-protected, tailnet-only port).

GET /healthz
GET /ig/comments?code=<shortcode>&limit=<n>   -> public comments via the IG burner

Disabled unless KABARINE_API_SECRET is set. The compose file publishes the port on
the Tailscale address only.
"""
import asyncio
import hmac
import logging
import os

from aiohttp import web

from src import ig_comments

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

    app = web.Application()
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/ig/comments", comments)
    return app


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
