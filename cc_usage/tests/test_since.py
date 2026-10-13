import datetime
import time

import cli
import analyze


def test_parse_since_relative_hours():
    now = 1_000_000.0
    assert cli.parse_since("2h", now=now) == now - 2 * 3600


def test_parse_since_relative_minutes():
    now = 1_000_000.0
    assert cli.parse_since("90m", now=now) == now - 90 * 60


def test_parse_since_relative_days_and_weeks():
    now = 1_000_000.0
    assert cli.parse_since("3d", now=now) == now - 3 * 86400
    assert cli.parse_since("2w", now=now) == now - 2 * 604800


def test_parse_since_iso_date():
    ts = cli.parse_since("2026-08-19")
    dt = datetime.datetime.fromtimestamp(ts)
    assert (dt.year, dt.month, dt.day) == (2026, 8, 19)
    assert dt.hour == 0


def test_parse_since_iso_datetime():
    ts = cli.parse_since("2026-08-19T14:00")
    dt = datetime.datetime.fromtimestamp(ts)
    assert (dt.year, dt.month, dt.day, dt.hour, dt.minute) == (2026, 8, 19, 14, 0)


def test_parse_since_none_returns_none():
    assert cli.parse_since(None) is None


def test_parse_since_invalid_raises():
    import pytest
    with pytest.raises(ValueError):
        cli.parse_since("не дата")


def test_bucket_step_short_window_is_5min():
    step, fmt_kind = analyze.bucket_step(3 * 3600)
    assert step == 300
    assert fmt_kind == "time"


def test_bucket_step_medium_window_is_1hour():
    step, fmt_kind = analyze.bucket_step(24 * 3600)
    assert step == 3600
    assert fmt_kind == "day_time"


def test_bucket_step_long_window_is_1day():
    step, fmt_kind = analyze.bucket_step(10 * 86400)
    assert step == 86400
    assert fmt_kind == "day"


def test_format_bucket_ts_matches_kind():
    ts = time.mktime((2026, 8, 19, 14, 0, 0, 0, 0, -1))
    assert analyze.format_bucket_ts(ts, "time") == "14:00"
    assert "19" in analyze.format_bucket_ts(ts, "day_time")
    assert "авг" in analyze.format_bucket_ts(ts, "day_time")
    assert analyze.format_bucket_ts(ts, "day") == "19.08"
