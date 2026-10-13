import analyze
import store
import tempfile
import os


def _rows(spec):
    """spec: список (ts, cache_write, cache_5m, cache_1h)."""
    return [{"ts": ts, "cache_write": cw, "cache_5m": c5m, "cache_1h": c1h,
             "session_id": "s1"} for ts, cw, c5m, c1h in spec]


def test_gaps_computed_within_session():
    rows = analyze.compute_gaps(_rows([(0, 1, 0, 0), (30, 1, 0, 0), (3600, 1, 0, 0)]))
    assert rows[0]["gap"] is None
    assert rows[1]["gap"] == 30
    assert rows[2]["gap"] == 3570


def test_baseline_needs_enough_active_samples():
    few = analyze.compute_gaps(_rows([(0, 100, 0, 0), (10, 100, 0, 0)]))
    assert analyze.session_baseline(few) is None
    many = analyze.compute_gaps(_rows([(i * 10, 1000, 0, 0) for i in range(8)]))
    assert analyze.session_baseline(many) == 1000


def test_threshold_falls_back_to_absolute():
    assert analyze.spike_threshold(None) == analyze.SPIKE_ABS
    assert analyze.spike_threshold(1000) == analyze.SPIKE_ABS     # 3000 < 20000
    assert analyze.spike_threshold(50000) == 150000               # 3 x baseline


def _active_then(tail_spec):
    """8 активных запросов (пауза 10с, cache_write 1000, без разбивки TTL) + хвост."""
    active = [(i * 10, 1000, 0, 0) for i in range(8)]
    return analyze.compute_gaps(_rows(active + tail_spec))


def test_no_cold_cache_after_10min_pause_when_previous_wrote_1h_cache():
    """Ключевой случай, ради которого переписан детектор: 10 минут — не потеря
    для часового кэша (TTL предыдущего запроса — cache_1h)."""
    rows = _active_then([(80 + 600, 900000, 0, 900000)])
    rows[-2]["cache_1h"] = 5000     # предыдущий (последний активный) запрос писал в 1h-кэш
    events = analyze.find_cold_cache(rows)
    assert events == []


def test_cold_cache_fires_after_70min_pause_for_1h_cache():
    rows = _active_then([(80 + 70 * 60, 900000, 0, 900000)])
    rows[-2]["cache_1h"] = 5000
    events = analyze.find_cold_cache(rows)
    assert len(events) == 1
    assert events[0]["cache_write"] == 900000
    assert events[0]["ttl_s"] == analyze.COLD_CACHE_1H_S


def test_cold_cache_fires_after_10min_pause_for_5m_cache():
    rows = _active_then([(80 + 10 * 60, 900000, 900000, 0)])
    rows[-2]["cache_5m"] = 5000     # предыдущий запрос писал в 5m-кэш
    events = analyze.find_cold_cache(rows)
    assert len(events) == 1
    assert events[0]["ttl_s"] == analyze.COLD_CACHE_5M_S


def test_zero_ttl_split_defaults_to_conservative_1h():
    """Старые записи без разбивки TTL: консервативно считаем часовой кэш,
    поэтому 10-минутная пауза потерей не считается."""
    rows = _active_then([(80 + 600, 900000, 0, 0)])   # предыдущий тоже без разбивки (0, 0)
    events = analyze.find_cold_cache(rows)
    assert events == []


def test_long_pause_with_small_write_is_not_cold_cache():
    """Пауза была, но кэш уцелел — потерь нет."""
    rows = _active_then([(80 + 9000, 500, 0, 0)])
    assert analyze.find_cold_cache(rows) == []


def test_big_write_without_pause_is_not_cold_cache():
    """Большая запись в активной работе — это рост контекста, не протухание."""
    rows = _active_then([(80, 900000, 0, 0)])
    assert analyze.find_cold_cache(rows) == []


def test_cold_cache_summary_windows_by_timestamp():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        conn = store.connect(db_path)
        try:
            requests = [
                {"ts": 100, "cache_write": 1000, "cache_5m": 0, "cache_1h": 1000,
                 "session_id": "s1", "request_id": "r1", "is_sidechain": 0},
                {"ts": 150, "cache_write": 1000, "cache_5m": 0, "cache_1h": 1000,
                 "session_id": "s1", "request_id": "r2", "is_sidechain": 0},
                {"ts": 200, "cache_write": 1000, "cache_5m": 0, "cache_1h": 1000,
                 "session_id": "s1", "request_id": "r3", "is_sidechain": 0},
                {"ts": 250, "cache_write": 1000, "cache_5m": 0, "cache_1h": 1000,
                 "session_id": "s1", "request_id": "r4", "is_sidechain": 0},
                {"ts": 300, "cache_write": 1000, "cache_5m": 0, "cache_1h": 1000,
                 "session_id": "s1", "request_id": "r5", "is_sidechain": 0},
                {"ts": 350, "cache_write": 1000, "cache_5m": 0, "cache_1h": 1000,
                 "session_id": "s1", "request_id": "r6", "is_sidechain": 0},
                {"ts": 400, "cache_write": 1000, "cache_5m": 0, "cache_1h": 1000,
                 "session_id": "s1", "request_id": "r7", "is_sidechain": 0},
                # пауза 4300с (>3600 TTL часового кэша) -> остывший кэш, ts < since=5000
                {"ts": 4700, "cache_write": 900000, "cache_5m": 0, "cache_1h": 900000,
                 "session_id": "s1", "request_id": "r8", "is_sidechain": 0},
                # пауза 4300с -> остывший кэш, ts >= since=5000
                {"ts": 9000, "cache_write": 900000, "cache_5m": 0, "cache_1h": 900000,
                 "session_id": "s1", "request_id": "r9", "is_sidechain": 0},
            ]
            store.insert_requests(conn, requests)

            summary_all = analyze.cold_cache_summary(conn)
            assert summary_all["n_events"] == 2
            assert summary_all["wasted"] == 1800000

            summary_windowed = analyze.cold_cache_summary(conn, since=5000)
            assert summary_windowed["n_events"] == 1
            assert summary_windowed["wasted"] == 900000
            assert summary_windowed["top"][0]["ts"] == 9000
        finally:
            conn.close()
