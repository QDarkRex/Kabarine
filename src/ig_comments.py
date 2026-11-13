"""Read the public comments of one Instagram post with the bot's burner session.

Uses gallery-dl's own Instagram API client (same cookies, headers and request
pacing as the bot's downloads), never a hand-made browser imitation. Calls are
spaced and capped per hour to protect the burner account.
"""
import re
import threading
import time

ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
CODE_RE = re.compile(r"^[A-Za-z0-9_-]{5,40}$")
MIN_SPACING = 20.0      # seconds between IG API calls (matches ig_min_request_spacing)
HOURLY_CAP = 20         # comment lookups per hour
MAX_PAGES = 3           # ~15 comments per page

_lock = threading.Lock()
_last_call = 0.0
_recent = []


class RateLimited(Exception):
    pass


def media_pk(code):
    value = 0
    for ch in code[:11]:
        value = value * 64 + ALPHABET.index(ch)
    return value


def _wait_turn():
    global _last_call
    now = time.time()
    _recent[:] = [t for t in _recent if now - t < 3600]
    if len(_recent) >= HOURLY_CAP:
        raise RateLimited("hourly cap reached")
    wait = _last_call + MIN_SPACING - now
    if wait > 0:
        time.sleep(wait)
    _last_call = time.time()
    _recent.append(_last_call)


def fetch_comments(code, cookies, limit=45):
    """Return {status, comment_count, comments:[{user, text, likes, replies, created_at}]}."""
    if not isinstance(code, str) or not CODE_RE.match(code):
        return {"status": "invalid_code"}
    if not cookies:
        return {"status": "no_session"}
    from gallery_dl import config, extractor
    with _lock:
        _wait_turn()
        config.set(("extractor",), "cookies", cookies)
        ex = extractor.find(f"https://www.instagram.com/p/{code}/")
        ex.initialize()
        comments, total, min_id = [], None, None
        for _page in range(MAX_PAGES):
            params = {"can_support_threading": "true", "permalink_enabled": "false"}
            if min_id:
                params["min_id"] = min_id
            data = ex.api._call(f"/v1/media/{media_pk(code)}/comments/", params=params)
            if total is None:
                total = data.get("comment_count")
            for item in data.get("comments") or []:
                text = str(item.get("text") or "").strip()
                if not text:
                    continue
                comments.append({
                    "user": str((item.get("user") or {}).get("username") or ""),
                    "text": text[:500],
                    "likes": int(item.get("comment_like_count") or 0),
                    "replies": int(item.get("child_comment_count") or 0),
                    "created_at": item.get("created_at"),
                })
            min_id = data.get("next_min_id")
            if not min_id or len(comments) >= limit:
                break
            time.sleep(2)
    comments.sort(key=lambda c: -c["likes"])
    return {"status": "ok", "comment_count": total, "comments": comments[:limit]}
