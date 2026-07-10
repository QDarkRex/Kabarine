"""/tt slash command group + the background poller that forwards new videos.

The whole `/tt` group lives in this one cog so the group object isn't split across
cogs (which Discord doesn't allow). The passive link-detection listener lives in
ondemand.py.
"""
import asyncio
import os
import random
from collections import defaultdict
from typing import Optional, Union

import discord
from discord import app_commands
from discord.ext import commands

from src import db, mentions, monitor, tiktok
from src.discord_post import handle_url
from src.log import get_logger

log = get_logger(__name__)

# Where /tt add can route videos: a normal channel or a thread.
Target = Union[discord.TextChannel, discord.Thread]
# Who to ping on a new video: a role or a specific person (or @everyone role).
Tag = Union[discord.Role, discord.Member]


def _short(e):
    """Last line of an exception's message — keeps expected TikTok errors to one line."""
    msg = str(e).strip()
    return msg.splitlines()[-1] if msg else e.__class__.__name__


class Tracking(commands.Cog):
    # Open to everyone in the server (no permission gate).
    tt = app_commands.Group(
        name="tt",
        description="Forward TikTok videos to Discord",
    )

    def __init__(self, bot):
        self.bot = bot
        self._poller_task = None
        self._backoff = 1.0  # auto-widens request spacing when TikTok starts refusing

    async def cog_load(self):
        self._poller_task = asyncio.create_task(self._run_poller())

    def cog_unload(self):
        if self._poller_task:
            self._poller_task.cancel()

    def _workdir(self):
        return os.path.join(os.getenv("DATA_PATH", "./data"), "downloads")

    # ---- commands ---------------------------------------------------------

    @tt.command(name="add", description="Track a TikTok account and post its new videos")
    @app_commands.describe(username="TikTok @username",
                           channel="Where to post (default: this channel/thread)",
                           tag="Optional role or person to ping on each new video")
    async def add(self, interaction: discord.Interaction, username: str,
                  channel: Optional[Target] = None, tag: Optional[Tag] = None):
        await interaction.response.defer(ephemeral=True)
        username = tiktok.normalize_username(username)
        target = channel or interaction.channel
        mtype, mid = mentions.parse_target(tag)
        # Seed baseline = current newest id, so we don't dump the back-catalogue.
        try:
            recent = await asyncio.to_thread(tiktok.list_recent, username, 1,
                                             self.bot.cookies, self.bot.proxy)
        except Exception as e:
            log.warning("add: couldn't read @%s (%s)", username, _short(e))
            await interaction.followup.send(
                f"❌ Couldn't read **@{username}**. Check the spelling and that the account is public.",
                ephemeral=True)
            return
        baseline = recent[0]["id"] if recent else None
        await db.add_subscription(self.bot.db_path, "tiktok", username, str(target.id),
                                  str(interaction.guild_id), "post", mtype, mid, baseline)
        ping = f" and ping {mentions.mention_string(mtype, mid)}" if mtype else ""
        await interaction.followup.send(
            f"✅ Tracking **@{username}** → {target.mention}{ping}. New videos will post there.",
            ephemeral=True)

    @tt.command(name="remove", description="Stop tracking a TikTok account")
    @app_commands.describe(username="TikTok @username", channel="Only this channel (default: everywhere)")
    async def remove(self, interaction: discord.Interaction, username: str,
                     channel: Optional[Target] = None):
        await interaction.response.defer(ephemeral=True)
        username = tiktok.normalize_username(username)
        n = await db.remove_subscription(self.bot.db_path, "tiktok", username,
                                         str(channel.id) if channel else None)
        if n:
            await interaction.followup.send(f"✅ Removed **@{username}** ({n} subscription(s)).", ephemeral=True)
        else:
            await interaction.followup.send(f"ℹ️ **@{username}** wasn't being tracked.", ephemeral=True)

    @tt.command(name="list", description="Show tracked TikTok accounts")
    async def list_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        rows = await db.list_subscriptions(self.bot.db_path, "tiktok", str(interaction.guild_id))
        if not rows:
            await interaction.followup.send("No accounts tracked yet. Use `/tt add`.", ephemeral=True)
            return
        lines = []
        for username, channel_id, _ctype, mtype, mid, _last, enabled in rows:
            mark = "" if enabled else " *(paused)*"
            lines.append(f"• **@{username}** → <#{channel_id}>{mentions.label(mtype, mid)}{mark}")
        await interaction.followup.send("\n".join(lines), ephemeral=True)

    @tt.command(name="get", description="Download and post one TikTok video now")
    @app_commands.describe(url="A TikTok video link")
    async def get(self, interaction: discord.Interaction, url: str):
        await interaction.response.defer()
        try:
            await handle_url(interaction.channel, url, self.bot.cfg, self.bot.cookies,
                             self._workdir(), self.bot.proxy)
            await interaction.followup.send("✅ Done.", ephemeral=True)
        except Exception:
            log.exception("/tt get failed for %s", url)
            await interaction.followup.send("❌ Couldn't download that link.", ephemeral=True)

    # ---- paced scheduler --------------------------------------------------

    def _spacing(self, account_count):
        """Seconds to wait between individual TikTok requests.

        Sized so one full sweep of all accounts takes ~sweep_target_seconds, but never
        faster than min_request_spacing (a safety floor). Net effect: each account is
        re-checked roughly every max(sweep_target_seconds, accounts * min_request_spacing).
        """
        target = float(self.bot.cfg.get("sweep_target_seconds", 150))
        floor = float(self.bot.cfg.get("min_request_spacing", 1.5))
        return max(floor, target / max(account_count, 1))

    def _jittered(self, spacing):
        j = float(self.bot.cfg.get("request_jitter", 0.5))
        return max(0.1, spacing + random.uniform(-j, j))

    async def _run_poller(self):
        await self.bot.wait_until_ready()
        log.info("tiktok poller alive (paced scheduler)")
        while not self.bot.is_closed():
            try:
                await self._one_sweep()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("poller sweep crashed; backing off 10s")
                await asyncio.sleep(10)

    async def _one_sweep(self):
        """Walk every tracked account once, evenly spaced over ~sweep_target_seconds.

        Spacing is widened by an adaptive backoff factor: if many accounts fail to
        list in a sweep (a sign TikTok is rate-limiting us), we slow down for the next
        sweep instead of hammering through — and speed back up once it recovers.
        """
        rows = await db.get_enabled(self.bot.db_path, "tiktok", "post")
        by_user = defaultdict(list)
        for username, channel_id, mtype, mid, last_seen in rows:
            by_user[username].append((channel_id, mtype, mid, last_seen))
        if not by_user:
            await asyncio.sleep(10)  # nothing tracked yet — idle, don't busy-loop
            return

        spacing = self._spacing(len(by_user)) * self._backoff
        failures = 0
        for username, subs in by_user.items():
            if not await self._check_account(username, subs):
                failures += 1
            await asyncio.sleep(self._jittered(spacing))

        # Adapt for next sweep based on how many accounts refused us.
        if failures / len(by_user) >= 0.3:
            self._backoff = min(self._backoff * 2, 8.0)
            log.warning("poll: %d/%d accounts failed this sweep — slowing down (spacing x%.0f)",
                        failures, len(by_user), self._backoff)
        elif failures == 0 and self._backoff > 1.0:
            self._backoff = max(self._backoff / 2, 1.0)
            log.info("poll: recovered — easing back (spacing x%.1f)", self._backoff)

    async def _check_account(self, username, subs):
        """Returns True if the listing succeeded (even if no new videos), False if it failed."""
        scan = int(self.bot.cfg.get("playlist_scan_count", 5))
        try:
            recent = await asyncio.to_thread(tiktok.list_recent, username, scan,
                                             self.bot.cookies, self.bot.proxy)
        except Exception as e:
            # Expected occasionally cookie-free (rate-limits, JS challenge, cache lag).
            log.warning("poll: couldn't list @%s (%s)", username, _short(e))
            return False
        await monitor.forward_new(self.bot, "tiktok", username, subs, recent, handle_url,
                                  self._workdir(), self.bot.cookies, self.bot.proxy, "TikTok")
        return True


async def setup(bot):
    await bot.add_cog(Tracking(bot))
