"""/tt slash command group + the background pollers that forward new videos and stories.

The whole `/tt` group lives in this one cog so the group object isn't split across
cogs (which Discord doesn't allow). The passive link-detection listener lives in
ondemand.py. Videos and stories have separate pollers: stories last 24h, so they are
checked far less often (tiktok_story_sweep_seconds) and through a different engine
(gallery-dl; see src/tiktok_story.py).
"""
import asyncio
import os
import random
from collections import defaultdict
from typing import Optional, Union

import discord
from discord import app_commands
from discord.ext import commands

from src import db, mentions, monitor, tiktok, tiktok_story
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
        self._story_task = None
        self._backoff = 1.0  # auto-widens request spacing when TikTok starts refusing

    async def cog_load(self):
        self._poller_task = asyncio.create_task(self._run_poller())
        self._story_task = asyncio.create_task(self._run_story_poller())

    def cog_unload(self):
        for task in (self._poller_task, self._story_task):
            if task:
                task.cancel()

    def _workdir(self):
        return os.path.join(os.getenv("DATA_PATH", "./data"), "downloads")

    # ---- commands ---------------------------------------------------------

    @tt.command(name="add", description="Track a TikTok account and post its new videos and/or stories")
    @app_commands.describe(username="TikTok @username",
                           channel="Where to post (default: this channel/thread)",
                           tag="Optional role or person to ping on each new item",
                           type="What to forward (default: videos)")
    @app_commands.choices(type=[
        app_commands.Choice(name="videos", value="post"),
        app_commands.Choice(name="stories", value="story"),
        app_commands.Choice(name="both", value="both"),
    ])
    async def add(self, interaction: discord.Interaction, username: str,
                  channel: Optional[Target] = None, tag: Optional[Tag] = None,
                  type: Optional[app_commands.Choice[str]] = None):
        await interaction.response.defer(ephemeral=True)
        if not tiktok.is_valid_username(username):
            await interaction.followup.send(
                "❌ That isn't a TikTok username. Type the handle itself, e.g. `indahjkt48` — "
                "choosing a role or member from Discord's @ list turns it into a mention.",
                ephemeral=True)
            return
        username = tiktok.normalize_username(username)
        target = channel or interaction.channel
        mtype, mid = mentions.parse_target(tag)
        kind = type.value if type else "post"
        # Seed baseline = current newest id, so we don't dump the back-catalogue. This
        # listing also proves the account exists and gives a video to read its ids from.
        # It has to go through the @handle page (no secUid yet), which fails on most
        # single tries from the server — so try a few times before refusing.
        recent, err = None, None
        for attempt in range(3):
            try:
                recent = await asyncio.to_thread(tiktok.list_recent, username, 1,
                                                 self.bot.cookies, self.bot.proxy)
                break
            except Exception as e:
                err = e
                await asyncio.sleep(2)
        if recent is None:
            log.warning("add: couldn't read @%s (%s)", username, _short(err))
            await interaction.followup.send(
                f"❌ Couldn't read **@{username}**. Check the spelling and that the account is public.",
                ephemeral=True)
            return

        added, note = [], ""
        if kind in ("post", "both"):
            baseline = recent[0]["id"] if recent else None
            await db.add_subscription(self.bot.db_path, "tiktok", username, str(target.id),
                                      str(interaction.guild_id), "post", mtype, mid, baseline)
            added.append("videos")
        if kind in ("story", "both"):
            story_baseline = await self._story_baseline(username, recent)
            if story_baseline is None:
                note = ("\n_(Couldn't read its stories just now — the bot will start once it can, "
                        "without re-posting stories that were already up.)_")
            await db.add_subscription(self.bot.db_path, "tiktok", username, str(target.id),
                                      str(interaction.guild_id), "story", mtype, mid, story_baseline)
            added.append("stories")

        ping = f" and ping {mentions.mention_string(mtype, mid)}" if mtype else ""
        await interaction.followup.send(
            f"✅ Tracking **@{username}** ({' + '.join(added)}) → {target.mention}{ping}.{note}",
            ephemeral=True)

    async def _story_baseline(self, username, recent):
        """Marker for a new story subscription: the newest active story id, "0" when the
        account has none up (so its very next story is forwarded), or None when stories
        couldn't be read (seeded on the first successful check instead, without a dump)."""
        try:
            user_id = await self._user_id(username, recent[0]["url"] if recent else None)
            stories = await asyncio.to_thread(tiktok_story.list_stories, user_id, username,
                                              self.bot.cookies, self.bot.proxy)
        except Exception as e:
            log.warning("add: couldn't read stories of @%s (%s)", username, _short(e))
            return None
        return stories[0]["id"] if stories else "0"

    async def _ids(self, username, video_url=None):
        """(numeric user id, secUid): cached, else read from one video page, else from a
        fresh profile listing. The video page comes first because the profile listing
        fails for most accounts on any given try from the server, while a single video
        page reads reliably; a video id is usually on record from the video subscription.
        Both ids come from the same page, so a row cached before secUid existed is
        topped up the first time it is needed."""
        user_id, sec_uid = await db.get_ids(self.bot.db_path, username)
        if user_id and sec_uid:
            return user_id, sec_uid
        if not video_url:
            vid = await db.known_video_id(self.bot.db_path, username)
            if vid:
                video_url = f"https://www.tiktok.com/@{username}/video/{vid}"
        try:
            user_id, sec_uid = await asyncio.to_thread(tiktok_story.resolve_ids, username,
                                                       self.bot.cookies, self.bot.proxy, video_url)
        except Exception:
            if not video_url:
                raise
            # That video may have been deleted — fall back to the profile listing.
            user_id, sec_uid = await asyncio.to_thread(tiktok_story.resolve_ids, username,
                                                       self.bot.cookies, self.bot.proxy)
        await db.set_ids(self.bot.db_path, username, user_id, sec_uid)
        return user_id, sec_uid

    async def _user_id(self, username, video_url=None):
        """Numeric id only (the story path). A cached one is enough — don't make story
        checks depend on also resolving the secUid, which only video listing needs."""
        user_id, _sec = await db.get_ids(self.bot.db_path, username)
        return user_id or (await self._ids(username, video_url))[0]

    @tt.command(name="remove", description="Stop tracking a TikTok account")
    @app_commands.describe(username="TikTok @username", channel="Only this channel (default: everywhere)",
                           type="What to stop (default: both videos and stories)")
    @app_commands.choices(type=[
        app_commands.Choice(name="videos", value="post"),
        app_commands.Choice(name="stories", value="story"),
        app_commands.Choice(name="both", value="both"),
    ])
    async def remove(self, interaction: discord.Interaction, username: str,
                     channel: Optional[Target] = None,
                     type: Optional[app_commands.Choice[str]] = None):
        await interaction.response.defer(ephemeral=True)
        username = tiktok.normalize_username(username)
        content_type = type.value if type and type.value != "both" else None
        n = await db.remove_subscription(self.bot.db_path, "tiktok", username,
                                         str(channel.id) if channel else None, content_type)
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
        for username, channel_id, ctype, mtype, mid, _last, enabled in rows:
            mark = "" if enabled else " *(paused)*"
            kind = "stories" if ctype == "story" else "videos"
            lines.append(f"• **@{username}** ({kind}) → <#{channel_id}>{mentions.label(mtype, mid)}{mark}")
        # Discord rejects messages over 2000 characters; ~57 accounts is well past that.
        chunk = ""
        for line in lines:
            if len(chunk) + len(line) + 1 > 1900:
                await interaction.followup.send(chunk, ephemeral=True)
                chunk = ""
            chunk += line + "\n"
        if chunk:
            await interaction.followup.send(chunk, ephemeral=True)

    @tt.command(name="get", description="Download and post one TikTok video or photo post now")
    @app_commands.describe(url="A TikTok video or photo post link")
    async def get(self, interaction: discord.Interaction, url: str):
        if not tiktok.is_downloadable_url(url):
            await interaction.response.send_message(
                "I can only download a single TikTok **video/photo** post link. Live rooms "
                "(`/live`) never end, and a profile link isn't one post.",
                ephemeral=True)
            return
        await interaction.response.defer()
        try:
            await handle_url(interaction.channel, url, self.bot.cfg, self.bot.cookies,
                             self._workdir(), self.bot.proxy)
            await interaction.followup.send("✅ Done.", ephemeral=True)
        except tiktok.LiveContentError as e:
            log.info("/tt get refused %s (%s)", url, e)
            await interaction.followup.send(
                "❌ That link turned out to be a **live stream** — I can only "
                "download finished videos.", ephemeral=True)
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
        """Returns True if the listing succeeded (even if no new videos), False if it failed.

        Lists through the account's secUid (tiktokuser:), which skips the profile page
        that fails for most accounts from the server; falls back to the @handle listing
        when the secUid isn't known yet or its listing fails.
        """
        scan = int(self.bot.cfg.get("playlist_scan_count", 5))
        try:
            _uid, sec_uid = await self._ids(username)
        except Exception:
            sec_uid = None          # no readable video yet — the @handle listing still might work
        recent = None
        if sec_uid:
            try:
                recent = await asyncio.to_thread(tiktok.list_recent, username, scan,
                                                 self.bot.cookies, self.bot.proxy, sec_uid)
            except Exception as e:
                log.info("poll: secUid listing failed for @%s (%s); trying @handle",
                         username, _short(e))
        if recent is None:
            try:
                recent = await asyncio.to_thread(tiktok.list_recent, username, scan,
                                                 self.bot.cookies, self.bot.proxy)
            except Exception as e:
                # Expected occasionally cookie-free (rate-limits, JS challenge, cache lag).
                log.warning("poll: couldn't list @%s (%s)", username, _short(e))
                return False
        await monitor.forward_new(self.bot, "tiktok", username, subs, recent, handle_url,
                                  self._workdir(), self.bot.cookies, self.bot.proxy, "TikTok",
                                  headline=f"🎬 **TikTok Post** · **@{username}**")
        return True

    # ---- story poller -----------------------------------------------------

    async def _run_story_poller(self):
        await self.bot.wait_until_ready()
        if not self.bot.cfg.get("tiktok_story_enabled", True):
            log.info("tiktok story poller disabled (tiktok_story_enabled=false)")
            return
        log.info("tiktok story poller alive")
        while not self.bot.is_closed():
            try:
                await self._story_sweep()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("story sweep crashed; backing off 30s")
                await asyncio.sleep(30)

    async def _story_sweep(self):
        """Walk every story subscription once over ~tiktok_story_sweep_seconds."""
        rows = await db.get_enabled(self.bot.db_path, "tiktok", "story")
        by_user = defaultdict(list)
        for username, channel_id, mtype, mid, last_seen in rows:
            by_user[username].append((channel_id, mtype, mid, last_seen))
        if not by_user:
            await asyncio.sleep(30)  # no story subscriptions — idle, don't busy-loop
            return
        target = float(self.bot.cfg.get("tiktok_story_sweep_seconds", 600))
        floor = float(self.bot.cfg.get("tiktok_story_min_spacing", 3))
        spacing = max(floor, target / len(by_user))
        for username, subs in by_user.items():
            await self._check_stories(username, subs)
            await asyncio.sleep(self._jittered(spacing))

    async def _check_stories(self, username, subs):
        try:
            user_id = await self._user_id(username)
            stories = await asyncio.to_thread(tiktok_story.list_stories, user_id, username,
                                              self.bot.cookies, self.bot.proxy)
        except Exception as e:
            log.warning("stories: couldn't check @%s (%s)", username, _short(e))
            return
        if not stories:
            # A successful "nothing up" read is a baseline too: without it, a subscription
            # whose add-time read failed would SEED (skip) its first real story.
            for channel_id, _mtype, _mid, last_seen in subs:
                if last_seen is None:
                    await db.update_last_seen(self.bot.db_path, "tiktok", username, channel_id,
                                              "0", "story")
            return
        await monitor.forward_new(self.bot, "tiktok", username, subs, stories, handle_url,
                                  self._workdir(), self.bot.cookies, self.bot.proxy, "TikTok",
                                  content_type="story", noun="story", by_id=True,
                                  headline=f"📖 **TikTok Story** · **@{username}**")


async def setup(bot):
    await bot.add_cog(Tracking(bot))
