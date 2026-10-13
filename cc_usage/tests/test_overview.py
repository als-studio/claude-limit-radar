import analyze
import store


def _insert(conn, rows):
    store.insert_requests(conn, [
        dict(request_id="r%d" % i, message_id="m%d" % i, session_id=r.get("s", "s1"),
             ts=r["ts"], model=r.get("model", "claude-opus-5"), effort=None,
             service_tier=None, input=0, output=r.get("out", 0), thinking=0,
             cache_read=r.get("cr", 0), cache_write=r.get("cw", 0),
             cache_1h=0, cache_5m=0,
             is_sidechain=r.get("sub", 0), agent_file=None,
             project="p", cwd=None, git_branch=None, cc_version=None)
        for i, r in enumerate(rows)])


def test_ramp_measures_entry_cost_and_window(tmp_path):
    conn = store.connect(tmp_path / "u.sqlite")
    _insert(conn, [
        {"ts": 1000, "cw": 50000},              # вход
        {"ts": 1060, "cw": 1000},
        {"ts": 1120, "cw": 2000, "sub": 1},     # субагент внутри окна
        {"ts": 9000, "cw": 999},                # вне окна
    ])
    r = analyze.session_ramp(conn, "s1", window_s=300)
    assert r["entry_cost"] == 50000
    assert r["window_total"] == 53000
    assert r["n_requests"] == 3
    assert r["n_subagents"] == 1
    assert r["subagent_tokens"] == 2000
    assert len(r["per_minute"]) == 5


def test_cache_efficiency(tmp_path):
    conn = store.connect(tmp_path / "u.sqlite")
    _insert(conn, [{"ts": 1, "cr": 900, "cw": 100}])
    e = analyze.cache_efficiency(conn)
    assert e["hit_ratio"] == 0.9


def test_overview_has_all_sections(tmp_path):
    conn = store.connect(tmp_path / "u.sqlite")
    _insert(conn, [{"ts": 1000, "cw": 100, "cr": 900, "out": 10}])
    store.rebuild_sessions(conn)
    o = analyze.overview(conn)
    for key in ("period", "totals", "efficiency", "cold_cache",
                "by_day", "by_model", "by_project", "sessions",
                "cost", "cost_by_model", "cost_by_project", "spend_by_bucket",
                "diagnoses"):
        assert key in o
    assert o["totals"]["cache_write"] == 100


def test_overview_n_sessions_respects_window(tmp_path):
    # Task 10 review, Fix 2: карточка "Сессий" раньше читала len(o["sessions"]) —
    # это all-time список с LIMIT 100, несогласованный с окном "Расход" рядом.
    conn = store.connect(tmp_path / "u.sqlite")
    _insert(conn, [
        {"ts": 1000, "cw": 100, "s": "s1"},
        {"ts": 9000, "cw": 100, "s": "s2"},
    ])
    store.rebuild_sessions(conn)
    assert analyze.overview(conn)["totals"]["n_sessions"] == 2
    assert analyze.overview(conn, since=5000)["totals"]["n_sessions"] == 1


def test_overview_on_empty_db_does_not_crash(tmp_path):
    conn = store.connect(tmp_path / "u.sqlite")
    o = analyze.overview(conn)
    assert o["totals"]["cache_write"] == 0
    assert o["sessions"] == []


def test_sessions_by_cost_includes_avg_context_tokens(tmp_path):
    # Finding 3 (финальное ревью): "Самые дорогие сессии" должны нести
    # средний размер контекста на запрос для КАЖДОЙ сессии, не только для
    # тех, что попали в where диагноза Д3.
    conn = store.connect(tmp_path / "u.sqlite")
    _insert(conn, [
        {"ts": 1000, "cw": 100, "cr": 900, "s": "s1"},
        {"ts": 1010, "cw": 100, "cr": 300, "s": "s1"},
    ])
    store.rebuild_sessions(conn)
    o = analyze.overview(conn)
    s1 = next(s for s in o["sessions_by_cost"] if s["session_id"] == "s1")
    assert s1["avg_context_tokens"] == 600   # (900 + 300) / 2
