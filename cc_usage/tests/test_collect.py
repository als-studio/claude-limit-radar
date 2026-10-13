import json

import ingest
import store


def _line(req, msg, ts, sess="s1", cw=100):
    return json.dumps({
        "timestamp": ts, "sessionId": sess, "requestId": req,
        "cwd": "C:/p", "gitBranch": "main",
        "message": {"id": msg, "model": "claude-opus-5",
                    "usage": {"input_tokens": 1, "output_tokens": 2,
                              "cache_creation_input_tokens": cw,
                              "cache_read_input_tokens": 10}},
    }) + "\n"


def _make_project(tmp_path):
    proj = tmp_path / "projects" / "myproj"
    proj.mkdir(parents=True)
    (proj / "s1.jsonl").write_text(
        _line("r1", "m1", "2026-08-18T10:00:00Z")
        + _line("r2", "m2", "2026-08-18T10:05:00Z"),
        encoding="utf-8")
    sub = proj / "s1" / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-x.jsonl").write_text(
        _line("r3", "m3", "2026-08-18T10:02:00Z"), encoding="utf-8")
    return tmp_path / "projects"


def test_collect_counts_subagents_and_builds_sessions(tmp_path):
    projects = _make_project(tmp_path)
    outdir = tmp_path / "cc-usage"
    outdir.mkdir()
    conn = store.connect(tmp_path / "u.sqlite")

    res = ingest.collect(conn, projects, outdir)
    assert res["inserted"] == 3           # включая субагента
    assert res["sessions"] == 1

    row = conn.execute("select * from sessions").fetchone()
    assert row["n_requests"] == 3
    assert row["cache_write"] == 300
    assert row["project"] == "myproj"

    n_sub = conn.execute(
        "select count(*) from requests where is_sidechain = 1").fetchone()[0]
    assert n_sub == 1


def test_collect_is_idempotent(tmp_path):
    projects = _make_project(tmp_path)
    outdir = tmp_path / "cc-usage"
    outdir.mkdir()
    conn = store.connect(tmp_path / "u.sqlite")
    ingest.collect(conn, projects, outdir)
    before = conn.execute("select sum(cache_write) from requests").fetchone()[0]
    res2 = ingest.collect(conn, projects, outdir)
    after = conn.execute("select sum(cache_write) from requests").fetchone()[0]
    assert res2["inserted"] == 0
    assert before == after


def test_log_rotates(tmp_path):
    ingest.log_line(tmp_path, "x" * 100)
    assert (tmp_path / "collect.log").exists()
    assert "x" * 100 in (tmp_path / "collect.log").read_text(encoding="utf-8")


def test_log_rotation_maintains_three_generations(tmp_path, monkeypatch):
    """Verify rotation keeps exactly 3 files: collect.log, .1, .2 (not .3)."""
    monkeypatch.setattr(ingest, "LOG_MAX_BYTES", 100)  # tiny threshold

    # Write 5 large lines to force 4+ rotations, reaching steady state
    for i in range(5):
        ingest.log_line(tmp_path, f"generation-{i}: " + "x" * 80)

    # Check exactly 3 log files exist
    log_files = sorted(tmp_path.glob("collect.log*"))
    assert len(log_files) == 3, f"Expected 3 files, got {len(log_files)}: {log_files}"

    # Verify correct names (no .3 file)
    names = {f.name for f in log_files}
    assert names == {"collect.log", "collect.log.1", "collect.log.2"}

    # Verify oldest content is gone (generation-0 should not appear in any file)
    all_content = "\n".join(f.read_text(encoding="utf-8") for f in log_files)
    assert "generation-0" not in all_content
    # But newer content should be present
    assert "generation-4" in all_content
