import store


def _req(**kw):
    row = dict(
        request_id="req_1", message_id="msg_1", session_id="s1", ts=1000.0,
        model="claude-opus-5", effort="high", service_tier="standard",
        input=2, output=100, thinking=50,
        cache_read=1000, cache_write=200, cache_1h=200, cache_5m=0,
        is_sidechain=0, agent_file=None,
        project="demo", cwd="C:/Projects/demo", git_branch="main",
        cc_version="2.1.234",
    )
    row.update(kw)
    return row


def test_insert_and_dedup(tmp_path):
    conn = store.connect(tmp_path / "u.sqlite")
    assert store.insert_requests(conn, [_req()]) == 1
    # тот же (request_id, message_id) — вставки не происходит
    assert store.insert_requests(conn, [_req()]) == 0
    # другой message_id при том же request_id — это другая строка
    assert store.insert_requests(conn, [_req(message_id="msg_2")]) == 1
    assert conn.execute("select count(*) from requests").fetchone()[0] == 2


def test_file_state_roundtrip(tmp_path):
    conn = store.connect(tmp_path / "u.sqlite")
    assert store.get_file_state(conn, "a.jsonl") is None
    store.set_file_state(conn, "a.jsonl", size=100, mtime=5.0, offset=90)
    st = store.get_file_state(conn, "a.jsonl")
    assert st["size"] == 100 and st["offset"] == 90


def test_ratelimits_dedup(tmp_path):
    conn = store.connect(tmp_path / "u.sqlite")
    row = dict(ts=1.0, session_id="s1", five_hour_pct=10.0, five_hour_reset=99,
               seven_day_pct=20.0, seven_day_reset=98, ctx_pct=5.0,
               ctx_in=1000, cost_usd=0.5)
    assert store.insert_ratelimits(conn, [row]) == 1
    assert store.insert_ratelimits(conn, [row]) == 0


def test_schema_version_is_3(tmp_path):
    conn = store.connect(tmp_path / "u.sqlite")
    v = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0]
    assert int(v) == 3


def test_session_meta_table_exists(tmp_path):
    conn = store.connect(tmp_path / "u.sqlite")
    conn.execute("SELECT session_id, title, title_source, updated_at FROM session_meta")


def test_set_and_get_session_title(tmp_path):
    conn = store.connect(tmp_path / "u.sqlite")
    store.set_session_title(conn, "s1", "Моя сессия", "last-prompt", 1000.0)
    got = store.get_session_title(conn, "s1")
    assert got["title"] == "Моя сессия"
    assert got["title_source"] == "last-prompt"


def test_ai_title_not_overwritten_by_last_prompt(tmp_path):
    conn = store.connect(tmp_path / "u.sqlite")
    store.set_session_title(conn, "s1", "Заголовок от ai-title", "ai-title", 1000.0)
    store.set_session_title(conn, "s1", "Обрывок промпта", "last-prompt", 2000.0)
    got = store.get_session_title(conn, "s1")
    assert got["title"] == "Заголовок от ai-title"
    assert got["title_source"] == "ai-title"


def test_ai_title_overwrites_last_prompt(tmp_path):
    conn = store.connect(tmp_path / "u.sqlite")
    store.set_session_title(conn, "s1", "Обрывок промпта", "last-prompt", 1000.0)
    store.set_session_title(conn, "s1", "Заголовок от ai-title", "ai-title", 2000.0)
    got = store.get_session_title(conn, "s1")
    assert got["title"] == "Заголовок от ai-title"


def test_get_session_title_missing_returns_none(tmp_path):
    conn = store.connect(tmp_path / "u.sqlite")
    assert store.get_session_title(conn, "nope") is None


def test_existing_db_migrates_schema_version_1_to_current(tmp_path):
    """Старая база с версией 1 в meta открывается, получает session_meta и
    sessions.cwd (ALTER TABLE на уже существующей боевой таблице, см.
    _ensure_column — CREATE TABLE IF NOT EXISTS колонку не добавляет).
    """
    db_path = tmp_path / "old.sqlite"
    conn = store.connect(db_path)
    conn.execute("UPDATE meta SET value = '1' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()
    conn2 = store.connect(db_path)     # повторное открытие = повторная migrate()
    v = conn2.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0]
    assert int(v) == store.SCHEMA_VERSION
    conn2.execute("SELECT 1 FROM session_meta")   # не падает
    conn2.execute("SELECT cwd FROM sessions")      # не падает


def test_migrate_adds_fresh_to_existing_ratelimits(tmp_path):
    """Колонка добавляется в уже существующую таблицу без потери строк."""
    import sqlite3
    db = tmp_path / "u.sqlite"
    raw = sqlite3.connect(str(db))
    raw.execute("CREATE TABLE ratelimits (ts REAL, session_id TEXT,"
                " five_hour_pct REAL, five_hour_reset INTEGER,"
                " seven_day_pct REAL, seven_day_reset INTEGER,"
                " ctx_pct REAL, ctx_in INTEGER, cost_usd REAL,"
                " PRIMARY KEY (session_id, ts))")
    raw.execute("INSERT INTO ratelimits (ts, session_id, five_hour_pct)"
                " VALUES (1000, 's1', 10.0)")
    raw.commit()
    raw.close()

    conn = store.connect(db)
    row = conn.execute("SELECT five_hour_pct, fresh FROM ratelimits").fetchone()
    assert row["five_hour_pct"] == 10.0
    assert row["fresh"] is None


# --- сигнал активности для зонда лимита -----------------------------------
#
# Зонд живёт в собственном каталоге (`<config>/cc-usage/probe-cwd`) именно
# затем, чтобы его собственные ping-и можно было отличить от работы человека.
# Без этого гейт «слать ping только при новой активности» открыт всегда:
# зонд видит свой же предыдущий запрос и запускает себя снова.

PROBE_CWD = "/home/u/.claude/cc-usage/probe-cwd"


def test_last_request_ts_ignores_probe_own_requests(tmp_path):
    conn = store.connect(tmp_path / "u.sqlite")
    store.insert_requests(conn, [
        _req(request_id="a", cwd="/home/u/work", ts=100.0),
        _req(request_id="b", cwd=PROBE_CWD, ts=200.0),
    ])
    assert store.last_request_ts(conn, exclude_cwd=PROBE_CWD) == 100.0


def test_last_request_ts_counts_everything_without_exclusion(tmp_path):
    conn = store.connect(tmp_path / "u.sqlite")
    store.insert_requests(conn, [
        _req(request_id="a", cwd="/home/u/work", ts=100.0),
        _req(request_id="b", cwd=PROBE_CWD, ts=200.0),
    ])
    assert store.last_request_ts(conn) == 200.0


def test_last_request_ts_keeps_rows_without_cwd(tmp_path):
    """cwd бывает пустым в старых записях — такие строки не наши, их нельзя
    молча выкидывать из сигнала активности."""
    conn = store.connect(tmp_path / "u.sqlite")
    store.insert_requests(conn, [_req(request_id="a", cwd=None, ts=300.0)])
    assert store.last_request_ts(conn, exclude_cwd=PROBE_CWD) == 300.0


def test_last_request_ts_is_none_on_empty_db(tmp_path):
    conn = store.connect(tmp_path / "u.sqlite")
    assert store.last_request_ts(conn, exclude_cwd=PROBE_CWD) is None


# --- usage_snapshots: замер лимита через /api/oauth/usage ------------------
#
# Отдельная таблица, а не расширение ratelimits — источники разной природы
# (statusline-хук vs серверный эндпоинт) должны оставаться сравнимыми.

def _snapshot(**kw):
    row = dict(
        ts=1000.0, five_hour_pct=1.0, five_hour_reset=1788936599,
        seven_day_pct=9.0, seven_day_reset=1788933599,
        scoped_pct=12.0, scoped_model="Fable", scoped_reset=1788933599,
        extra_enabled=0, extra_pct=0.0, severity="normal", raw="{}",
    )
    row.update(kw)
    return row


def test_usage_snapshots_insert_and_dedup(tmp_path):
    conn = store.connect(tmp_path / "u.sqlite")
    assert store.insert_usage_snapshots(conn, [_snapshot()]) == 1
    # тот же ts (PRIMARY KEY) — повторная вставка не проходит
    assert store.insert_usage_snapshots(conn, [_snapshot()]) == 0
    assert store.insert_usage_snapshots(conn, [_snapshot(ts=2000.0)]) == 1
    assert conn.execute("select count(*) from usage_snapshots").fetchone()[0] == 2


def test_usage_snapshots_missing_fields_are_null(tmp_path):
    conn = store.connect(tmp_path / "u.sqlite")
    store.insert_usage_snapshots(conn, [dict(ts=1000.0, raw="{}")])
    row = conn.execute("SELECT * FROM usage_snapshots WHERE ts = 1000.0").fetchone()
    assert row["five_hour_pct"] is None
    assert row["scoped_model"] is None


def test_meta_value_survives_roundtrip(tmp_path):
    conn = store.connect(tmp_path / "u.sqlite")
    assert store.get_meta(conn, "last_probe_ts") is None
    store.set_meta(conn, "last_probe_ts", "1788230000.5")
    assert store.get_meta(conn, "last_probe_ts") == "1788230000.5"
    store.set_meta(conn, "last_probe_ts", "1788240000.0")
    assert store.get_meta(conn, "last_probe_ts") == "1788240000.0"
