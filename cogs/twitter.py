"""X / Twitter on-demand downloads: the `/x get` slash command + pasted-link detection.

Mirrors /tt get and /ig get: media is reposted natively (photos at original resolution,
videos at the best mp4 variant) through the shared instagram_post.post_media, which
batches <=10 files per message and compresses or links anything over the upload cap.

Pasted links only react to tweets that HAVE media — people share X links constantly in
chat, and reposting text-only tweets would just be noise. `x_auto_detect_links` turns
the paste detection off on its own, without touching TikTok/Instagram detection.
"""
import asyncio
import os
import shutil

import discord
from discord import app_commands
from discord.ext import commands

from src import twitter
from src.discord_utils import suppress_link_embeds
from src.instagram_post import post_media
from src.log import get_logger

log = get_logger(__name__)


class Twitter(commands.Cog):
    x = app_commands.Group(name="x", description="Download media from X / Twitter posts")

    def __init__(self, bot):
        self.bot = bot

    def _workdir(self):
        return os.path.join(os.getenv("DATA_PATH", "./data"), "downloads")

    async def _post(self, channel, tid):
        """Download a tweet's media and post it. Returns the number of files found
        (0 = the tweet has no media). Raises if the tweet can't be read."""
        cfg = self.bot.cfg
        info = await asyncio.to_thread(
            twitter.download, tid, self._workdir(), int(cfg.get("download_timeout", 180)),
            int(cfg.get("max_files_per_post", 20)), int(cfg.get("max_download_mb", 300)),
            float(cfg.get("max_upload_mb", 10)))
        scratch = []
        try:
            if info["media"]:
                await post_media(channel, info["media"], twitter.caption(info), cfg, scratch,
                                 link_fallback=info["url"])
            if info["oversize"]:
                head = "" if info["media"] else twitter.caption(info) + "\n"
                await channel.send(f"{head}{info['url']}\n_({info['oversize']} video(s) too large "
                                   f"to download — see the link)_".strip())
            return len(info["media"]) + info["oversize"]
        finally:
            shutil.rmtree(info["dir"], ignore_errors=True)
            for p in scratch:
                try:
                    os.remove(p)
                except OSError:
                    pass

    @x.command(name="get", description="Download the photos/videos of an X (Twitter) post")
    @app_commands.describe(url="An x.com or twitter.com post link")
    async def get(self, interaction: discord.Interaction, url: str):
        tid = twitter.tweet_id(url)
        if not tid:
            await interaction.response.send_message(
                "I can only download a single X post link (`x.com/<user>/status/<id>`).",
                ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        try:
            n = await self._post(interaction.channel, tid)
        except Exception as e:
            log.warning("/x get failed for %s (%s)", url, e)
            await interaction.followup.send(
                "❌ Couldn't read that post — it may be deleted, protected, or age-restricted.",
                ephemeral=True)
            return
        if n:
            try:
                await interaction.delete_original_response()
            except discord.HTTPException:
                pass
        else:
            await interaction.followup.send("ℹ️ That post has no photos or videos.", ephemeral=True)

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or not message.guild:
            return
        if not (self.bot.cfg.get("auto_detect_links", True)
                and self.bot.cfg.get("x_auto_detect_links", True)):
            return
        links = twitter.find_links(message.content)
        if not links:
            return
        posted = False
        for url, tid in links[:3]:  # cap per message
            try:
                async with message.channel.typing():
                    posted = bool(await self._post(message.channel, tid)) or posted
            except Exception as e:
                log.info("x auto-download skipped %s (%s)", url, e)
        if posted:  # leave text-only tweets' native embed alone
            await suppress_link_embeds(message, self.bot.cfg)


async def setup(bot):
    await bot.add_cog(Twitter(bot))
