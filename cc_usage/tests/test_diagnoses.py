import analyze
import pricing
import store


SONNET = "claude-sonnet-5"
OPUS = "claude-opus-5"


def _req(session_id="s1", ts=1000, model=SONNET, **kw):
    base = dict(request_id="r-%s-%s" % (session_id, ts), message_id="m-%s-%s" % (session_id, ts),
                session_id=session_id, ts=ts, model=model, effort=None, service_tier=None,
                input=0, output=0, thinking=0, cache_read=0, cache_write=0,
                cache_1h=0, cache_5m=0, is_sidechain=0, agent_file=None,
                project="p", cwd=None, git_branch=None, cc_version=None)
    base.update(kw)
    return base


def test_cost_by_model_groups_by_exact_model_and_flags_unpriced(tmp_path):
    conn = store.connect(tmp_path / "u.sqlite")
    store.insert_requests(conn, [
        _req(ts=100, model=SONNET, input=1_000_000),
        _req(ts=200, model=OPUS, output=1_000_000),
        _req(ts=300, model="k3", output=1_000_000),
    ])
    by_model = analyze.cost_by_model(conn)
    by_name = {r["model"]: r for r in by_model}
    assert round(by_name[SONNET]["cost"], 6) == 2.0
    assert round(by_name[OPUS]["cost"], 6) == 25.0
    assert by_name["k3"]["cost"] is None
    assert by_name[SONNET]["n"] == 1


def test_cost_by_model_respects_since_until(tmp_path):
    conn = store.connect(tmp_path / "u.sqlite")
    store.insert_requests(conn, [
        _req(ts=100, model=SONNET, input=1_000_000),
        _req(ts=9000, model=SONNET, input=1_000_000),
    ])
    by_model = analyze.cost_by_model(conn, since=5000)
    assert by_model[0]["n"] == 1


def test_cost_by_project_groups_by_project_in_dollars(tmp_path):
    # Task 10 review, Fix 3: таблица по проектам на главном экране раньше
    # показывала сырые токены (fmt.tokens(p["cw"])) — единственная выжившая
    # строка в токенах на денежном экране.
    conn = store.connect(tmp_path / "u.sqlite")
    store.insert_requests(conn, [
        _req(ts=100, model=SONNET, input=1_000_000, project="alpha"),
        _req(ts=200, model=OPUS, output=1_000_000, project="beta"),
        _req(ts=300, model="k3", output=1_000_000, project="alpha"),  # непрайсовано — пропускается
    ])
    by_project = analyze.cost_by_project(conn)
    by_name = {r["k"]: r for r in by_project}
    assert round(by_name["alpha"]["cost"], 6) == 2.0
    assert round(by_name["beta"]["cost"], 6) == 25.0


def test_spend_by_bucket_covers_empty_buckets(tmp_path):
    conn = store.connect(tmp_path / "u.sqlite")
    store.insert_requests(conn, [_req(ts=1000, model=SONNET, output=1_000_000)])
    buckets = analyze.spend_by_bucket(conn, since=0, until=3 * 3600)
    assert len(buckets) > 1
    assert any(b["cost"] > 0 for b in buckets)
    assert any(b["cost"] == 0 for b in buckets)


def test_session_display_name_prefers_ai_title():
    name, source = analyze.session_display_name(
        {"session_id": "abcd1234", "project": "p", "first_ts": 1000},
        {"title": "Моё имя", "title_source": "ai-title"})
    assert name == "Моё имя"
    assert source == "ai-title"


def test_session_display_name_falls_back_to_project_and_time():
    name, source = analyze.session_display_name(
        {"session_id": "abcd1234", "project": "myproj", "first_ts": 1755500000}, None)
    assert "myproj" in name
    assert source == "project"


def test_session_display_name_falls_back_to_id():
    name, source = analyze.session_display_name(
        {"session_id": "abcd1234", "project": None, "first_ts": None}, None)
    assert name == "abcd1234"
    assert source == "id"


def test_session_display_name_prefers_cwd_leaf_over_mangled_project():
    # project — мангленное Claude Code имя каталога ("C--Projects-demo--
    # claude-worktrees-feat-DEMO-41-agent-backend"), нечитаемо в отчёте.
    # cwd хранит путь как есть — последний сегмент читаем.
    name, source = analyze.session_display_name(
        {"session_id": "abcd1234", "project": "C--Projects-demo--claude-worktrees-feat-DEMO-41",
         "cwd": "C:\\Projects\\demo\\claude-worktrees\\feat\\DEMO-41", "first_ts": 1755500000},
        None)
    assert "DEMO-41" in name
    assert "C--Projects" not in name
    assert source == "project"


def test_session_display_name_falls_back_to_project_when_cwd_is_bare_drive():
    # cwd вида "C:" без пути — не путь, использовать project как раньше.
    name, source = analyze.session_display_name(
        {"session_id": "abcd1234", "project": "myproj", "cwd": "C:", "first_ts": 1755500000}, None)
    assert "myproj" in name


def test_resolve_session_by_exact_id(tmp_path):
    conn = store.connect(tmp_path / "u.sqlite")
    store.insert_requests(conn, [_req(session_id="sess-full-id")])
    store.rebuild_sessions(conn)
    assert analyze.resolve_session(conn, "sess-full-id") == "sess-full-id"


def test_resolve_session_by_id_prefix(tmp_path):
    conn = store.connect(tmp_path / "u.sqlite")
    store.insert_requests(conn, [_req(session_id="sess-full-id")])
    store.rebuild_sessions(conn)
    assert analyze.resolve_session(conn, "sess-full") == "sess-full-id"


def test_resolve_session_by_name_substring(tmp_path):
    conn = store.connect(tmp_path / "u.sqlite")
    store.insert_requests(conn, [_req(session_id="s1")])
    store.rebuild_sessions(conn)
    store.set_session_title(conn, "s1", "Статистика по тегам", "ai-title", 1000.0)
    assert analyze.resolve_session(conn, "тегам") == "s1"


def test_resolve_session_not_found_raises(tmp_path):
    import pytest
    conn = store.connect(tmp_path / "u.sqlite")
    with pytest.raises(ValueError):
        analyze.resolve_session(conn, "нет такой сессии")


def test_diagnose_cold_cache_reports_fact_and_avoidable(tmp_path):
    conn = store.connect(tmp_path / "u.sqlite")
    rows = [_req(ts=i * 10, model=SONNET, cache_write=1000, cache_1h=1000) for i in range(8)]
    rows.append(_req(ts=80 + 4000, model=SONNET, cache_write=900000, cache_1h=900000))
    store.insert_requests(conn, rows)
    store.rebuild_sessions(conn)
    diags = analyze.diagnoses(conn)
    cold = next((d for d in diags if d["id"] == "cold_cache"), None)
    assert cold is not None
    assert cold["fact_usd"] > 0
    assert cold["avoidable_usd"] is not None
    assert cold["assumption"]
    assert cold["where"][0]["session_id"] == "s1"


def test_diagnose_expensive_model_compares_to_sonnet_price(tmp_path):
    conn = store.connect(tmp_path / "u.sqlite")
    rows = ([_req(session_id="opus-session", ts=i, model=OPUS, output=1_000_000)
            for i in range(20)])
    store.insert_requests(conn, rows)
    store.rebuild_sessions(conn)
    diags = analyze.diagnoses(conn)
    exp = next((d for d in diags if d["id"] == "expensive_model"), None)
    assert exp is not None
    assert exp["fact_usd"] == 20 * 25.0
    # оценка устранимого: разница с ценой Sonnet (25 - 10 = 15 за MTok output)
    assert round(exp["avoidable_usd"], 2) == round(20 * (25.0 - 10.0), 2)


def test_diagnose_context_cost_has_no_avoidable_it_is_not_a_loss(tmp_path):
    conn = store.connect(tmp_path / "u.sqlite")
    rows = [_req(session_id="ctx", ts=i, model=SONNET, cache_read=10_000_000)
            for i in range(20)]
    store.insert_requests(conn, rows)
    store.rebuild_sessions(conn)
    diags = analyze.diagnoses(conn)
    ctx = next((d for d in diags if d["id"] == "context_cost"), None)
    assert ctx is not None
    assert ctx["avoidable_usd"] is None    # не потеря — плата за размер контекста


def test_diagnose_subagents_reports_sidechain_cost(tmp_path):
    conn = store.connect(tmp_path / "u.sqlite")
    rows = [_req(session_id="sub", ts=i, model=SONNET, output=1_000_000, is_sidechain=1)
            for i in range(20)]
    store.insert_requests(conn, rows)
    store.rebuild_sessions(conn)
    diags = analyze.diagnoses(conn)
    sub = next((d for d in diags if d["id"] == "subagents"), None)
    assert sub is not None
    assert sub["fact_usd"] == 20 * 10.0


def test_diagnosis_below_1_percent_is_omitted(tmp_path):
    conn = store.connect(tmp_path / "u.sqlite")
    rows = [_req(session_id="big", ts=i, model=SONNET, output=100_000_000) for i in range(5)]
    rows += [_req(session_id="tiny-opus", ts=1000 + i, model=OPUS, output=1)
            for i in range(3)]
    store.insert_requests(conn, rows)
    store.rebuild_sessions(conn)
    diags = analyze.diagnoses(conn)
    ids = {d["id"] for d in diags}
    assert "expensive_model" not in ids     # $75e-6 от полумиллиарда — не показываем
    insufficient_ids = {i["id"] for i in analyze.diagnoses_insufficient_ids(conn)}
    assert "expensive_model" in insufficient_ids


def test_diagnoses_on_empty_db_returns_empty_list(tmp_path):
    conn = store.connect(tmp_path / "u.sqlite")
    assert analyze.diagnoses(conn) == []


# --- регрессии по ревью Task 8 (Fix 1-3) --------------------------------------

def test_overview_spend_by_bucket_count_matches_bucket_label_format(tmp_path):
    """spend_by_bucket и bucket_label_format должны считаться от одного и
    того же окна (Fix 1) — иначе окно 1ч даёт ~13 корзин по 300с, а не
    десятки тысяч корзин от epoch 0."""
    conn = store.connect(tmp_path / "u.sqlite")
    store.insert_requests(conn, [_req(ts=1000, model=SONNET, output=1_000_000)])
    store.rebuild_sessions(conn)
    o = analyze.overview(conn, since=1000, until=1000 + 3600)
    assert o["bucket_label_format"] == "time"     # шаг 300с при окне <= 6ч
    assert len(o["spend_by_bucket"]) == 13         # 3600 // 300 + 1
    assert any(b["cost"] > 0 for b in o["spend_by_bucket"])


def test_sessions_by_cost_respects_window(tmp_path):
    """Сессия с активностью только до начала окна не должна попадать в
    sessions_by_cost (Fix 2) — иначе "самые дорогие сессии" показывали
    all-time доллары рядом с windowed итогом."""
    conn = store.connect(tmp_path / "u.sqlite")
    store.insert_requests(conn, [
        _req(session_id="old", ts=100, model=SONNET, output=1_000_000),
        _req(session_id="new", ts=5000, model=SONNET, output=1_000_000),
    ])
    store.rebuild_sessions(conn)
    o = analyze.overview(conn, since=4000, until=6000)
    ids = {s["session_id"] for s in o["sessions_by_cost"]}
    assert "new" in ids
    assert "old" not in ids


def test_diagnose_expensive_model_excludes_cheaper_than_sonnet_models(tmp_path):
    """Haiku дешевле Sonnet-5 по каждой компоненте цены — Д2 не должен
    засчитывать его как "дорогую модель" (Fix 3): ни по имени (Haiku не
    равен CHEAPEST_CURRENT_MODEL по строке), ни по факту (его реальная
    цена ниже контрфактической цены Sonnet, delta <= 0)."""
    conn = store.connect(tmp_path / "u.sqlite")
    HAIKU = "claude-haiku-4-5"
    rows = [_req(session_id="opus-session", ts=i, model=OPUS, output=1_000_000)
            for i in range(20)]
    rows += [_req(session_id="haiku-session", ts=1000 + i, model=HAIKU, output=1_000_000)
            for i in range(20)]
    store.insert_requests(conn, rows)
    store.rebuild_sessions(conn)
    diags = analyze.diagnoses(conn)
    exp = next((d for d in diags if d["id"] == "expensive_model"), None)
    assert exp is not None
    assert exp["fact_usd"] == 20 * 25.0     # только Opus; Haiku не в fact
    sids = {w["session_id"] for w in exp["where"]}
    assert "haiku-session" not in sids
    assert "opus-session" in sids


def test_cache_write_sum_unaffected_by_pricing_refactor(tmp_path):
    """Ключевой инвариант: рефакторинг отчёта не должен менять сумму cache_write.

    Это защита от порчи дедупликации — если тест начал падать, ищи причину в
    store.py/ingest.py, а не в pricing.py/analyze.py (стоимость считается
    поверх cache_write, не подменяет его)."""
    conn = store.connect(tmp_path / "u.sqlite")
    rows = [_req(ts=i, model=SONNET, cache_write=1000 + i) for i in range(50)]
    store.insert_requests(conn, rows)
    store.insert_requests(conn, rows)     # повторная вставка тех же request_id/message_id
    store.rebuild_sessions(conn)
    total = conn.execute("SELECT SUM(cache_write) FROM requests").fetchone()[0]
    expected = sum(1000 + i for i in range(50))
    assert total == expected      # INSERT OR IGNORE не задублировал
