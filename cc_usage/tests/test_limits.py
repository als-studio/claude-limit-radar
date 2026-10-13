import pytest

import analyze
import store


def _rl(conn, rows):
    store.insert_ratelimits(conn, [
        dict(ts=ts, session_id="s1", five_hour_pct=pct, five_hour_reset=0,
             seven_day_pct=0.0, seven_day_reset=0, ctx_pct=0.0,
             ctx_in=0, cost_usd=0.0, fresh=1) for ts, pct in rows])


def _req(conn, rows):
    store.insert_requests(conn, [
        dict(request_id="r%d" % i, message_id="m%d" % i, session_id="s1",
             ts=ts, model="claude-opus-5", effort=None, service_tier=None,
             input=0, output=0, thinking=0, cache_read=0, cache_write=cw,
             cache_1h=0, cache_5m=0, is_sidechain=0, agent_file=None,
             project="p", cwd=None, git_branch=None, cc_version=None)
        for i, (ts, cw) in enumerate(rows)])


def test_intervals_pair_tokens_with_percent_growth(tmp_path):
    conn = store.connect(tmp_path / "u.sqlite")
    _rl(conn, [(1000, 10.0), (2000, 12.0)])
    _req(conn, [(1500, 1_000_000)])
    iv = analyze.limit_intervals(conn)
    assert len(iv) == 1
    assert iv[0]["d_pct"] == 2.0
    assert iv[0]["tokens"]["cache_write"] == 1_000_000


def test_sliding_window_decay_is_discarded(tmp_path):
    """Проценты убывают сами, когда старые запросы выходят из окна."""
    conn = store.connect(tmp_path / "u.sqlite")
    _rl(conn, [(1000, 50.0), (2000, 45.0), (3000, 48.0)])
    _req(conn, [(1500, 500_000), (2500, 500_000)])
    iv = analyze.limit_intervals(conn)
    assert all(i["d_pct"] > 0 for i in iv)
    assert len(iv) == 1          # убывающий интервал отброшен


def test_weights_report_low_confidence_on_thin_data(tmp_path):
    conn = store.connect(tmp_path / "u.sqlite")
    _rl(conn, [(1000, 10.0), (2000, 12.0)])
    _req(conn, [(1500, 1_000_000)])
    w = analyze.limit_weights(conn)
    assert w["confidence"] in ("none", "low")
    assert w["note"]             # оговорка обязательна

def test_weights_on_empty_db(tmp_path):
    conn = store.connect(tmp_path / "u.sqlite")
    w = analyze.limit_weights(conn)
    assert w["confidence"] == "none"
    assert w["n_intervals"] == 0


def test_intervals_pair_tokens_with_percent_growth_seven_day(tmp_path):
    conn = store.connect(tmp_path / "u.sqlite")
    store.insert_ratelimits(conn, [
        dict(ts=1000, session_id="s1", five_hour_pct=0.0, five_hour_reset=0,
             seven_day_pct=10.0, seven_day_reset=0, ctx_pct=0.0, ctx_in=0, cost_usd=0.0,
             fresh=1),
        dict(ts=2000, session_id="s1", five_hour_pct=0.0, five_hour_reset=0,
             seven_day_pct=12.0, seven_day_reset=0, ctx_pct=0.0, ctx_in=0, cost_usd=0.0,
             fresh=1),
    ])
    _req(conn, [(1500, 1_000_000)])
    iv = analyze.limit_intervals(conn, pct_field="seven_day_pct")
    assert len(iv) == 1
    assert iv[0]["d_pct"] == 2.0
    assert iv[0]["shares"][0]["session_id"] == "s1"


def test_intervals_default_pct_field_is_five_hour(tmp_path):
    """Обратная совместимость: старые вызовы без pct_field продолжают читать five_hour_pct."""
    conn = store.connect(tmp_path / "u.sqlite")
    _rl(conn, [(1000, 10.0), (2000, 12.0)])
    _req(conn, [(1500, 1_000_000)])
    iv = analyze.limit_intervals(conn)
    assert len(iv) == 1
    assert iv[0]["d_pct"] == 2.0
    assert iv[0]["shares"][0]["session_id"] == "s1"


def test_hard_reset_drop_is_discarded_like_sliding_decay(tmp_path):
    """Резкий сброс по расписанию (7d) даёт один сильно отрицательный d_pct —
    growth-only фильтр отбрасывает его так же, как плавное скользящее убывание."""
    conn = store.connect(tmp_path / "u.sqlite")
    store.insert_ratelimits(conn, [
        dict(ts=1000, session_id="s1", five_hour_pct=0.0, five_hour_reset=0,
             seven_day_pct=90.0, seven_day_reset=0, ctx_pct=0.0, ctx_in=0, cost_usd=0.0,
             fresh=1),
        dict(ts=2000, session_id="s1", five_hour_pct=0.0, five_hour_reset=0,
             seven_day_pct=1.0, seven_day_reset=0, ctx_pct=0.0, ctx_in=0, cost_usd=0.0,
             fresh=1),  # сброс
        dict(ts=3000, session_id="s1", five_hour_pct=0.0, five_hour_reset=0,
             seven_day_pct=4.0, seven_day_reset=0, ctx_pct=0.0, ctx_in=0, cost_usd=0.0,
             fresh=1),
    ])
    _req(conn, [(1500, 500_000), (2500, 500_000)])
    iv = analyze.limit_intervals(conn, pct_field="seven_day_pct")
    assert all(i["d_pct"] > 0 for i in iv)
    assert len(iv) == 1          # интервал через сброс отброшен


def test_weights_seven_day_low_confidence_on_thin_data(tmp_path):
    conn = store.connect(tmp_path / "u.sqlite")
    store.insert_ratelimits(conn, [
        dict(ts=1000, session_id="s1", five_hour_pct=0.0, five_hour_reset=0,
             seven_day_pct=10.0, seven_day_reset=0, ctx_pct=0.0, ctx_in=0, cost_usd=0.0),
        dict(ts=2000, session_id="s1", five_hour_pct=0.0, five_hour_reset=0,
             seven_day_pct=12.0, seven_day_reset=0, ctx_pct=0.0, ctx_in=0, cost_usd=0.0),
    ])
    _req(conn, [(1500, 1_000_000)])
    w = analyze.limit_weights(conn, pct_field="seven_day_pct")
    assert w["confidence"] in ("none", "low")
    assert w["note"]


def test_snapshot_empty_db_is_none(tmp_path):
    conn = store.connect(tmp_path / "u.sqlite")
    assert analyze.limit_snapshot(conn) is None


def test_snapshot_returns_latest_in_window_not_globally_latest(tmp_path):
    conn = store.connect(tmp_path / "u.sqlite")
    store.insert_ratelimits(conn, [
        dict(ts=1000, session_id="s1", five_hour_pct=10.0, five_hour_reset=0,
             seven_day_pct=20.0, seven_day_reset=0, ctx_pct=0.0, ctx_in=0, cost_usd=0.0),
        dict(ts=2000, session_id="s1", five_hour_pct=15.0, five_hour_reset=0,
             seven_day_pct=25.0, seven_day_reset=0, ctx_pct=0.0, ctx_in=0, cost_usd=0.0),
        dict(ts=9000, session_id="s1", five_hour_pct=90.0, five_hour_reset=0,
             seven_day_pct=95.0, seven_day_reset=0, ctx_pct=0.0, ctx_in=0, cost_usd=0.0),
    ])
    snap = analyze.limit_snapshot(conn, since=0, until=3000)
    assert snap["five_hour_pct"] == 15.0
    assert snap["seven_day_pct"] == 25.0


def test_snapshot_prefers_fresh_mark_over_later_stale_echo(tmp_path):
    """Карточка "Лимит на момент отчёта" не должна показывать застывшее эхо.

    Fix wave 2, находка 4 (Codex): limit_snapshot() брал последний замер по
    ts, не проверяя fresh. Простаивающая сессия эхом повторяет старое
    значение позже по времени, чем последний РЕАЛЬНЫЙ (свежий) замер —
    отчёт показывал бы застывшее число как "лимит сейчас".
    """
    conn = store.connect(tmp_path / "u.sqlite")
    store.insert_ratelimits(conn, [
        dict(ts=1000, session_id="a", five_hour_pct=10.0, five_hour_reset=0,
             seven_day_pct=10.0, seven_day_reset=0, ctx_pct=0.0, ctx_in=0,
             cost_usd=0.0, fresh=1),
        dict(ts=5000, session_id="idle", five_hour_pct=99.0, five_hour_reset=0,
             seven_day_pct=99.0, seven_day_reset=0, ctx_pct=0.0, ctx_in=0,
             cost_usd=0.0, fresh=0),
    ])
    snap = analyze.limit_snapshot(conn)
    assert snap["ts"] == 1000
    assert snap["five_hour_pct"] == 10.0
    assert snap["fresh"] is True
    assert snap["source"] == "statusline"


def test_snapshot_falls_back_to_last_row_and_flags_stale(tmp_path):
    """Ни одного свежего замера в окне и нет usage_snapshots — берём
    последний ratelimits-замер, но честно помечаем его устаревшим."""
    conn = store.connect(tmp_path / "u.sqlite")
    store.insert_ratelimits(conn, [
        dict(ts=1000, session_id="idle", five_hour_pct=13.0, five_hour_reset=0,
             seven_day_pct=13.0, seven_day_reset=0, ctx_pct=0.0, ctx_in=0,
             cost_usd=0.0, fresh=0),
    ])
    snap = analyze.limit_snapshot(conn)
    assert snap["five_hour_pct"] == 13.0
    assert snap["fresh"] is False
    assert snap["source"] == "statusline"


# --- Стадия B: usage_snapshots (/api/oauth/usage) как источник ------------
# Стадия B: "analyze/render учатся
# брать окна лимита из usage_snapshots, когда в ratelimits нет свежих
# fresh-замеров; в отчёте видно, откуда взята цифра".

def _us(conn, rows):
    store.insert_usage_snapshots(conn, [
        dict(ts=ts, five_hour_pct=five_hour_pct, five_hour_reset=0,
             seven_day_pct=seven_day_pct, seven_day_reset=0,
             scoped_pct=None, scoped_model=None, scoped_reset=None,
             extra_enabled=None, extra_pct=None, severity=None, raw="{}")
        for ts, five_hour_pct, seven_day_pct in rows])


def test_snapshot_falls_back_to_usage_api_when_no_ratelimits_at_all(tmp_path):
    """statusLine не настроен (ratelimits пуст) — берём последний poll
    /api/oauth/usage вместо того, чтобы объявлять "нет данных"."""
    conn = store.connect(tmp_path / "u.sqlite")
    _us(conn, [(1000, 5.0, 20.0)])
    snap = analyze.limit_snapshot(conn)
    assert snap["five_hour_pct"] == 5.0
    assert snap["seven_day_pct"] == 20.0
    assert snap["fresh"] is True
    assert snap["source"] == "usage_api"


def test_snapshot_falls_back_to_usage_api_over_stale_ratelimits_echo(tmp_path):
    """Единственный ratelimits-замер в окне — устаревшее эхо (fresh=0), но
    есть более свежий poll через usage_api — предпочесть его устаревшему эху."""
    conn = store.connect(tmp_path / "u.sqlite")
    store.insert_ratelimits(conn, [
        dict(ts=1000, session_id="idle", five_hour_pct=99.0, five_hour_reset=0,
             seven_day_pct=99.0, seven_day_reset=0, ctx_pct=0.0, ctx_in=0,
             cost_usd=0.0, fresh=0),
    ])
    _us(conn, [(2000, 7.0, 30.0)])
    snap = analyze.limit_snapshot(conn)
    assert snap["five_hour_pct"] == 7.0
    assert snap["seven_day_pct"] == 30.0
    assert snap["source"] == "usage_api"


def test_snapshot_prefers_fresh_ratelimits_over_usage_api(tmp_path):
    """Живой fresh-замер statusline точнее периодического poll — приоритет
    у него, даже если usage_api формально свежее по времени."""
    conn = store.connect(tmp_path / "u.sqlite")
    store.insert_ratelimits(conn, [
        dict(ts=1000, session_id="a", five_hour_pct=10.0, five_hour_reset=0,
             seven_day_pct=10.0, seven_day_reset=0, ctx_pct=0.0, ctx_in=0,
             cost_usd=0.0, fresh=1),
    ])
    _us(conn, [(5000, 50.0, 50.0)])
    snap = analyze.limit_snapshot(conn)
    assert snap["five_hour_pct"] == 10.0
    assert snap["source"] == "statusline"


def test_snapshot_falls_back_to_stale_ratelimits_when_usage_api_outside_window(tmp_path):
    """usage_api существует, но вне запрошенного окна — не подмешивать его,
    вернуть устаревший ratelimits-замер как раньше."""
    conn = store.connect(tmp_path / "u.sqlite")
    store.insert_ratelimits(conn, [
        dict(ts=1000, session_id="idle", five_hour_pct=13.0, five_hour_reset=0,
             seven_day_pct=13.0, seven_day_reset=0, ctx_pct=0.0, ctx_in=0,
             cost_usd=0.0, fresh=0),
    ])
    _us(conn, [(50_000, 7.0, 30.0)])
    snap = analyze.limit_snapshot(conn, since=0, until=2000)
    assert snap["five_hour_pct"] == 13.0
    assert snap["source"] == "statusline"


def _rl_for(conn, session_id, rows):
    store.insert_ratelimits(conn, [
        dict(ts=ts, session_id=session_id, five_hour_pct=pct, five_hour_reset=0,
             seven_day_pct=0.0, seven_day_reset=0, ctx_pct=0.0, ctx_in=0, cost_usd=0.0)
        for ts, pct in rows])


def test_session_pct_share_attributes_growth_to_session(tmp_path):
    # Счётчик лимита аккаунт-wide — один ряд, а не пара независимых
    # per-session рядов (см. limit_events). Прирост между двумя замерами
    # делится между s1 и s2 пропорционально их токенам внутри интервала.
    conn = store.connect(tmp_path / "u.sqlite")
    _rl_fresh(conn, [(1000, "s1", 10.0, 1), (2000, "s1", 17.0, 1)])
    _req_full(conn, [(1500, "s1", 0, 2_000_000, 0), (1600, "s2", 0, 5_000_000, 0)])
    share = analyze.session_pct_share(conn, since=None, until=None)
    assert share["s1"] == 2.0
    assert share["s2"] == 5.0


def test_session_pct_share_omits_sessions_without_growth(tmp_path):
    conn = store.connect(tmp_path / "u.sqlite")
    _rl_for(conn, "s1", [(1000, 10.0)])   # один замер — интервалов нет
    share = analyze.session_pct_share(conn, since=None, until=None)
    assert "s1" not in share


def test_limit_spike_none_below_medium_confidence(tmp_path):
    conn = store.connect(tmp_path / "u.sqlite")
    _rl_for(conn, "s1", [(1000, 10.0), (2000, 12.0)])
    _req(conn, [(1500, 1_000_000)])
    assert analyze._diagnose_limit_spike(conn, None, None, "five_hour_pct") is None


def test_limit_spike_attributes_biggest_interval_at_medium_confidence(tmp_path):
    conn = store.connect(tmp_path / "u.sqlite")
    # 100 маленьких интервалов в "фоновой" сессии — набирают medium confidence
    rows = []
    for i in range(101):
        rows.append(dict(ts=1000 + i * 10, session_id="bg", five_hour_pct=i * 0.1,
                         five_hour_reset=0, seven_day_pct=0.0, seven_day_reset=0,
                         ctx_pct=0.0, ctx_in=0, cost_usd=0.0))
    store.insert_ratelimits(conn, rows)
    store.insert_requests(conn, [
        dict(request_id="bg%d" % i, message_id="bg%d" % i, session_id="bg",
             ts=1000 + i * 10 + 5, model="claude-opus-5", effort=None, service_tier=None,
             input=0, output=0, thinking=0, cache_read=0, cache_write=1000,
             cache_1h=0, cache_5m=0, is_sidechain=0, agent_file=None,
             project="p", cwd=None, git_branch=None, cc_version=None)
        for i in range(100)])
    # один явно доминирующий скачок в другой сессии
    _rl_fresh(conn, [(5000, "spike", 0.0, 1), (5100, "spike", 50.0, 1)])
    store.insert_requests(conn, [
        dict(request_id="sp1", message_id="sp1", session_id="spike", ts=5050,
             model="claude-opus-5", effort=None, service_tier=None, input=0, output=0,
             thinking=0, cache_read=0, cache_write=5_000_000, cache_1h=0, cache_5m=0,
             is_sidechain=0, agent_file=None, project="p", cwd=None, git_branch=None,
             cc_version=None)])

    w = analyze.limit_weights(conn, pct_field="five_hour_pct")
    assert w["confidence"] == "medium"

    d = analyze._diagnose_limit_spike(conn, None, None, "five_hour_pct")
    assert d is not None
    assert d["where"][0]["session_id"] == "spike"
    assert d["fact_pct"] == 50.0
    assert d["confidence"] == "medium"


def test_limit_spike_gate_uses_windowed_confidence_not_global(tmp_path):
    """Confidence-гейт Д←5 должен относиться к ОКНУ отчёта, не ко всей БД.

    Fix wave 2, находка 8 (Kimi): limit_weights() без since/until считает
    confidence по всей истории; окно --since 24h могло унаследовать
    "medium" от старых данных, хотя в самом окне — один-два интервала,
    статистически ничем не отличающиеся от случая, который confidence и
    должна отсекать.

    Фикстура — та же, что в
    test_limit_spike_attributes_biggest_interval_at_medium_confidence (101
    интервал => глобальный confidence "medium"), но окно отчёта сужено до
    одного интервала спайка — там его меньше MIN_INTERVALS_MEDIUM.
    """
    conn = store.connect(tmp_path / "u.sqlite")
    rows = []
    for i in range(101):
        rows.append(dict(ts=1000 + i * 10, session_id="bg", five_hour_pct=i * 0.1,
                         five_hour_reset=0, seven_day_pct=0.0, seven_day_reset=0,
                         ctx_pct=0.0, ctx_in=0, cost_usd=0.0))
    store.insert_ratelimits(conn, rows)
    store.insert_requests(conn, [
        dict(request_id="bg%d" % i, message_id="bg%d" % i, session_id="bg",
             ts=1000 + i * 10 + 5, model="claude-opus-5", effort=None, service_tier=None,
             input=0, output=0, thinking=0, cache_read=0, cache_write=1000,
             cache_1h=0, cache_5m=0, is_sidechain=0, agent_file=None,
             project="p", cwd=None, git_branch=None, cc_version=None)
        for i in range(100)])
    _rl_fresh(conn, [(5000, "spike", 0.0, 1), (5100, "spike", 50.0, 1)])
    store.insert_requests(conn, [
        dict(request_id="sp1", message_id="sp1", session_id="spike", ts=5050,
             model="claude-opus-5", effort=None, service_tier=None, input=0, output=0,
             thinking=0, cache_read=0, cache_write=5_000_000, cache_1h=0, cache_5m=0,
             is_sidechain=0, agent_file=None, project="p", cwd=None, git_branch=None,
             cc_version=None)])

    assert analyze.limit_weights(conn, pct_field="five_hour_pct")["confidence"] == "medium"
    d = analyze._diagnose_limit_spike(conn, 4900, 5200, "five_hour_pct")
    assert d is None


def test_session_pct_share_respects_window(tmp_path):
    # s1 растёт внутри окна [0, 3000), s2 растёт вне его — только s1 должен
    # попасть в сумму (см. Fix 8, финальное ревью: since/until фильтр по
    # iv["t1"] раньше не был покрыт тестом).
    conn = store.connect(tmp_path / "u.sqlite")
    _rl_fresh(conn, [(1000, "s1", 10.0, 1), (2000, "s1", 12.0, 1)])
    _rl_fresh(conn, [(5000, "s2", 0.0, 1), (6000, "s2", 5.0, 1)])
    store.insert_requests(conn, [
        dict(request_id="a1", message_id="a1", session_id="s1", ts=1500, model="claude-opus-5",
             effort=None, service_tier=None, input=0, output=0, thinking=0, cache_read=0,
             cache_write=1_000_000, cache_1h=0, cache_5m=0, is_sidechain=0, agent_file=None,
             project="p", cwd=None, git_branch=None, cc_version=None),
        dict(request_id="a2", message_id="a2", session_id="s2", ts=5500, model="claude-opus-5",
             effort=None, service_tier=None, input=0, output=0, thinking=0, cache_read=0,
             cache_write=1_000_000, cache_1h=0, cache_5m=0, is_sidechain=0, agent_file=None,
             project="p", cwd=None, git_branch=None, cc_version=None),
    ])
    share = analyze.session_pct_share(conn, since=0, until=3000)
    assert share == {"s1": 2.0}


def test_limit_summary_shape_on_empty_db(tmp_path):
    conn = store.connect(tmp_path / "u.sqlite")
    s = analyze.limit_summary(conn)
    assert s["snapshot"] is None
    assert s["five_hour"]["diagnosis"] is None
    assert s["seven_day"]["diagnosis"] is None
    assert s["five_hour"]["weights"]["confidence"] == "none"


def _rl_fresh(conn, rows):
    """rows: (ts, session_id, pct, fresh)."""
    store.insert_ratelimits(conn, [
        dict(ts=ts, session_id=sid, five_hour_pct=pct, five_hour_reset=0,
             seven_day_pct=pct, seven_day_reset=0, ctx_pct=0.0,
             ctx_in=0, cost_usd=0.0, fresh=fresh)
        for ts, sid, pct, fresh in rows])


def _req_at(conn, rows):
    """rows: (ts, session_id, cache_write). Токены нужны, чтобы был расход."""
    store.insert_requests(conn, [
        dict(request_id="r%d" % i, message_id="m%d" % i, session_id=sid,
             ts=ts, model="claude-opus-5", effort=None, service_tier=None,
             input=0, output=0, thinking=0, cache_read=0, cache_write=cw,
             cache_1h=0, cache_5m=0, is_sidechain=0, agent_file=None,
             project="p", cwd=None, git_branch=None, cc_version=None)
        for i, (ts, sid, cw) in enumerate(rows)])


def test_events_use_global_series_across_sessions(tmp_path):
    """Счётчик аккаунт-wide: ряд один на все сессии, а не по сессии."""
    conn = store.connect(tmp_path / "u.sqlite")
    _rl_fresh(conn, [(1000, "a", 10.0, 1), (1100, "b", 12.0, 1)])
    ev = analyze.limit_events(conn)
    assert len(ev) == 1
    assert ev[0]["d_pct"] == 2.0
    assert (ev[0]["t0"], ev[0]["t1"]) == (1000, 1100)


def test_stale_echo_is_excluded(tmp_path):
    """fresh=0 не попадает в ряд, даже если запрос рядом есть."""
    conn = store.connect(tmp_path / "u.sqlite")
    _rl_fresh(conn, [(1000, "a", 10.0, 1), (1100, "a", 99.0, 0),
                     (1200, "a", 11.0, 1)])
    _req_at(conn, [(1050, "a", 1_000_000)])
    ev = analyze.limit_events(conn)
    assert len(ev) == 1
    assert ev[0]["d_pct"] == 1.0          # 10 -> 11, эхо 99 отброшено


def test_frozen_session_produces_no_events(tmp_path):
    """Простаивающая сессия повторяет застывшее значение — событий нет."""
    conn = store.connect(tmp_path / "u.sqlite")
    _rl_fresh(conn, [(1000 + i * 15, "idle", 13.0, 0) for i in range(50)])
    assert analyze.limit_events(conn) == []


def test_interleaved_stale_sessions_produce_no_sawtooth(tmp_path):
    """Регресс на +6592 пп: чередование сессий с разным возрастом кэша.

    Без гейта свежести пары 13->19->13->19 читались бы как непрерывный рост.
    """
    conn = store.connect(tmp_path / "u.sqlite")
    rows = []
    for i in range(20):
        rows.append((1000 + i * 20, "a", 13.0, 0))
        rows.append((1010 + i * 20, "b", 19.0, 0))
    _rl_fresh(conn, rows)
    assert analyze.limit_events(conn) == []


def test_null_fresh_resolved_by_requests(tmp_path):
    """Старые строки: свежесть восстанавливается сшивкой с requests."""
    conn = store.connect(tmp_path / "u.sqlite")
    _rl_fresh(conn, [(1000, "a", 10.0, None), (2000, "a", 12.0, None)])
    _req_at(conn, [(990, "a", 1_000_000), (1990, "a", 1_000_000)])
    ev = analyze.limit_events(conn)
    assert len(ev) == 1
    assert ev[0]["d_pct"] == 2.0


def test_null_fresh_without_recent_request_is_dropped(tmp_path):
    """Запрос был давно — значение в замере устарело."""
    conn = store.connect(tmp_path / "u.sqlite")
    _rl_fresh(conn, [(1000, "a", 10.0, None), (5000, "a", 12.0, None)])
    _req_at(conn, [(990, "a", 1_000_000)])   # к ts=5000 отстал на 4010 c
    assert analyze.limit_events(conn) == []


def test_events_ignore_decrease(tmp_path):
    """Сброс окна или остаточное расхождение — не событие."""
    conn = store.connect(tmp_path / "u.sqlite")
    _rl_fresh(conn, [(1000, "a", 50.0, 1), (2000, "a", 5.0, 1)])
    assert analyze.limit_events(conn) == []


def test_events_respect_window(tmp_path):
    """Окно не глотает событие целиком, если его рост начался ДО since.

    Раньше since=8000 отсекал замер на 1100 (12.0) вместе с ним самим —
    видна была только пара внутри окна (9000->9100, +5.0), а прирост
    1100->9000 (+8.0), защёлкнувшийся уже ВНУТРИ окна, терялся полностью
    (fix-wave-2, находка 6). Якорный замер перед since восстанавливает эту
    пару, не выходя за пределы окна больше одного шага назад.
    """
    conn = store.connect(tmp_path / "u.sqlite")
    _rl_fresh(conn, [(1000, "a", 10.0, 1), (1100, "a", 12.0, 1),
                     (9000, "a", 20.0, 1), (9100, "a", 25.0, 1)])
    ev = analyze.limit_events(conn, since=8000)
    assert len(ev) == 2
    assert ev[0]["t0"] == 1100
    assert ev[0]["t1"] == 9000
    assert ev[0]["d_pct"] == 8.0
    assert ev[1]["d_pct"] == 5.0


def test_events_reject_unknown_field(tmp_path):
    conn = store.connect(tmp_path / "u.sqlite")
    with pytest.raises(ValueError):
        analyze.limit_events(conn, pct_field="ctx_pct")


def _req_full(conn, rows):
    """rows: (ts, session_id, cache_read, cache_write, is_sidechain)."""
    store.insert_requests(conn, [
        dict(request_id="r%d" % i, message_id="m%d" % i, session_id=sid,
             ts=ts, model="claude-opus-5", effort=None, service_tier=None,
             input=0, output=0, thinking=0, cache_read=cr, cache_write=cw,
             cache_1h=0, cache_5m=0, is_sidechain=side, agent_file=None,
             project="p", cwd=None, git_branch=None, cc_version=None)
        for i, (ts, sid, cr, cw, side) in enumerate(rows)])


def test_event_splits_by_tokens(tmp_path):
    """Прирост делится пропорционально токенам, сумма долей равна d_pct."""
    conn = store.connect(tmp_path / "u.sqlite")
    _rl_fresh(conn, [(1000, "a", 10.0, 1), (2000, "a", 20.0, 1)])
    _req_full(conn, [(1500, "a", 0, 3_000_000, 0), (1600, "b", 0, 1_000_000, 0)])
    ev = analyze.limit_events(conn)
    assert len(ev) == 1
    e = ev[0]
    assert e["kind"] == "attributed"
    assert [s["session_id"] for s in e["shares"]] == ["a", "b"]
    assert e["shares"][0]["pct"] == pytest.approx(7.5)
    assert e["shares"][1]["pct"] == pytest.approx(2.5)
    assert sum(s["pct"] for s in e["shares"]) == pytest.approx(e["d_pct"])
    assert e["n_candidates"] == 2
    assert e["width_s"] == 1000


def test_limit_events_include_growth_that_started_before_since(tmp_path):
    """Событие, чей рост начался до since, не должно пропадать целиком.

    Fix wave 2, находка 6 (Codex, "события на левой границе теряются"):
    _fresh_marks() строил пары только из замеров внутри окна — прирост,
    зафиксированный ДО since, но защёлкнувшийся замером ВНУТРИ окна,
    терялся полностью, а не частично.
    """
    conn = store.connect(tmp_path / "u.sqlite")
    _rl_fresh(conn, [(1000, "a", 10.0, 1), (2000, "a", 20.0, 1)])
    _req_full(conn, [(1500, "a", 0, 1_000_000, 0)])
    ev = analyze.limit_events(conn, since=1500)   # since между t0=1000 и t1=2000
    assert len(ev) == 1
    assert ev[0]["t0"] == 1000
    assert ev[0]["t1"] == 2000
    assert ev[0]["d_pct"] == 10.0


def test_limit_events_anchor_does_not_leak_when_nothing_after_since(tmp_path):
    """Якорь перед since не создаёт фантомное событие, если пар для него нет."""
    conn = store.connect(tmp_path / "u.sqlite")
    _rl_fresh(conn, [(1000, "a", 10.0, 1)])
    ev = analyze.limit_events(conn, since=1500)
    assert ev == []


def test_event_without_requests_is_elsewhere(tmp_path):
    """Аккаунт общий: расход мог прийти с другой машины."""
    conn = store.connect(tmp_path / "u.sqlite")
    _rl_fresh(conn, [(1000, "a", 10.0, 1), (2000, "a", 20.0, 1)])
    ev = analyze.limit_events(conn)
    assert ev[0]["kind"] == "elsewhere"
    assert ev[0]["shares"] == []
    assert ev[0]["n_candidates"] == 0


def test_event_with_own_session_request_just_before_t0_is_elsewhere(tmp_path):
    """Запрос той же сессии, что породила t0, — не признак неоднозначности.

    Fix wave 2, находка 3: fresh=1 у замера t0 означает "сессия только что
    сходила в API" — значит запрос этой же сессии почти вплотную перед t0
    присутствует ВСЕГДА, структурно, независимо от того, было ли реальное
    внешнее потребление в (t0, t1]. Раньше ЛЮБОЙ запрос в (t0-FRESH_LAG, t0]
    (включая этот гарантированный) переводил событие в "unresolved" — то
    есть ветка срабатывала по конструкции, а не по признаку неоднозначности,
    и реальные "расход не с этой машины" тонули в "unresolved" на любой
    активной машине. Собственный запрос сессии-источника t0 из проверки
    исключается: остаётся исходная семантика spec — интервал пуст, других
    кандидатов нет → "не с этой машины".
    """
    conn = store.connect(tmp_path / "u.sqlite")
    _rl_fresh(conn, [(1000, "a", 10.0, 1), (2000, "a", 20.0, 1)])
    _req_full(conn, [(990, "a", 0, 1_000_000, 0)])   # 10 c до t0, та же сессия "a"
    e = analyze.limit_events(conn)[0]
    assert e["kind"] == "elsewhere"
    assert e["shares"] == []
    assert e["n_candidates"] == 0


def test_event_with_other_session_request_just_before_t0_is_unresolved(tmp_path):
    """Запрос ДРУГОЙ сессии рядом с t0 — настоящая неоднозначность.

    В отличие от предыдущего теста, здесь запрос не гарантирован структурой
    свежести: сессия "b" могла быть активна независимо от того, что вызвало
    рост t0→t1. Раз мы не знаем, чья это активность, — честный ответ
    "не атрибутировано", а не уверенное "не с этой машины".
    """
    conn = store.connect(tmp_path / "u.sqlite")
    _rl_fresh(conn, [(1000, "a", 10.0, 1), (2000, "a", 20.0, 1)])
    _req_full(conn, [(990, "b", 0, 1_000_000, 0)])   # 10 c до t0, ДРУГАЯ сессия
    e = analyze.limit_events(conn)[0]
    assert e["kind"] == "unresolved"
    assert e["shares"] == []
    assert e["n_candidates"] == 0


def test_event_with_only_sidechain_is_background(tmp_path):
    conn = store.connect(tmp_path / "u.sqlite")
    _rl_fresh(conn, [(1000, "a", 10.0, 1), (2000, "a", 20.0, 1)])
    _req_full(conn, [(1500, "a", 0, 1_000_000, 1)])
    assert analyze.limit_events(conn)[0]["kind"] == "background"


def test_event_with_zero_tokens_is_unexplained(tmp_path):
    conn = store.connect(tmp_path / "u.sqlite")
    _rl_fresh(conn, [(1000, "a", 10.0, 1), (2000, "a", 20.0, 1)])
    _req_full(conn, [(1500, "a", 0, 0, 0)])
    assert analyze.limit_events(conn)[0]["kind"] == "unexplained"


def test_event_reports_composition(tmp_path):
    conn = store.connect(tmp_path / "u.sqlite")
    _rl_fresh(conn, [(1000, "a", 10.0, 1), (2000, "a", 20.0, 1)])
    _req_full(conn, [(1500, "a", 900_000, 100_000, 0)])
    e = analyze.limit_events(conn)[0]
    assert e["cache_read_share"] == pytest.approx(0.9)
    assert e["cache_write_share"] == pytest.approx(0.1)
    assert e["peak_context"] == 900_000
    assert e["models"] == ["claude-opus-5"]


def test_intervals_drop_events_without_tokens(tmp_path):
    """Сироты — результат limit_events, но не основание для весов."""
    conn = store.connect(tmp_path / "u.sqlite")
    _rl_fresh(conn, [(1000, "a", 10.0, 1), (2000, "a", 20.0, 1),
                     (3000, "a", 25.0, 1)])
    _req_full(conn, [(2500, "a", 0, 1_000_000, 0)])
    assert len(analyze.limit_events(conn)) == 2
    iv = analyze.limit_intervals(conn)
    assert len(iv) == 1
    assert iv[0]["d_pct"] == 5.0


def test_session_pct_share_splits_between_sessions(tmp_path):
    conn = store.connect(tmp_path / "u.sqlite")
    _rl_fresh(conn, [(1000, "a", 10.0, 1), (2000, "a", 20.0, 1)])
    _req_full(conn, [(1500, "a", 0, 3_000_000, 0), (1600, "b", 0, 1_000_000, 0)])
    share = analyze.session_pct_share(conn, None, None)
    assert share["a"] == pytest.approx(7.5)
    assert share["b"] == pytest.approx(2.5)


def test_limit_curve_for_buckets_holds_last_fresh_value(tmp_path):
    conn = store.connect(tmp_path / "u.sqlite")
    _rl_fresh(conn, [(1000, "a", 10.0, 1), (3000, "a", 20.0, 1)])
    buckets = [{"ts": ts} for ts in (500, 1500, 2500, 3500)]
    curve = analyze.limit_curve_for_buckets(conn, buckets, pct_field="five_hour_pct")
    assert curve == [None, 10.0, 10.0, 20.0]


def test_mod_and_statusline_in_one_session_produce_no_sawtooth(tmp_path):
    """В терминале пишут оба источника: statusline целыми (43), мод с
    десятыми (43.4). Чередование 43 -> 43.4 -> 43 -> 43.4 читалось бы как
    повторный рост на 0.4; у сессии с замерами мода statusline не берём."""
    conn = store.connect(tmp_path / "u.sqlite")
    rows = []
    for i in range(5):
        rows.append(dict(ts=1000 + i * 20, session_id="a", five_hour_pct=43.0,
                         fresh=1, source=None))
        rows.append(dict(ts=1010 + i * 20, session_id="a", five_hour_pct=43.4,
                         fresh=1, source="mod"))
    store.insert_ratelimits(conn, rows)
    assert analyze.limit_events(conn) == []


# --- Шаг 4 limit-radar-mod: снимки /api/oauth/usage в общем ряду ----------
# Радар на моде, шаг 4. Снимок — замер без
# сессии: рост между ответами сессий (простой, claude.ai, другая машина)
# должен становиться событием, а не пропадать.

def _rl_mod(conn, rows):
    """rows: (ts, session_id, pct). Замеры мода, свежие."""
    store.insert_ratelimits(conn, [
        dict(ts=ts, session_id=sid, five_hour_pct=pct, seven_day_pct=pct,
             fresh=1, source="mod")
        for ts, sid, pct in rows])


def test_snapshot_only_series_produces_events(tmp_path):
    conn = store.connect(tmp_path / "u.sqlite")
    _us(conn, [(1000, 10.0, 30.0), (1600, 12.0, 30.0), (2200, 15.0, 31.0)])
    ev = analyze.limit_events(conn)
    assert [(e["t0"], e["t1"], e["d_pct"]) for e in ev] == [
        (1000, 1600, 2.0), (1600, 2200, 3.0)]
    assert all(e["kind"] == "elsewhere" for e in ev)
    ev7 = analyze.limit_events(conn, pct_field="seven_day_pct")
    assert [(e["t0"], e["t1"], e["d_pct"]) for e in ev7] == [(1600, 2200, 1.0)]


def test_snapshot_splits_idle_growth_between_mod_marks(tmp_path):
    """Сессия ответила, ушла в простой, рост пришёл из claude.ai, потом
    сессия снова ответила. Без снимка это одно событие 10 -> 20,
    атрибутированное сессии целиком; со снимком — простой отдельно."""
    conn = store.connect(tmp_path / "u.sqlite")
    _rl_mod(conn, [(1000, "a", 10.0), (5000, "a", 20.0)])
    _us(conn, [(3000, 17.0, 17.0)])
    _req_at(conn, [(990, "a", 1_000_000), (4990, "a", 1_000_000)])
    ev = analyze.limit_events(conn)
    assert [(e["t0"], e["t1"], e["d_pct"]) for e in ev] == [
        (1000, 3000, 7.0), (3000, 5000, 3.0)]
    assert ev[0]["kind"] == "elsewhere"
    assert ev[1]["kind"] == "attributed"
    assert [s["session_id"] for s in ev[1]["shares"]] == ["a"]
    assert ev[1]["shares"][0]["pct"] == pytest.approx(3.0)


def test_snapshot_never_appears_as_session(tmp_path):
    conn = store.connect(tmp_path / "u.sqlite")
    _us(conn, [(1000, 10.0, 10.0), (3000, 14.0, 14.0)])
    _rl_mod(conn, [(2000, "a", 12.0)])
    _req_at(conn, [(1500, "a", 1_000_000), (2500, "b", 1_000_000)])
    share = analyze.session_pct_share(conn, None, None)
    assert set(share) == {"a", "b"}
    assert sum(share.values()) == pytest.approx(4.0)


def test_snapshot_and_mark_with_same_ts_sort_without_error(tmp_path):
    """session_id снимка — None; сортировка по (ts, session_id) с None и
    строкой в одном ts не должна падать."""
    conn = store.connect(tmp_path / "u.sqlite")
    _us(conn, [(1000, 10.0, 10.0), (2000, 12.0, 12.0)])
    _rl_mod(conn, [(2000, "a", 12.0)])
    ev = analyze.limit_events(conn)
    assert [(e["t0"], e["d_pct"]) for e in ev] == [(1000, 2.0)]


def test_snapshots_respect_window(tmp_path):
    conn = store.connect(tmp_path / "u.sqlite")
    _us(conn, [(1000, 10.0, 10.0), (2000, 12.0, 12.0), (3000, 15.0, 15.0),
               (4000, 19.0, 19.0)])
    ev = analyze.limit_events(conn, since=2500, until=3500)
    # якорь 2000 (последний перед since) + 3000 внутри; 4000 за until
    assert [(e["t0"], e["t1"], e["d_pct"]) for e in ev] == [(2000, 3000, 3.0)]


def test_anchor_is_latest_mark_across_both_sources(tmp_path):
    """Якорь перед since — последний свежий замер из ЛЮБОГО источника.
    Два якоря дали бы пару целиком до since — событие вне окна."""
    conn = store.connect(tmp_path / "u.sqlite")
    _rl_mod(conn, [(1000, "a", 10.0), (5000, "a", 20.0)])
    _us(conn, [(2000, 15.0, 15.0)])
    ev = analyze.limit_events(conn, since=3000)
    assert [(e["t0"], e["t1"], e["d_pct"]) for e in ev] == [(2000, 5000, 5.0)]
    _us(conn, [(500, 8.0, 8.0)])
    ev = analyze.limit_events(conn, since=1500)
    assert [(e["t0"], e["t1"]) for e in ev] == [(1000, 2000), (2000, 5000)]


def test_mixed_precision_sources_produce_no_sawtooth(tmp_path):
    """Мод может отдавать десятые (43.4), API — целые (43). Чередование
    43.4 -> 43 -> 43.4 не должно читаться как повторный рост на 0.4."""
    conn = store.connect(tmp_path / "u.sqlite")
    _rl_mod(conn, [(1000, "a", 43.4), (3000, "a", 43.4), (5000, "a", 43.4)])
    _us(conn, [(2000, 43.0, 43.0), (4000, 43.0, 43.0)])
    assert analyze.limit_events(conn) == []


def test_mixed_precision_rounded_api_produces_no_sawtooth(tmp_path):
    """Обратный случай: API округлил вверх (44), мод 43.6."""
    conn = store.connect(tmp_path / "u.sqlite")
    _us(conn, [(1000, 44.0, 44.0), (3000, 44.0, 44.0), (5000, 44.0, 44.0)])
    _rl_mod(conn, [(2000, "a", 43.6), (4000, "a", 43.6)])
    assert analyze.limit_events(conn) == []


def test_mixed_precision_keeps_real_growth(tmp_path):
    """Подавление пилы не съедает настоящий рост через смену источника."""
    conn = store.connect(tmp_path / "u.sqlite")
    _rl_mod(conn, [(1000, "a", 43.4), (3000, "a", 45.2)])
    _us(conn, [(2000, 43.0, 43.0), (4000, 46.0, 46.0)])
    ev = analyze.limit_events(conn)
    assert sum(e["d_pct"] for e in ev) == pytest.approx(46.0 - 43.4)


def test_stale_latest_anchor_does_not_fall_back_to_old_snapshot(tmp_path):
    """Последний замер до since — эхо (не свежий). Старый снимок за ним
    якорем не становится: пара через месяц шла бы через сбросы окна."""
    conn = store.connect(tmp_path / "u.sqlite")
    _us(conn, [(100, 10.0, 10.0)])
    _rl_fresh(conn, [(2000, "a", 3.0, 0), (5000, "a", 13.0, 1)])
    assert analyze.limit_events(conn, since=3000) == []
