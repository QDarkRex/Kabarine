"""SQLite state (via aiosqlite) for tracked accounts across platforms.

One row per (platform, account, channel, content_type) subscription. `last_seen_id`
is the newest item id already handled for that subscription; seeded on add so we never
dump the back-catalogue. Optional per-subscription mention (role / user / everyone) is
pinged when a new item is forwarded.

`content_type` is 'post' or 'story'. Story markers are compared numerically (TikTok
ids are time-ordered) because the story a marker points at expires after 24h. The old
TikTok-only `accounts` table (if present from an earlier version) is migrated in on
first run.
"""
import aiosqlite

_SCHEMA = """
CREATE TABLE IF NOT EXISTS subscriptions (
    platform     TEXT NOT NULL DEFAULT 'tiktok',
    username     TEXT NOT NULL,
    channel_id   TEXT NOT NULL,
    guild_id     TEXT,
    content_type TEXT NOT NULL DEFAULT 'post',
    mention_type TEXT,           -- 'role' | 'user' | 'everyone' | NULL
    mention_id   TEXT,           -- role/user id ('everyone'/none -> NULL)
    last_seen_id TEXT,
    enabled      INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (platform, username, channel_id, content_type)
);
"""

# Per-account TikTok ids, resolved once from a video page and cached:
#   user_id - numeric; story listing needs it to skip the profile page TikTok answers
#             403 to from the server (src/tiktok_story.py).
#   sec_uid - "MS4wLjABAAAA..."; video listing via `tiktokuser:<sec_uid>` skips the same
#             profile page. Measured 2026-10-30, all 54 accounts at once: the @handle
#             listing failed 35/54, tiktokuser:<sec_uid> 0/54, same videos in same order.
_USER_IDS_SCHEMA = """
CREATE TABLE IF NOT EXISTS tiktok_user_ids (
    username TEXT PRIMARY KEY,
    user_id  TEXT NOT NULL,
    sec_uid  TEXT
);
"""


async def init_db(path):
    async with aiosqlite.connect(path) as db:
        await db.execute(_SCHEMA)
        await db.execute(_USER_IDS_SCHEMA)
        # Tables created before sec_uid existed: add the column (rows get it lazily).
        cur = await db.execute("PRAGMA table_info(tiktok_user_ids)")
        if "sec_uid" not in {row[1] for row in await cur.fetchall()}:
            await db.execute("ALTER TABLE tiktok_user_ids ADD COLUMN sec_uid TEXT")
        # Migrate the old TikTok-only `accounts` table (username, channel_id, guild_id,
        # last_seen_id, enabled) ONCE, then rename it out of the way. It used to be
        # re-run on every start, and INSERT OR IGNORE then quietly re-created every
        # subscription removed since the migration — /tt remove was undone by the next
        # restart (seen 2026-10-26: two deleted accounts came back with their old
        # markers). The renamed table is kept as a backup.
        cur = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='accounts'")
        if await cur.fetchone():
            await db.execute(
                "INSERT OR IGNORE INTO subscriptions "
                "(platform, username, channel_id, guild_id, content_type, last_seen_id, enabled) "
                "SELECT 'tiktok', username, channel_id, guild_id, 'post', last_seen_id, enabled "
                "FROM accounts")
            await db.execute("ALTER TABLE accounts RENAME TO accounts_migrated_backup")
        await db.commit()


async def add_subscription(path, platform, username, channel_id, guild_id,
                           content_type="post", mention_type=None, mention_id=None,
                           last_seen_id=None):
    async with aiosqlite.connect(path) as db:
        await db.execute(
            "INSERT OR REPLACE INTO subscriptions "
            "(platform, username, channel_id, guild_id, content_type, "
            " mention_type, mention_id, last_seen_id, enabled) "
            "VALUES (?,?,?,?,?,?,?,?,1)",
            (platform, username, channel_id, guild_id, content_type,
             mention_type, mention_id, last_seen_id),
        )
        await db.commit()


async def remove_subscription(path, platform, username, channel_id=None, content_type="post"):
    """Remove a subscription. channel_id=None removes it from every channel;
    content_type=None removes every type. Returns number of rows deleted."""
    q = "DELETE FROM subscriptions WHERE platform=? AND username=?"
    args = [platform, username]
    if channel_id:
        q += " AND channel_id=?"
        args.append(channel_id)
    if content_type:
        q += " AND content_type=?"
        args.append(content_type)
    async with aiosqlite.connect(path) as db:
        cur = await db.execute(q, args)
        await db.commit()
        return cur.rowcount


async def get_ids(path, username):
    """(user_id, sec_uid) cached for this account; either may be None."""
    async with aiosqlite.connect(path) as db:
        cur = await db.execute("SELECT user_id, sec_uid FROM tiktok_user_ids WHERE username=?",
                               (username,))
        row = await cur.fetchone()
        return (row[0], row[1]) if row else (None, None)


async def set_ids(path, username, user_id, sec_uid=None):
    """Cache an account's ids. A None sec_uid keeps whatever is already stored."""
    async with aiosqlite.connect(path) as db:
        await db.execute(
            "INSERT INTO tiktok_user_ids (username, user_id, sec_uid) VALUES (?,?,?) "
            "ON CONFLICT(username) DO UPDATE SET user_id=excluded.user_id, "
            "sec_uid=COALESCE(excluded.sec_uid, tiktok_user_ids.sec_uid)",
            (username, user_id, sec_uid))
        await db.commit()


async def get_user_id(path, username):
    return (await get_ids(path, username))[0]


async def set_user_id(path, username, user_id):
    await set_ids(path, username, user_id)


async def known_video_id(path, username):
    """A video id already on record for this TikTok account (a video subscription's
    marker), or None. Lets the account's ids be read from one video page instead of the
    profile listing, which fails often from the server."""
    async with aiosqlite.connect(path) as db:
        cur = await db.execute(
            "SELECT last_seen_id FROM subscriptions WHERE platform='tiktok' AND username=? "
            "AND content_type='post' AND last_seen_id IS NOT NULL AND last_seen_id != '0' LIMIT 1",
            (username,))
        row = await cur.fetchone()
        return row[0] if row else None


async def list_subscriptions(path, platform, guild_id=None):
    q = ("SELECT username, channel_id, content_type, mention_type, mention_id, "
         "last_seen_id, enabled FROM subscriptions WHERE platform=?")
    args = [platform]
    if guild_id:
        q += " AND guild_id=?"
        args.append(guild_id)
    q += " ORDER BY username"
    async with aiosqlite.connect(path) as db:
        cur = await db.execute(q, args)
        return await cur.fetchall()


async def get_enabled(path, platform, content_type="post"):
    """Enabled subs as (username, channel_id, mention_type, mention_id, last_seen_id)."""
    async with aiosqlite.connect(path) as db:
        cur = await db.execute(
            "SELECT username, channel_id, mention_type, mention_id, last_seen_id "
            "FROM subscriptions WHERE platform=? AND content_type=? AND enabled=1",
            (platform, content_type),
        )
        return await cur.fetchall()


async def update_last_seen(path, platform, username, channel_id, last_seen_id,
                           content_type="post"):
    async with aiosqlite.connect(path) as db:
        await db.execute(
            "UPDATE subscriptions SET last_seen_id=? "
            "WHERE platform=? AND username=? AND channel_id=? AND content_type=?",
            (last_seen_id, platform, username, channel_id, content_type),
        )
        await db.commit()
