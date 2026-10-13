import json

import ingest
import store


def _write_jsonl(path, lines):
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")


def test_backfill_finds_titles_without_touching_requests(tmp_path):
    projects = tmp_path / "projects" / "proj1"
    projects.mkdir(parents=True)
    f = projects / "sess-1.jsonl"
    _write_jsonl(f, [
        {"type": "ai-title", "sessionId": "sess-1", "aiTitle": "Заголовок сессии"},
        {"type": "user", "sessionId": "sess-1"},
    ])
    conn = store.connect(tmp_path / "u.sqlite")
    res = ingest.backfill_titles(conn, tmp_path / "projects")
    assert res["files"] == 1
    assert res["titles"] == 1
    got = store.get_session_title(conn, "sess-1")
    assert got["title"] == "Заголовок сессии"
    # requests и file-offset не тронуты
    assert conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 0
    assert store.get_file_state(conn, f) is None


def test_backfill_is_idempotent(tmp_path):
    projects = tmp_path / "projects" / "proj1"
    projects.mkdir(parents=True)
    f = projects / "sess-1.jsonl"
    _write_jsonl(f, [{"type": "ai-title", "sessionId": "sess-1", "aiTitle": "Имя"}])
    conn = store.connect(tmp_path / "u.sqlite")
    ingest.backfill_titles(conn, tmp_path / "projects")
    res2 = ingest.backfill_titles(conn, tmp_path / "projects")
    assert res2["titles"] == 1     # нашли снова, но не задублировали
    assert conn.execute("SELECT COUNT(*) FROM session_meta").fetchone()[0] == 1


def test_backfill_on_missing_dir_returns_zero(tmp_path):
    conn = store.connect(tmp_path / "u.sqlite")
    res = ingest.backfill_titles(conn, tmp_path / "no-such-dir")
    assert res == {"files": 0, "titles": 0}
