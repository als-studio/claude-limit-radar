import json

import ingest
import store


def _line(req, msg, ts="2026-08-18T02:06:11.949Z", cw=100):
    return json.dumps({
        "timestamp": ts, "sessionId": "s1", "requestId": req,
        "message": {"id": msg, "model": "m",
                    "usage": {"input_tokens": 1, "output_tokens": 2,
                              "cache_creation_input_tokens": cw,
                              "cache_read_input_tokens": 0}},
    }) + "\n"


def test_reads_only_new_lines(tmp_path):
    f = tmp_path / "a.jsonl"
    f.write_bytes(_line("r1", "m1").encode("utf-8"))
    lines, off = ingest.read_new_lines(f, 0)
    assert len(lines) == 1
    with open(f, "ab") as fh:
        fh.write(_line("r2", "m2").encode("utf-8"))
    lines2, off2 = ingest.read_new_lines(f, off)
    assert len(lines2) == 1
    assert json.loads(lines2[0])["requestId"] == "r2"
    assert off2 > off


def test_truncated_tail_is_not_consumed(tmp_path):
    """Живая сессия дописывает файл: последняя строка может быть обрезана."""
    f = tmp_path / "a.jsonl"
    full = _line("r1", "m1")
    partial = _line("r2", "m2")[:20]        # обрыв на середине
    f.write_bytes((full + partial).encode("utf-8"))
    lines, off = ingest.read_new_lines(f, 0)
    assert len(lines) == 1                   # обрезанная не отдана
    assert off == len(full.encode("utf-8"))  # offset до последнего \n
    # дописали хвост — строка подбирается целиком
    rest = _line("r2", "m2")[20:]
    with open(f, "ab") as fh:
        fh.write(rest.encode("utf-8"))
    lines2, _ = ingest.read_new_lines(f, off)
    assert len(lines2) == 1
    assert json.loads(lines2[0])["requestId"] == "r2"


def test_rewritten_file_is_reread(tmp_path):
    """Файл стал короче сохранённого offset — значит переписан."""
    f = tmp_path / "a.jsonl"
    f.write_text(_line("r1", "m1") + _line("r2", "m2"), encoding="utf-8")
    _, off = ingest.read_new_lines(f, 0)
    f.write_text(_line("r9", "m9"), encoding="utf-8")   # короче прежнего
    lines, new_off = ingest.read_new_lines(f, off)
    assert len(lines) == 1
    assert json.loads(lines[0])["requestId"] == "r9"


def test_ingest_is_idempotent(tmp_path):
    f = tmp_path / "a.jsonl"
    f.write_text(_line("r1", "m1") + _line("r2", "m2"), encoding="utf-8")
    conn = store.connect(tmp_path / "u.sqlite")
    assert ingest.ingest_jsonl_file(conn, f)[0] == 2
    assert ingest.ingest_jsonl_file(conn, f)[0] == 0     # нечего дочитывать
    assert conn.execute("select count(*) from requests").fetchone()[0] == 2


def test_duplicate_usage_rows_counted_once(tmp_path):
    """Один API-запрос пишется несколькими строками с повтором usage."""
    f = tmp_path / "a.jsonl"
    f.write_text(_line("r1", "m1", cw=500) * 3, encoding="utf-8")
    conn = store.connect(tmp_path / "u.sqlite")
    ingest.ingest_jsonl_file(conn, f)
    total = conn.execute("select sum(cache_write) from requests").fetchone()[0]
    assert total == 500      # не 1500
