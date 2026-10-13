# cc_usage/tests/test_export.py
import gzip
import json
import time

import export
import store

SECRETS = ["secretproject", "C:/secret/path", "secret-branch"]


def _seed(conn):
    store.insert_requests(conn, [dict(
        request_id="r1", message_id="m1", session_id="s1", ts=1755500000,
        model="claude-opus-5", effort="high", service_tier="standard",
        input=1, output=5, thinking=2, cache_read=900, cache_write=1000,
        cache_1h=1000, cache_5m=0, is_sidechain=0, agent_file=None,
        project="secretproject", cwd="C:/secret/path",
        git_branch="secret-branch", cc_version="2.1")])
    store.rebuild_sessions(conn)


def test_export_anonymizes_by_default(tmp_path):
    conn = store.connect(tmp_path / "u.sqlite")
    _seed(conn)
    out = export.export_bundle(conn, tmp_path / "b.json.gz")
    raw = gzip.open(out, "rt", encoding="utf-8").read()
    for secret in SECRETS:
        assert secret not in raw          # проверка на утечку, не на формат
    data = json.loads(raw)
    assert data["requests"][0]["cache_write"] == 1000   # метрики на месте


def test_export_can_keep_names_explicitly(tmp_path):
    conn = store.connect(tmp_path / "u.sqlite")
    _seed(conn)
    out = export.export_bundle(conn, tmp_path / "b2.json.gz", anonymize=False)
    raw = gzip.open(out, "rt", encoding="utf-8").read()
    assert "secretproject" in raw


def test_same_project_hashes_consistently(tmp_path):
    a = export.anonymize_value("proj", "salt")
    b = export.anonymize_value("proj", "salt")
    c = export.anonymize_value("other", "salt")
    assert a == b and a != c and len(a) == 12


def test_roundtrip_import(tmp_path):
    src = store.connect(tmp_path / "a.sqlite")
    _seed(src)
    out = export.export_bundle(src, tmp_path / "b.json.gz")
    dst = store.connect(tmp_path / "b.sqlite")
    assert export.import_bundle(dst, out) == 1
    assert dst.execute("select sum(cache_write) from requests").fetchone()[0] == 1000
    # повторный импорт ничего не дублирует
    assert export.import_bundle(dst, out) == 0


def test_roundtrip_plain_json_suffix(tmp_path):
    src = store.connect(tmp_path / "a.sqlite")
    _seed(src)
    out = export.export_bundle(src, tmp_path / "b.json")   # суффикс .json
    dst = store.connect(tmp_path / "b2.sqlite")
    assert export.import_bundle(dst, out) == 1
    assert dst.execute("select sum(cache_write) from requests").fetchone()[0] == 1000


def _seed_with_title(conn):
    _seed(conn)
    store.set_session_title(conn, "s1", "Секретный промпт клиента", "ai-title", 1755500000.0)


def test_export_includes_session_meta(tmp_path):
    conn = store.connect(tmp_path / "u.sqlite")
    _seed_with_title(conn)
    out = export.export_bundle(conn, tmp_path / "b.json.gz", anonymize=False)
    data = json.loads(gzip.open(out, "rt", encoding="utf-8").read())
    assert data["version"] == 2
    assert data["session_meta"][0]["title"] == "Секретный промпт клиента"


def test_export_anonymizes_title_by_default(tmp_path):
    conn = store.connect(tmp_path / "u.sqlite")
    _seed_with_title(conn)
    out = export.export_bundle(conn, tmp_path / "b.json.gz")
    raw = gzip.open(out, "rt", encoding="utf-8").read()
    assert "Секретный промпт клиента" not in raw
    data = json.loads(raw)
    assert data["session_meta"][0]["title"] == "«сессия 1»" or data["session_meta"][0]["title"].startswith("«сессия")


def test_import_old_bundle_version_without_session_meta_does_not_fail(tmp_path):
    old_bundle = {
        "version": 1, "exported_at": 1755500000.0, "anonymized": False,
        "requests": [dict(
            request_id="r1", message_id="m1", session_id="s1", ts=1755500000,
            model="claude-opus-5", effort=None, service_tier=None,
            input=1, output=5, thinking=0, cache_read=900, cache_write=1000,
            cache_1h=1000, cache_5m=0, is_sidechain=0, agent_file=None,
            project="p", cwd=None, git_branch=None, cc_version="2.1")],
        "ratelimits": [],
    }
    path = tmp_path / "old.json.gz"
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        json.dump(old_bundle, fh)
    conn = store.connect(tmp_path / "u.sqlite")
    n = export.import_bundle(conn, path)
    assert n == 1
    assert store.get_session_title(conn, "s1") is None    # имён не было — и не появилось


def test_roundtrip_preserves_title(tmp_path):
    src = store.connect(tmp_path / "a.sqlite")
    _seed_with_title(src)
    out = export.export_bundle(src, tmp_path / "b.json.gz", anonymize=False)
    dst = store.connect(tmp_path / "b.sqlite")
    export.import_bundle(dst, out)
    assert store.get_session_title(dst, "s1")["title"] == "Секретный промпт клиента"


def test_export_days_window_excludes_out_of_window_session_meta(tmp_path):
    # Finding 5 (финальное ревью): session_meta (заголовок сессии — по сути
    # пользовательский промпт) экспортировался целиком, игнорируя --days —
    # утечка чужого окна при --no-anonymize.
    conn = store.connect(tmp_path / "u.sqlite")
    now = time.time()

    def _row(rid, sid, ts):
        return dict(request_id=rid, message_id=rid, session_id=sid, ts=ts,
                    model="claude-opus-5", effort=None, service_tier=None,
                    input=1, output=5, thinking=0, cache_read=0, cache_write=0,
                    cache_1h=0, cache_5m=0, is_sidechain=0, agent_file=None,
                    project="p", cwd=None, git_branch=None, cc_version=None)

    store.insert_requests(conn, [_row("in1", "s-in", now - 3600)])          # внутри окна
    store.insert_requests(conn, [_row("out1", "s-out", now - 100 * 86400)])  # далеко за окном
    store.rebuild_sessions(conn)
    store.set_session_title(conn, "s-in", "Заголовок внутри окна", "ai-title", now)
    store.set_session_title(conn, "s-out", "Секретный заголовок за окном", "ai-title", now)

    out = export.export_bundle(conn, tmp_path / "b.json.gz", days=7, anonymize=False)
    raw = gzip.open(out, "rt", encoding="utf-8").read()
    assert "Секретный заголовок за окном" not in raw
    data = json.loads(raw)
    titles_by_session = {m["session_id"]: m["title"] for m in data["session_meta"]}
    assert titles_by_session.get("s-in") == "Заголовок внутри окна"
    assert "s-out" not in titles_by_session


def test_import_ignores_session_meta_entry_with_null_session_id(tmp_path):
    bundle = {
        "version": 2, "exported_at": 1755500000.0, "anonymized": False,
        "requests": [dict(
            request_id="r1", message_id="m1", session_id="s1", ts=1755500000,
            model="claude-opus-5", effort=None, service_tier=None,
            input=1, output=5, thinking=0, cache_read=900, cache_write=1000,
            cache_1h=1000, cache_5m=0, is_sidechain=0, agent_file=None,
            project="p", cwd=None, git_branch=None, cc_version="2.1")],
        "ratelimits": [],
        "session_meta": [
            dict(session_id="s1", title="Нормальный заголовок",
                 title_source="ai-title", updated_at=1755500000.0),
            dict(session_id=None, title="Подброшенный", title_source="ai-title",
                 updated_at=1755500000.0),
        ],
    }
    path = tmp_path / "malicious.json.gz"
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        json.dump(bundle, fh)
    conn = store.connect(tmp_path / "u.sqlite")
    n = export.import_bundle(conn, path)
    assert n == 1
    assert store.get_session_title(conn, "s1")["title"] == "Нормальный заголовок"
    assert conn.execute(
        "SELECT COUNT(*) FROM session_meta WHERE session_id IS NULL"
    ).fetchone()[0] == 0
