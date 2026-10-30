"""watchdog/watchdog.py state machine with a fake clock and a fake send."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "watchdog"))
import watchdog  # noqa: E402


def setup(tmp_path, **env):
    clock = [1_000_000.0]
    sent = []
    hb = tmp_path / "hb.json"
    dog = watchdog.Watchdog({"WATCH": f"bot={hb}", **env}, sent.append, now=lambda: clock[0])

    def beat():
        hb.write_text(json.dumps({"ts": clock[0]}), encoding="utf-8")

    def advance(minutes):
        clock[0] += minutes * 60
    return dog, sent, beat, advance


def test_parse_watch():
    assert watchdog.parse_watch("a=/x/hb.json, b = /y/hb.json,junk") == {"a": "/x/hb.json", "b": "/y/hb.json"}


def test_stale_alert_repeat_and_recovery(tmp_path):
    dog, sent, beat, advance = setup(tmp_path)
    beat()
    advance(10)
    dog.check()
    assert sent == []
    advance(6)                                  # 16 min without a beat
    dog.check()
    assert len(sent) == 1 and "bot looks down" in sent[0] and "16 min" in sent[0]
    advance(60)
    dog.check()
    assert len(sent) == 1                       # no spam inside REPEAT_HOURS
    advance(6 * 60)
    dog.check()
    assert len(sent) == 2 and "still the case" in sent[1]
    beat()
    dog.check()
    assert sent[-1] == "✅ bot is alive again."


def test_missing_file_gets_grace_period(tmp_path):
    dog, sent, _beat, advance = setup(tmp_path, LANGUAGE="id")
    dog.check()
    advance(14)
    dog.check()
    assert sent == []
    advance(2)
    dog.check()
    assert len(sent) == 1 and "belum muncul" in sent[0]


def test_corrupt_file_counts_as_missing(tmp_path):
    dog, sent, _beat, advance = setup(tmp_path)
    (tmp_path / "hb.json").write_text("{not json", encoding="utf-8")
    advance(16)
    dog.check()
    assert len(sent) == 1 and "has not appeared" in sent[0]
