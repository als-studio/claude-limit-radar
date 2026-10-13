# cc_usage/tests/test_limit_samples.py
"""Импорт замеров лимита от мода Claude Code (limit-samples/<session>.ndjson).

Мод переписывает файл целиком, но тот лишь растёт: префикс не меняется.
Поэтому подходит тот же инкрементальный byte-offset, что и у ratelimits.
"""
import json
import os
import time

import export
import ingest
import store

REC = {"ts": 1791763247.12, "session": "s1", "five_hour_pct": 12.3,
       "five_hour_reset": 1791770000, "seven_day_pct": 43.0,
       "seven_day_reset": 1791957600, "fresh": 1, "source": "mod"}


def _line(**kw):
    return json.dumps({**REC, **kw}) + "\n"


def _samples_dir(tmp_path):
    d = tmp_path / "cc-usage" / "limit-samples"
    d.mkdir(parents=True)
    return d


def _age(path, seconds):
    t = time.time() - seconds
    os.utime(path, (t, t))


def test_parse_ratelimit_line_passes_source():
    assert ingest.parse_ratelimit_line(_line())["source"] == "mod"
    # строка статуслайна без source — NULL
    rec = {k: v for k, v in REC.items() if k != "source"}
    assert ingest.parse_ratelimit_line(json.dumps(rec))["source"] is None


def test_imports_and_stores_source(tmp_path):
    d = _samples_dir(tmp_path)
    (d / "s1.ndjson").write_text(_line() + _line(ts=REC["ts"] + 60),
                                 encoding="utf-8")
    conn = store.connect(tmp_path / "u.sqlite")
    res = ingest.ingest_limit_samples(conn, tmp_path / "cc-usage")
    assert res["inserted"] == 2
    assert res["files"] == 1
    rows = conn.execute("SELECT session_id, five_hour_pct, fresh, source"
                        " FROM ratelimits ORDER BY ts").fetchall()
    assert [tuple(r) for r in rows] == [("s1", 12.3, 1, "mod")] * 2


def test_repeated_import_adds_nothing(tmp_path):
    d = _samples_dir(tmp_path)
    f = d / "s1.ndjson"
    f.write_text(_line(), encoding="utf-8")
    conn = store.connect(tmp_path / "u.sqlite")
    out = tmp_path / "cc-usage"
    assert ingest.ingest_limit_samples(conn, out)["inserted"] == 1
    assert ingest.ingest_limit_samples(conn, out)["inserted"] == 0
    # даже если file-state потерян — дубликатов нет, держит PK (session_id, ts)
    conn.execute("DELETE FROM files")
    conn.commit()
    assert ingest.ingest_limit_samples(conn, out)["inserted"] == 0
    assert conn.execute("SELECT COUNT(*) FROM ratelimits").fetchone()[0] == 1


def test_grown_file_reads_only_new_lines(tmp_path, monkeypatch):
    d = _samples_dir(tmp_path)
    f = d / "s1.ndjson"
    f.write_text(_line(), encoding="utf-8")
    conn = store.connect(tmp_path / "u.sqlite")
    out = tmp_path / "cc-usage"
    ingest.ingest_limit_samples(conn, out)

    seen = []
    orig = ingest.parse_ratelimit_line
    monkeypatch.setattr(ingest, "parse_ratelimit_line",
                        lambda ln: seen.append(ln) or orig(ln))
    # мод переписывает файл целиком, префикс тот же
    f.write_text(_line() + _line(ts=REC["ts"] + 60, five_hour_pct=13.0),
                 encoding="utf-8")
    res = ingest.ingest_limit_samples(conn, out)
    assert res["inserted"] == 1
    assert len(seen) == 1                       # первую строку не перечитывали
    assert json.loads(seen[0])["five_hour_pct"] == 13.0


def test_partial_last_line_waits_until_complete(tmp_path):
    d = _samples_dir(tmp_path)
    f = d / "s1.ndjson"
    full2 = _line(ts=REC["ts"] + 60)
    f.write_text(_line() + full2[:20], encoding="utf-8")   # без перевода строки
    conn = store.connect(tmp_path / "u.sqlite")
    out = tmp_path / "cc-usage"
    assert ingest.ingest_limit_samples(conn, out)["inserted"] == 1
    f.write_text(_line() + full2, encoding="utf-8")
    assert ingest.ingest_limit_samples(conn, out)["inserted"] == 1
    assert conn.execute("SELECT COUNT(*) FROM ratelimits").fetchone()[0] == 2


def test_partial_line_blocks_deletion_of_old_file(tmp_path):
    """Недочитанный (оборванный) хвост — файл не удаляется даже старым."""
    d = _samples_dir(tmp_path)
    f = d / "s1.ndjson"
    f.write_text(_line() + '{"ts": 17', encoding="utf-8")
    _age(f, 2 * 86400)
    conn = store.connect(tmp_path / "u.sqlite")
    res = ingest.ingest_limit_samples(conn, tmp_path / "cc-usage")
    assert res["inserted"] == 1
    assert res["removed"] == 0
    assert f.exists()


def test_missing_and_empty_dir(tmp_path):
    conn = store.connect(tmp_path / "u.sqlite")
    out = tmp_path / "cc-usage"
    empty = {"files": 0, "inserted": 0, "removed": 0}
    assert ingest.ingest_limit_samples(conn, out) == empty     # нет каталога
    _samples_dir(tmp_path)
    assert ingest.ingest_limit_samples(conn, out) == empty     # пустой каталог


def test_old_fully_read_file_removed_fresh_kept(tmp_path):
    d = _samples_dir(tmp_path)
    old = d / "old.ndjson"
    old.write_text(_line(session="old"), encoding="utf-8")
    _age(old, 25 * 3600)
    fresh = d / "live.ndjson"
    fresh.write_text(_line(session="live"), encoding="utf-8")
    conn = store.connect(tmp_path / "u.sqlite")
    res = ingest.ingest_limit_samples(conn, tmp_path / "cc-usage")
    assert res["inserted"] == 2
    assert res["removed"] == 1
    assert not old.exists()          # сессия закончилась, всё прочитано
    assert fresh.exists()            # сессия, возможно, ещё идёт


def test_collect_includes_limit_samples(tmp_path):
    d = _samples_dir(tmp_path)
    (d / "s1.ndjson").write_text(_line(), encoding="utf-8")
    (tmp_path / "projects").mkdir()
    conn = store.connect(tmp_path / "u.sqlite")
    res = ingest.collect(conn, tmp_path / "projects", tmp_path / "cc-usage")
    assert res["limit_samples"] == 1
    log = (tmp_path / "cc-usage" / "collect.log").read_text(encoding="utf-8")
    assert "замеров мода 1" in log


def test_export_roundtrip_keeps_source(tmp_path):
    d = _samples_dir(tmp_path)
    (d / "s1.ndjson").write_text(_line(), encoding="utf-8")
    conn = store.connect(tmp_path / "u.sqlite")
    ingest.ingest_limit_samples(conn, tmp_path / "cc-usage")
    bundle = export.export_bundle(conn, tmp_path / "b.json.gz")
    conn2 = store.connect(tmp_path / "u2.sqlite")
    export.import_bundle(conn2, bundle)
    row = conn2.execute("SELECT source FROM ratelimits").fetchone()
    assert row["source"] == "mod"


def test_migrate_adds_source_to_existing_ratelimits(tmp_path):
    import sqlite3
    db = tmp_path / "u.sqlite"
    raw = sqlite3.connect(str(db))
    raw.execute("CREATE TABLE ratelimits (ts REAL, session_id TEXT,"
                " five_hour_pct REAL, five_hour_reset INTEGER,"
                " seven_day_pct REAL, seven_day_reset INTEGER,"
                " ctx_pct REAL, ctx_in INTEGER, cost_usd REAL, fresh INTEGER,"
                " PRIMARY KEY (session_id, ts))")
    raw.execute("INSERT INTO ratelimits (ts, session_id, five_hour_pct)"
                " VALUES (1000, 's1', 10.0)")
    raw.commit()
    raw.close()
    conn = store.connect(db)
    row = conn.execute("SELECT five_hour_pct, source FROM ratelimits").fetchone()
    assert row["five_hour_pct"] == 10.0
    assert row["source"] is None
