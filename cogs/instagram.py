"""The whole `/ig` command group + Instagram post/story monitoring.

Commands (mirroring `/tt`):
  /ig get <url>                        on-demand download of a post/reel/carousel
  /ig add <user> [chan] [tag] [type]   forward an account's NEW posts and/or stories
  /ig remove <user> [chan]
  /ig list

Plus a passive listener that auto-downloads Instagram links pasted in chat.

Monitoring is sharded across one or more "burners" (each a cookie+proxy pair). Each
burner runs its OWN poller in parallel and only handles the accounts assigned to it
(by a stable hash of the username), so load — and ban risk — is split across separate
logins/IPs. Polling is deliberately much slower than TikTok's — Instagram flags
aggressive automated access.

Stories can only be pulled as the whole current set (Instagram has no per-item URL),
so we list them cheaply, and only download+forward when something is newer than the
last item we handled. NOTE: viewing a story puts the burner in that account's
story-viewers list.
"""
import asyncio
import hashlib
import os
import random
import shutil
from collections import defaultdict
from typing import Optional, Union

import discord
from discord import app_commands
from discord.ext import commands

from src import db, instagram, mentions, monitor
from src.discord_utils import suppress_link_embeds
from src.instagram_post import handle_url, post_media
from src.log import get_logger

log = get_logger(__name__)

Target = Union[discord.TextChannel, discord.Thread]
Tag = Union[discord.Role, discord.Member]


def _short(e):
    msg = str(e).strip()
    return msg.splitlines()[-1] if msg else e.__class__.__name__


def _cleanup(paths):
    for p in paths:
        if p and os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass


class Instagram(commands.Cog):
    ig = app_commands.Group(name="ig", description="Download Instagram media & forward new posts/stories")

    def __init__(self, bot):
        self.bot = bot
        self._poller_tasks = []
        self._backoff = [1.0] * max(len(getattr(bot, "ig_burners", [])), 1)

    async def cog_load(self):
        # Downloader-only mode: skip the auto-feed pollers entirely. /ig get + paste
        # still work; the burner cookie is then used only on demand, so it lasts much
        # longer and rarely trips Instagram's automation flags.
        if not self.bot.cfg.get("ig_monitoring_enabled", True):
            log.info("instagram monitoring disabled (ig_monitoring_enabled=false) — "
                     "downloader-only mode, pollers not started")
            return
        for bi in range(len(self.bot.ig_burners)):
            self._poller_tasks.append(asyncio.create_task(self._run_poller(bi)))

    def cog_unload(self):
        for t in self._poller_tasks:
            t.cancel()

    def _workdir(self):
        return os.path.join(os.getenv("DATA_PATH", "./data"), "downloads")

    def _burner_for(self, username):
        """Stable account -> burner assignment (survives restarts). Returns
        (index, cookies, proxy) or None if no burners configured."""
        burners = self.bot.ig_burners
        if not burners:
            return None
        idx = int(hashlib.md5(username.encode()).hexdigest(), 16) % len(burners)
        b = burners[idx]
        return (idx, b["cookies"], b["proxy"])

    # ---- commands ---------------------------------------------------------

    @ig.command(name="get",
                description="Download an Instagram post/reel/carousel at native resolution")
    @app_commands.describe(url="An instagram.com post, reel, or tv link")
    async def get(self, interaction: discord.Interaction, url: str):
        if not instagram.is_downloadable_url(url):
            await interaction.response.send_message(
                "I can only download a single Instagram **post/reel** link "
                "(`/p/...`, `/reel/...`). Live streams, stories and profile links "
                "aren't supported.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        try:
            await handle_url(interaction.channel, url, self.bot.cfg, self.bot.ig_cookies,
                             self._workdir(), self.bot.ig_proxy)
            try:
                await interaction.delete_original_response()
            except discord.HTTPException:
                pass
        except Exception:
            log.exception("/ig get failed for %s", url)
            try:
                await interaction.followup.send(
                    "Sorry — I couldn't download that one. Check the link, or the "
                    "bot may need valid Instagram cookies.", ephemeral=True)
            except discord.HTTPException:
                pass

    @ig.command(name="add", description="Forward an Instagram account's new posts and/or stories")
    @app_commands.describe(username="Instagram @username",
                           channel="Where to post (default: this channel/thread)",
                           tag="Optional role or person to ping on each new item",
                           type="What to forward (default: posts)")
    @app_commands.choices(type=[
        app_commands.Choice(name="posts", value="post"),
        app_commands.Choice(name="stories", value="story"),
        app_commands.Choice(name="both", value="both"),
    ])
    async def add(self, interaction: discord.Interaction, username: str,
                  channel: Optional[Target] = None, tag: Optional[Tag] = None,
                  type: Optional[app_commands.Choice[str]] = None):
        await interaction.response.defer(ephemeral=True)
        if not instagram.is_valid_username(username):
            await interaction.followup.send(
                "❌ That isn't an Instagram username. Type the handle itself, e.g. `natgeo` — "
                "choosing a role or member from Discord's @ list turns it into a mention.",
                ephemeral=True)
            return
        username = instagram.normalize_username(username)
        burner = self._burner_for(username)
        if burner is None:
            await interaction.followup.send(
                "❌ No Instagram cookies are configured, so monitoring is off. "
                "Set `IG_COOKIES` to a burner cookies.txt first.", ephemeral=True)
            return
        _bi, cookies, proxy = burner
        target = channel or interaction.channel
        mtype, mid = mentions.parse_target(tag)
        kind = type.value if type else "post"
        wants_posts = kind in ("post", "both")
        wants_stories = kind in ("story", "both")

        # Verify the account is readable (this also seeds the post baseline).
        try:
            recent = await asyncio.to_thread(instagram.list_recent, username, 1, cookies, proxy) \
                if wants_posts else []
        except Exception as e:
            log.warning("ig add: couldn't read @%s (%s)", username, _short(e))
            await interaction.followup.send(
                f"❌ Couldn't read **@{username}**. Check the spelling, and that the bot's "
                f"Instagram cookies are valid (private accounts need the burner to follow them).",
                ephemeral=True)
            return

        added = []
        if wants_posts:
            baseline = recent[0]["id"] if recent else None
            await db.add_subscription(self.bot.db_path, "instagram", username, str(target.id),
                                      str(interaction.guild_id), "post", mtype, mid, baseline)
            added.append("posts")
        if wants_stories:
            try:
                st = await asyncio.to_thread(instagram.list_stories, username, cookies, proxy)
            except Exception:
                st = []
            await db.add_subscription(self.bot.db_path, "instagram", username, str(target.id),
                                      str(interaction.guild_id), "story", mtype, mid,
                                      st[0]["id"] if st else None)
            added.append("stories")

        ping = f" and ping {mentions.mention_string(mtype, mid)}" if mtype else ""
        note = ""
        if wants_stories:
            note = "\n_(Heads up: viewing stories puts this burner in the account's story-viewers list.)_"
        await interaction.followup.send(
            f"✅ Watching **@{username}** ({' + '.join(added)}) → {target.mention}{ping}. "
            f"New items appear within ~10–30 min.{note}", ephemeral=True)

    @ig.command(name="remove", description="Stop forwarding an Instagram account")
    @app_commands.describe(username="Instagram @username", channel="Only this channel (default: everywhere)")
    async def remove(self, interaction: discord.Interaction, username: str,
                     channel: Optional[Target] = None):
        await interaction.response.defer(ephemeral=True)
        username = instagram.normalize_username(username)
        chan = str(channel.id) if channel else None
        # remove both posts and stories subscriptions
        n = 0
        for ctype in ("post", "story"):
            n += await db.remove_subscription(self.bot.db_path, "instagram", username, chan, ctype)
        if n:
            await interaction.followup.send(f"✅ Removed **@{username}** ({n} subscription(s)).", ephemeral=True)
        else:
            await interaction.followup.send(f"ℹ️ **@{username}** wasn't being watched.", ephemeral=True)

    @ig.command(name="list", description="Show watched Instagram accounts")
    async def list_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        rows = await db.list_subscriptions(self.bot.db_path, "instagram", str(interaction.guild_id))
        if not rows:
            await interaction.followup.send("No Instagram accounts watched yet. Use `/ig add`.", ephemeral=True)
            return
        lines = []
        for username, channel_id, ctype, mtype, mid, _last, enabled in rows:
            mark = "" if enabled else " *(paused)*"
            kind = "stories" if ctype == "story" else "posts"
            lines.append(f"• **@{username}** ({kind}) → <#{channel_id}>{mentions.label(mtype, mid)}{mark}")
        await interaction.followup.send("\n".join(lines), ephemeral=True)

    # ---- auto-detect listener --------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or not message.guild:
            return
        if not self.bot.cfg.get("auto_detect_links", True):
            return
        # Only single posts/reels: a live room never ends, and a profile link would
        # make gallery-dl crawl the entire account until it times out.
        links = [u for u in instagram.find_links(message.content)
                 if instagram.is_downloadable_url(u)]
        if not links:
            return
        for url in links[:3]:
            try:
                async with message.channel.typing():
                    await handle_url(message.channel, url, self.bot.cfg, self.bot.ig_cookies,
                                     self._workdir(), self.bot.ig_proxy)
            except Exception:
                log.exception("auto-download failed for %s", url)
        await suppress_link_embeds(message, self.bot.cfg)

    # ---- paced scheduler (per-burner, slow) -------------------------------

    def _spacing(self, account_count):
        target = float(self.bot.cfg.get("ig_sweep_target_seconds", 900))
        floor = float(self.bot.cfg.get("ig_min_request_spacing", 20))
        return max(floor, target / max(account_count, 1))

    def _jittered(self, spacing):
        j = float(self.bot.cfg.get("ig_request_jitter", 5))
        return max(1.0, spacing + random.uniform(-j, j))

    async def _run_poller(self, bi):
        await self.bot.wait_until_ready()
        log.info("instagram poller %d alive (of %d burner(s))", bi, len(self.bot.ig_burners))
        self.bot.health.register(f"instagram[{bi}]")
        while not self.bot.is_closed():
            try:
                await self._one_sweep(bi)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("ig poller %d sweep crashed; backing off 30s", bi)
                await asyncio.sleep(30)

    async def _one_sweep(self, bi):
        cookies = self.bot.ig_burners[bi]["cookies"]
        proxy = self.bot.ig_burners[bi]["proxy"]

        posts_by, stories_by = defaultdict(list), defaultdict(list)
        for username, channel_id, mtype, mid, last_seen in \
                await db.get_enabled(self.bot.db_path, "instagram", "post"):
            if self._burner_for(username)[0] == bi:
                posts_by[username].append((channel_id, mtype, mid, last_seen))
        for username, channel_id, mtype, mid, last_seen in \
                await db.get_enabled(self.bot.db_path, "instagram", "story"):
            if self._burner_for(username)[0] == bi:
                stories_by[username].append((channel_id, mtype, mid, last_seen))

        users = set(posts_by) | set(stories_by)
        if not users:
            self.bot.health.sweep(f"instagram[{bi}]", 0, 0)
            await asyncio.sleep(30)
            return

        spacing = self._spacing(len(users)) * self._backoff[bi]
        failures = 0
        for username in users:
            if not await self._check_account(username, posts_by.get(username),
                                             stories_by.get(username), cookies, proxy):
                failures += 1
            await asyncio.sleep(self._jittered(spacing))
        self.bot.health.sweep(f"instagram[{bi}]", len(users) - failures, len(users))

        if failures / len(users) >= 0.3:
            self._backoff[bi] = min(self._backoff[bi] * 2, 8.0)
            log.warning("ig poll[%d]: %d/%d accounts failed — slowing down (spacing x%.0f)",
                        bi, failures, len(users), self._backoff[bi])
        elif failures == 0 and self._backoff[bi] > 1.0:
            self._backoff[bi] = max(self._backoff[bi] / 2, 1.0)
            log.info("ig poll[%d]: recovered — easing back (spacing x%.1f)", bi, self._backoff[bi])

    async def _check_account(self, username, post_subs, story_subs, cookies, proxy):
        ok = True
        if post_subs:
            scan = int(self.bot.cfg.get("ig_playlist_scan_count", 3))
            try:
                recent = await asyncio.to_thread(instagram.list_recent, username, scan, cookies, proxy)
            except Exception as e:
                log.warning("ig poll: couldn't list posts @%s (%s)", username, _short(e))
                ok = False
            else:
                await monitor.forward_new(self.bot, "instagram", username, post_subs, recent,
                                          handle_url, self._workdir(), cookies, proxy, "Instagram")
        if story_subs:
            try:
                await self._forward_stories(username, story_subs, cookies, proxy)
            except Exception as e:
                log.warning("ig poll: story check failed @%s (%s)", username, _short(e))
                ok = False
        return ok

    async def _forward_stories(self, username, subs, cookies, proxy):
        items = await asyncio.to_thread(instagram.list_stories, username, cookies, proxy)
        if not items:
            return
        current_ids = [it["id"] for it in items]   # newest-first
        newest = current_ids[0]
        downloaded = None
        try:
            for channel_id, mtype, mid, last_seen in subs:
                if last_seen is None:
                    await db.update_last_seen(self.bot.db_path, "instagram", username,
                                              channel_id, newest, "story")
                    continue
                if not any(int(i) > int(last_seen) for i in current_ids):
                    continue
                channel = self.bot.get_channel(int(channel_id))
                if channel is None:
                    log.warning("ig story: channel %s for @%s not found", channel_id, username)
                    continue
                if downloaded is None:
                    downloaded = await asyncio.to_thread(instagram.download_stories, username,
                                                         self._workdir(), cookies, proxy)
                new_items = [it for it in downloaded["items"] if int(it["id"]) > int(last_seen)]
                if not new_items:
                    log.warning("ig story: @%s had new listed ids %s but downloaded ids were %s",
                                username, current_ids, [it["id"] for it in downloaded["items"]])
                    continue
                for item in reversed(new_items):   # oldest-first
                    sid = item["id"]
                    path = item["path"]
                    scratch = []
                    try:
                        if mtype:
                            await mentions.announce(channel, mtype, mid,
                                                    f"new Instagram story from **@{username}**")
                        await post_media(channel, [path], f"**@{username}** — story",
                                         self.bot.cfg, scratch)
                    except Exception as e:
                        log.warning("ig story forward failed @%s %s (%s); retry next sweep",
                                    username, sid, _short(e))
                        self.bot.health.forward_failed()
                        _cleanup(scratch)
                        break
                    _cleanup(scratch)
                    self.bot.health.forwarded()
                    await db.update_last_seen(self.bot.db_path, "instagram", username,
                                              channel_id, sid, "story")
                    log.info("forwarded Instagram story @%s %s -> channel %s", username, sid, channel_id)
        finally:
            if downloaded:
                shutil.rmtree(downloaded["dir"], ignore_errors=True)


async def setup(bot):
    await bot.add_cog(Instagram(bot))
