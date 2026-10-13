# cc_usage/tests/test_ratelimits.py
import json
import subprocess
import sys
from pathlib import Path

import ingest
import store

STATUSLINE = Path(__file__).resolve().parent.parent / "statusline.py"

PAYLOAD = {
    "session_id": "s1",
    "model": {"id": "claude-opus-5", "display_name": "Opus 5"},
    "context_window": {"used_percentage": 9.4, "total_input_tokens": 94000,
                       "current_usage": {"cache_read_input_tokens": 29244,
                                         "cache_creation_input_tokens": 37771}},
    "cost": {"total_cost_usd": 1.23},
    "rate_limits": {"five_hour": {"used_percentage": 23.5, "resets_at": 1738425600},
                    "seven_day": {"used_percentage": 41.2, "resets_at": 1738857600}},
}


def _run_statusline(payload, cfgdir):
    """statusline.py вызывается как отдельный процесс — он автономен."""
    return subprocess.run(
        [sys.executable, str(STATUSLINE)],
        input=payload if isinstance(payload, str) else json.dumps(payload),
        capture_output=True, text=True, encoding="utf-8",
        env={**dict(__import__("os").environ), "CLAUDE_CONFIG_DIR": str(cfgdir)},
    )


def test_statusline_writes_daily_file(tmp_path):
    r = _run_statusline(PAYLOAD, tmp_path)
    assert r.returncode == 0
    assert "5h 23.5%" in r.stdout
    files = list((tmp_path / "cc-usage").glob("ratelimits-*.ndjson"))
    assert len(files) == 1
    rec = json.loads(files[0].read_text(encoding="utf-8").splitlines()[0])
    assert rec["five_hour_pct"] == 23.5
    assert rec["session"] == "s1"


def test_statusline_survives_missing_rate_limits(tmp_path):
    """На API-ключе rate_limits нет вовсе."""
    p = {k: v for k, v in PAYLOAD.items() if k != "rate_limits"}
    r = _run_statusline(p, tmp_path)
    assert r.returncode == 0
    assert r.stdout.strip()          # строка всё равно напечатана


def test_statusline_survives_garbage(tmp_path):
    r = _run_statusline("not json at all", tmp_path)
    assert r.returncode == 0
    assert r.stdout.strip()


def test_statusline_marks_first_write_fresh(tmp_path):
    """Первый тик сессии — настоящее наблюдение, а не эхо."""
    _run_statusline(PAYLOAD, tmp_path)
    files = list((tmp_path / "cc-usage").glob("ratelimits-*.ndjson"))
    rec = json.loads(files[0].read_text(encoding="utf-8").splitlines()[0])
    assert rec["fresh"] == 1


def test_statusline_skips_write_when_signature_unchanged(tmp_path):
    """Тот же sig внутри MIN_INTERVAL — строки не появляется вовсе."""
    _run_statusline(PAYLOAD, tmp_path)
    _run_statusline(PAYLOAD, tmp_path)
    files = list((tmp_path / "cc-usage").glob("ratelimits-*.ndjson"))
    assert len(files[0].read_text(encoding="utf-8").splitlines()) == 1


def test_statusline_marks_echo_not_fresh(tmp_path):
    """Тот же sig, но MIN_INTERVAL истёк — пишем эхо с fresh=0.

    Время не ждём: отматываем ts в файле состояния сессии назад.
    """
    _run_statusline(PAYLOAD, tmp_path)
    statef = tmp_path / "cc-usage" / "state-s1.json"
    state = json.loads(statef.read_text(encoding="utf-8"))
    state["ts"] -= 20                      # MIN_INTERVAL = 15
    statef.write_text(json.dumps(state), encoding="utf-8")

    _run_statusline(PAYLOAD, tmp_path)
    files = list((tmp_path / "cc-usage").glob("ratelimits-*.ndjson"))
    lines = files[0].read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[1])["fresh"] == 0


def test_statusline_state_is_per_session_not_shared(tmp_path):
    """Гонка параллельных хуков разных сессий устраняется структурно.

    Fix wave 2, находка 6 (Codex): общий state.json — read-modify-write без
    блокировки; две параллельные сессии могли затирать состояние друг
    друга. Состояние сессии теперь живёт в отдельном файле — параллельная
    запись другой сессии физически не может его задеть.
    """
    p_a = json.loads(json.dumps(PAYLOAD))
    p_a["session_id"] = "session-a"
    p_b = json.loads(json.dumps(PAYLOAD))
    p_b["session_id"] = "session-b"
    _run_statusline(p_a, tmp_path)
    _run_statusline(p_b, tmp_path)
    outdir = tmp_path / "cc-usage"
    assert not (outdir / "state.json").exists()
    assert (outdir / "state-session-a.json").exists()
    assert (outdir / "state-session-b.json").exists()


def test_statusline_marks_changed_signature_fresh(tmp_path):
    """Новый ответ API меняет и ctx_in, и cost_usd -> замер свежий.

    Реальный новый ответ API всегда добавляет хоть сколько-то к cost_usd
    (см. test_statusline_context_reset_without_new_cost_is_not_fresh —
    ctx_in один, без роста cost_usd, теперь НЕ считается свежим замером).
    """
    _run_statusline(PAYLOAD, tmp_path)
    p = json.loads(json.dumps(PAYLOAD))
    p["context_window"]["total_input_tokens"] = 95000
    p["cost"]["total_cost_usd"] = PAYLOAD["cost"]["total_cost_usd"] + 0.01
    _run_statusline(p, tmp_path)
    files = list((tmp_path / "cc-usage").glob("ratelimits-*.ndjson"))
    lines = files[0].read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[1])["fresh"] == 1


def test_statusline_context_reset_without_new_cost_is_not_fresh(tmp_path):
    """/clear меняет ctx_in без обращения к API — это не новое наблюдение.

    Fix wave 2, находка 5 (Kimi): sig включал только ctx_in, который может
    измениться и от /clear/rewind, не только от ответа API — ровно тот
    артефакт, ради устранения которого гейт свежести вводился. cost_usd
    монотонен и меняется только при реально оплаченном обращении к API,
    поэтому он даёт более точный сигнал "было ли обращение к API".
    """
    _run_statusline(PAYLOAD, tmp_path)
    p = json.loads(json.dumps(PAYLOAD))
    p["context_window"]["total_input_tokens"] = 500      # /clear уронил контекст
    # cost.total_cost_usd НЕ меняется — API не вызывался
    _run_statusline(p, tmp_path)
    files = list((tmp_path / "cc-usage").glob("ratelimits-*.ndjson"))
    lines = files[0].read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[1])["fresh"] == 0


def test_statusline_new_cost_without_ctx_change_is_fresh(tmp_path):
    """Рост cost_usd при неизменном ctx_in — всё равно реальный ответ API."""
    _run_statusline(PAYLOAD, tmp_path)
    p = json.loads(json.dumps(PAYLOAD))
    p["cost"]["total_cost_usd"] = PAYLOAD["cost"]["total_cost_usd"] + 0.01
    _run_statusline(p, tmp_path)
    files = list((tmp_path / "cc-usage").glob("ratelimits-*.ndjson"))
    lines = files[0].read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[1])["fresh"] == 1


def test_ingest_ratelimits_dedups_and_removes_old(tmp_path):
    out = tmp_path / "cc-usage"
    out.mkdir()
    rec = {"ts": 1000.0, "session": "s1", "five_hour_pct": 10.0,
           "five_hour_reset": 5, "seven_day_pct": 20.0, "seven_day_reset": 6,
           "ctx_pct": 3.0, "ctx_in": 100, "cost_usd": 0.1}
    old = out / "ratelimits-20260101.ndjson"
    old.write_text(json.dumps(rec) + "\n", encoding="utf-8")
    today = out / ingest.ratelimits_filename(__import__("time").time())
    today.write_text(json.dumps({**rec, "ts": 2000.0}) + "\n", encoding="utf-8")

    conn = store.connect(tmp_path / "u.sqlite")
    res = ingest.ingest_ratelimits(conn, out)
    assert res["inserted"] == 2
    assert not old.exists()          # вчерашний удалён после импорта
    assert today.exists()            # сегодняшний остаётся
    # повторный заход ничего не добавляет
    assert ingest.ingest_ratelimits(conn, out)["inserted"] == 0
