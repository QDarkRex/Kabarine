"""X downloads: variant choice under the upload cap, oversize files, temp-dir cleanup."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src import twitter  # noqa: E402

# The real 4K tweet that left a 300 MB file behind (fxtwitter variants, 302 s).
VIDEO = {"type": "video", "duration": 302.333, "url": "https://v/best.mp4", "variants": [
    {"content_type": "video/mp4", "bitrate": 256000, "url": "https://v/256k.mp4"},
    {"content_type": "video/mp4", "bitrate": 832000, "url": "https://v/832k.mp4"},
    {"content_type": "video/mp4", "bitrate": 2176000, "url": "https://v/2m.mp4"},
    {"content_type": "video/mp4", "bitrate": 10368000, "url": "https://v/10m.mp4"},
    {"content_type": "video/mp4", "bitrate": 25128000, "url": "https://v/25m.mp4"},
    {"content_type": "application/x-mpegURL", "url": "https://v/pl.m3u8"}]}


def test_best_variant_that_fits_the_upload_cap():
    assert twitter._best_video_url(VIDEO, 50) == "https://v/832k.mp4"     # ~33 MB
    assert twitter._best_video_url(VIDEO, 100) == "https://v/2m.mp4"      # ~86 MB
    assert twitter._best_video_url(VIDEO, 5) == "https://v/256k.mp4"      # nothing fits: smallest
    assert twitter._best_video_url(VIDEO) == "https://v/25m.mp4"          # no cap: best
    assert twitter._best_video_url({**VIDEO, "duration": None}, 50) == "https://v/25m.mp4"
    assert twitter._best_video_url({"url": "https://v/only.mp4"}, 50) == "https://v/only.mp4"


class FakeResponse:
    def __init__(self, size, fail_after=None):
        self.size, self.fail_after = size, fail_after

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def raise_for_status(self):
        pass

    def iter_content(self, n):
        sent = 0
        while sent < self.size:
            if self.fail_after is not None and sent >= self.fail_after:
                raise ConnectionError("reset")
            chunk = b"x" * min(n, self.size - sent)
            sent += len(chunk)
            yield chunk


def _tweet(*items):
    return {"media": {"all": list(items)}, "author": {"screen_name": "u"}, "url": "https://x.com/u/status/1"}


def test_oversize_item_is_skipped_and_counted(monkeypatch, tmp_path):
    photo = {"type": "photo", "url": "https://p/1.jpg"}
    monkeypatch.setattr(twitter, "fetch", lambda tid: _tweet(photo, VIDEO))
    sizes = {"https://p/1.jpg": 1000, "https://v/832k.mp4": 3 * 1024 * 1024}
    monkeypatch.setattr(twitter.requests, "get", lambda url, **k: FakeResponse(sizes[url]))
    info = twitter.download("1", str(tmp_path), max_mb=2, fit_mb=50)
    assert [os.path.basename(p) for p in info["media"]] == ["1_1.jpg"] and info["oversize"] == 1
    assert sorted(os.listdir(info["dir"])) == ["1_1.jpg"]           # partial video removed


def test_temp_dir_removed_when_download_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(twitter, "fetch", lambda tid: _tweet(VIDEO))
    monkeypatch.setattr(twitter.requests, "get", lambda url, **k: FakeResponse(10 ** 6, fail_after=1))
    with pytest.raises(ConnectionError):
        twitter.download("1", str(tmp_path), fit_mb=50)
    assert os.listdir(tmp_path) == []
