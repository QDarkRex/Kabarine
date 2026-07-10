import asyncio
import os

import discord
from discord.ext import commands
from dotenv import load_dotenv

from src import db
from src.config import load_config
from src.log import get_logger, setup_logging

load_dotenv()
log = get_logger(__name__)


def _resolve_cookies():
    path = os.getenv("TIKTOK_COOKIES") or ""
    path = path.strip()
    if not path:
        return None
    if not os.path.exists(path):
        log.warning("TIKTOK_COOKIES set to %s but file not found — continuing without cookies", path)
        return None
    log.info("using TikTok cookies from %s", path)
    return path


def _resolve_ig_burners():
    """Build the list of Instagram 'burners' — each a (cookies, proxy) pair used to
    poll a shard of the watched accounts, so load (and ban risk) is split across
    separate logins/IPs.

    IG_COOKIES : comma-separated cookie-file paths (one per burner).
    IG_PROXIES : comma-separated proxy URLs, paired to burners by position
                 (falls back to the single IG_PROXY for a one-burner setup).
    """
    cookie_list = [c.strip() for c in (os.getenv("IG_COOKIES") or "").split(",") if c.strip()]
    proxy_list = [p.strip() for p in
                  (os.getenv("IG_PROXIES") or os.getenv("IG_PROXY") or "").split(",") if p.strip()]

    burners = []
    for i, cookies in enumerate(cookie_list):
        if not os.path.exists(cookies):
            log.warning("IG cookie file %s not found — skipping that burner", cookies)
            continue
        burners.append({"cookies": cookies, "proxy": proxy_list[i] if i < len(proxy_list) else None})

    if not burners:
        log.warning("No Instagram cookies configured — /ig monitoring is off, and /ig get will "
                    "hit the login wall for most posts. Set IG_COOKIES to a burner cookies.txt.")
    else:
        withproxy = sum(1 for b in burners if b["proxy"])
        log.info("Instagram: %d burner(s) configured (%d with a proxy)", len(burners), withproxy)
        # One proxy-less burner is fine (it's on the server IP, a *different* IP from the
        # proxied ones). The problem is 2+ proxy-less burners: they'd share the server IP,
        # which Instagram links together.
        noproxy = len(burners) - withproxy
        if noproxy >= 2:
            log.warning("%d IG burners have no proxy — they all share the server IP, which Instagram "
                        "links together (co-ban risk). Give all but one its own proxy.", noproxy)
    return burners


async def _run():
    setup_logging()
    cfg = load_config()

    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("BOT_TOKEN is missing — set it in .env")

    data_path = os.getenv("DATA_PATH", "./data")
    os.makedirs(os.path.join(data_path, "downloads"), exist_ok=True)
    db_path = os.path.join(data_path, "tiktok.db")
    await db.init_db(db_path)

    intents = discord.Intents.default()
    intents.message_content = True  # needed to read pasted TikTok links

    bot = commands.Bot(command_prefix=cfg.get("prefix", "!"), intents=intents)
    bot.cfg = cfg
    bot.db_path = db_path
    bot.cookies = _resolve_cookies()
    bot.proxy = (os.getenv("TIKTOK_PROXY") or "").strip() or None
    if bot.proxy:
        log.info("routing TikTok requests through a proxy")  # don't log the URL (may hold creds)
    bot.ig_burners = _resolve_ig_burners()
    # On-demand /ig get + paste-detect use the first burner (any will do).
    bot.ig_cookies = bot.ig_burners[0]["cookies"] if bot.ig_burners else None
    bot.ig_proxy = bot.ig_burners[0]["proxy"] if bot.ig_burners else None

    @bot.event
    async def on_ready():
        log.info("%s is online", bot.user)
        try:
            synced = await bot.tree.sync()
            log.info("synced %d slash command(s)", len(synced))
        except Exception:
            log.exception("slash command sync failed")

    async with bot:
        for fn in sorted(os.listdir("./cogs")):
            if fn.endswith(".py") and fn != "__init__.py":
                await bot.load_extension(f"cogs.{fn[:-3]}")
                log.info("loaded cog %s", fn)
        await bot.start(token)


if __name__ == "__main__":
    asyncio.run(_run())
