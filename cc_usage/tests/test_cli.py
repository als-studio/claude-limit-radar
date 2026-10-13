import re

import analyze
import cli
import store


def _seed(conn):
    rows = [dict(request_id="r%d" % i, message_id="m%d" % i, session_id="s1",
                 ts=1755500000 + i * 10, model="claude-sonnet-5", effort=None,
                 service_tier=None, input=0, output=5, thinking=0,
                 cache_read=900, cache_write=1000, cache_1h=1000, cache_5m=0,
                 is_sidechain=0, agent_file=None, project="p", cwd=None,
                 git_branch=None, cc_version=None) for i in range(8)]
    rows.append(dict(rows[0], request_id="rx", message_id="mx",
                     ts=1755500000 + 9000, cache_write=900000, cache_1h=900000))
    store.insert_requests(conn, rows)
    store.rebuild_sessions(conn)


def test_explain_reports_cold_cache_and_diagnoses(tmp_path):
    conn = store.connect(tmp_path / "u.sqlite")
    _seed(conn)
    text = cli.explain_text(conn)
    assert "спайк" not in text.lower()
    assert "остывш" in text.lower()
    assert "$" in text
    assert len(text) < 8000


def test_explain_on_empty_db_says_no_data(tmp_path):
    conn = store.connect(tmp_path / "u.sqlite")
    text = cli.explain_text(conn)
    assert "нет данных" in text.lower()


def test_cli_collect_runs(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    projects.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    assert cli.main(["collect"]) == 0


def test_cli_backfill_titles_runs(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    projects.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    assert cli.main(["backfill-titles"]) == 0


def test_cli_report_accepts_since(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    (tmp_path / "projects").mkdir()
    conn = store.connect(tmp_path / "cc-usage" / "usage.sqlite")
    _seed(conn)
    conn.close()
    assert cli.main(["report", "--since", "2h", "--out", str(tmp_path / "r.html")]) == 0
    assert (tmp_path / "r.html").exists()


def test_cli_report_days_is_synonym_for_since(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    (tmp_path / "projects").mkdir()
    conn = store.connect(tmp_path / "cc-usage" / "usage.sqlite")
    _seed(conn)
    conn.close()
    assert cli.main(["report", "--days", "1", "--out", str(tmp_path / "r.html")]) == 0


def test_cli_help_never_says_spike(capsys):
    import pytest
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    out = capsys.readouterr().out.lower()
    assert "спайк" not in out


def test_cli_report_debug_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    (tmp_path / "projects").mkdir()
    conn = store.connect(tmp_path / "cc-usage" / "usage.sqlite")
    _seed(conn)
    conn.close()
    out = tmp_path / "r.html"
    assert cli.main(["report", "--debug", "--out", str(out)]) == 0
    assert "перевод в проценты лимита" in out.read_text(encoding="utf-8").lower()


def test_cli_session_resolves_by_name(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    (tmp_path / "projects").mkdir()
    conn = store.connect(tmp_path / "cc-usage" / "usage.sqlite")
    _seed(conn)
    store.set_session_title(conn, "s1", "Уникальное имя сессии", "ai-title", 1755500000.0)
    conn.close()
    assert cli.main(["explain", "--session", "Уникальное"]) == 0


def test_explain_empty_window_differs_from_empty_db(tmp_path):
    conn = store.connect(tmp_path / "u.sqlite")
    _seed(conn)
    text = cli.explain_text(conn, since="2099-01-01")
    assert "база пуста" not in text.lower()
    assert "collect" not in text.lower()
    assert "окне" in text.lower()


def test_explain_session_shows_dollar_total(tmp_path):
    # Finding 2 (финальное ревью): разбор сессии раньше показывал только
    # токены ("стоимость входа", "за первые 5 минут") — итог сессии в $
    # отсутствовал вовсе.
    conn = store.connect(tmp_path / "u.sqlite")
    _seed(conn)
    text = cli.explain_text(conn, session="s1")
    assert re.search(r"итого по сессии: \$", text.lower())


def test_cli_unknown_session_prints_error_not_traceback(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    (tmp_path / "projects").mkdir()
    conn = store.connect(tmp_path / "cc-usage" / "usage.sqlite")
    _seed(conn)
    conn.close()
    rc = cli.main(["explain", "--session", "нет-такой-сессии"])
    assert rc != 0
    out = capsys.readouterr().out
    assert "сессия не найдена" in out.lower()


def test_explain_shows_limit_snapshot_when_window_has_no_requests(tmp_path):
    # AC1 не требует наличия requests в окне — только хотя бы одного замера
    # ratelimits (Fix 1, финальное ревью).
    conn = store.connect(tmp_path / "u.sqlite")
    _seed(conn)
    store.insert_ratelimits(conn, [
        dict(ts=4200000000, session_id="s1", five_hour_pct=42.0, five_hour_reset=0,
             seven_day_pct=7.0, seven_day_reset=0, ctx_pct=0.0, ctx_in=0, cost_usd=0.0),
    ])
    text = cli.explain_text(conn, since="2099-01-01")
    assert "окне нет запросов" in text.lower()
    assert re.search(r"5h 42\.0%", text)


def test_explain_shows_usage_api_source_when_no_ratelimits(tmp_path):
    """Стадия B: без единого замера ratelimits explain всё равно показывает
    лимит из poll /api/oauth/usage и называет источник."""
    conn = store.connect(tmp_path / "u.sqlite")
    _seed(conn)
    store.insert_usage_snapshots(conn, [
        dict(ts=4200000000, five_hour_pct=5.0, five_hour_reset=0,
             seven_day_pct=20.0, seven_day_reset=0,
             scoped_pct=None, scoped_model=None, scoped_reset=None,
             extra_enabled=None, extra_pct=None, severity=None, raw="{}")])
    text = cli.explain_text(conn, since="2099-01-01")
    assert "/api/oauth/usage" in text
    assert re.search(r"5h 5\.0%", text)
    assert re.search(r"7d 20\.0%", text)


def test_explain_limit_note_low_confidence_wording_does_not_say_no_breakdown(tmp_path):
    conn = store.connect(tmp_path / "u.sqlite")
    _seed(conn)
    store.insert_ratelimits(conn, [
        dict(ts=1755500000 + 5, session_id="s1", five_hour_pct=10.0, five_hour_reset=0,
             seven_day_pct=0.0, seven_day_reset=0, ctx_pct=0.0, ctx_in=0, cost_usd=0.0),
        dict(ts=1755500000 + 15, session_id="s1", five_hour_pct=12.0, five_hour_reset=0,
             seven_day_pct=0.0, seven_day_reset=0, ctx_pct=0.0, ctx_in=0, cost_usd=0.0),
    ])
    text = cli.explain_text(conn)
    assert "наблюдений мало" in text.lower()
    assert "недостаточно данных для разбивки" not in text.lower()


def test_explain_has_limit_section_no_data(tmp_path):
    # test_cli.py уже определяет _seed(conn) вверху файла (session "s1",
    # requests с cache_write) — переиспользуем его, не дублируем поля.
    conn = store.connect(tmp_path / "u.sqlite")
    _seed(conn)
    text = cli.explain_text(conn)
    assert "## Лимит" in text
    assert "данных нет" in text.lower()   # requests есть, ratelimits нет — сработает новая ветка, не старая "нет данных: база пуста"


def test_explain_lists_limit_events(tmp_path):
    conn = store.connect(tmp_path / "u.sqlite")
    store.insert_ratelimits(conn, [
        dict(ts=1000, session_id="a", five_hour_pct=10.0, five_hour_reset=0,
             seven_day_pct=10.0, seven_day_reset=0, ctx_pct=0.0, ctx_in=0,
             cost_usd=0.0, fresh=1),
        dict(ts=2000, session_id="a", five_hour_pct=20.0, five_hour_reset=0,
             seven_day_pct=20.0, seven_day_reset=0, ctx_pct=0.0, ctx_in=0,
             cost_usd=0.0, fresh=1)])
    text = cli.explain_text(conn, since=None, until=None)
    assert "События" in text
    assert "+10.0 пп" in text
    assert "не с этой машины" in text
    # оба поля лимита выросли одинаково — теперь в ленте обе строки
    assert "5h +10.0 пп" in text
    assert "7d +10.0 пп" in text


def test_explain_lists_seven_day_events_without_five_hour_ones(tmp_path):
    """События недельного лимита раньше не показывались вообще.

    Блок «События» брал limit_events без pct_field, то есть всегда только
    five_hour_pct (финальное ревью limit-events).
    """
    conn = store.connect(tmp_path / "u.sqlite")
    store.insert_ratelimits(conn, [
        dict(ts=1000, session_id="a", five_hour_pct=10.0, five_hour_reset=0,
             seven_day_pct=3.0, seven_day_reset=0, ctx_pct=0.0, ctx_in=0,
             cost_usd=0.0, fresh=1),
        dict(ts=2000, session_id="a", five_hour_pct=10.0, five_hour_reset=0,
             seven_day_pct=8.0, seven_day_reset=0, ctx_pct=0.0, ctx_in=0,
             cost_usd=0.0, fresh=1)])
    text = cli.explain_text(conn, since=None, until=None)
    assert "7d +5.0 пп" in text
    assert "5h +" not in text          # по 5h роста нет — строки быть не должно


def test_explain_limit_spike_shows_jump_and_leader_share_separately(tmp_path):
    """Скачок и доля лидера в нём — два разных числа в одной строке."""
    conn = store.connect(tmp_path / "u.sqlite")
    _seed(conn)
    rl_rows, req_rows = [], []
    for i in range(101):
        ts = 1755600000 + i * 10
        rl_rows.append(dict(ts=ts, session_id="bg", five_hour_pct=i * 0.1,
                            five_hour_reset=0, seven_day_pct=0.0, seven_day_reset=0,
                            ctx_pct=0.0, ctx_in=0, cost_usd=0.0))
        if i > 0:
            req_rows.append(dict(request_id="bg%d" % i, message_id="bg%d" % i,
                                 session_id="bg", ts=ts - 5, model="claude-opus-5",
                                 effort=None, service_tier=None, input=0, output=0,
                                 thinking=0, cache_read=0, cache_write=1000,
                                 cache_1h=0, cache_5m=0, is_sidechain=0, agent_file=None,
                                 project="p", cwd=None, git_branch=None, cc_version=None))
    store.insert_ratelimits(conn, rl_rows)
    store.insert_requests(conn, req_rows)
    # +50 пп на двух кандидатов (4M и 1M) — лидеру достаётся 40 пп, не 50
    store.insert_ratelimits(conn, [
        dict(ts=1755700000, session_id="spike", five_hour_pct=0.0, five_hour_reset=0,
             seven_day_pct=0.0, seven_day_reset=0, ctx_pct=0.0, ctx_in=0,
             cost_usd=0.0, fresh=1),
        dict(ts=1755700100, session_id="spike", five_hour_pct=50.0, five_hour_reset=0,
             seven_day_pct=0.0, seven_day_reset=0, ctx_pct=0.0, ctx_in=0,
             cost_usd=0.0, fresh=1),
    ])
    store.insert_requests(conn, [
        dict(request_id="sp1", message_id="sp1", session_id="spike", ts=1755700050,
             model="claude-opus-5", effort=None, service_tier=None, input=0, output=0,
             thinking=0, cache_read=0, cache_write=4_000_000, cache_1h=0, cache_5m=0,
             is_sidechain=0, agent_file=None, project="p", cwd=None, git_branch=None,
             cc_version=None),
        dict(request_id="sp2", message_id="sp2", session_id="other", ts=1755700060,
             model="claude-opus-5", effort=None, service_tier=None, input=0, output=0,
             thinking=0, cache_read=0, cache_write=1_000_000, cache_1h=0, cache_5m=0,
             is_sidechain=0, agent_file=None, project="p", cwd=None, git_branch=None,
             cc_version=None)])
    text = cli.explain_text(conn)
    assert "самый резкий одиночный скачок +50.0 пп" in text
    assert "~40.0 пп" in text


# --- probe-limit: зонд лимита ---------------------------------------------
#
# Зонд тратит лимит аккаунта. Прежняя версия (новый pty каждые 15 минут)
# выжигала недельный лимит Claude Pro за 2.6 суток, поэтому здесь проверяется
# ровно одно: без причины ping не уходит.

import limit_probe   # noqa: E402


def _no_tmux(monkeypatch):
    monkeypatch.setattr(limit_probe, "tmux_available", lambda: True)


def test_probe_limit_fails_when_tmux_is_missing(tmp_path, monkeypatch, capsys):
    """Отказ должен быть именно про tmux: тот же код возврата даёт и «ping не
    ушёл», а это совсем другая причина и другое лечение."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(limit_probe, "tmux_available", lambda: False)
    assert cli.main(["probe-limit"]) != 0
    assert "tmux" in capsys.readouterr().out


def test_probe_limit_sends_no_ping_without_activity(tmp_path, monkeypatch):
    """Главная защита от повторения аварии: нет работы — нет трат."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    _no_tmux(monkeypatch)
    conn = store.connect(tmp_path / "cc-usage" / "usage.sqlite")
    store.set_meta(conn, "last_probe_ts", "9000")
    conn.close()

    sent = []
    monkeypatch.setattr(limit_probe, "send_ping",
                        lambda *a, **k: sent.append(a) or True)
    monkeypatch.setattr(limit_probe, "ensure_session",
                        lambda *a, **k: (False, "сессия жива"))
    monkeypatch.setattr(cli.time, "time", lambda: 9000 + 1000)

    assert cli.main(["probe-limit", "--if-active"]) == 0
    assert sent == []


def test_probe_limit_pings_and_remembers_the_moment(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    _no_tmux(monkeypatch)
    conn = store.connect(tmp_path / "cc-usage" / "usage.sqlite")
    store.set_meta(conn, "last_probe_ts", "9000")
    store.insert_requests(conn, [dict(
        request_id="r1", message_id="m1", session_id="s1", ts=9500.0,
        model="claude-sonnet-5", effort=None, service_tier=None,
        input=1, output=1, thinking=0, cache_read=0, cache_write=0,
        cache_1h=0, cache_5m=0, is_sidechain=0, agent_file=None,
        project="p", cwd="/home/u/work", git_branch=None, cc_version=None)])
    conn.close()

    sent = []
    monkeypatch.setattr(limit_probe, "send_ping",
                        lambda *a, **k: sent.append(a) or True)
    monkeypatch.setattr(limit_probe, "ensure_session",
                        lambda *a, **k: (False, "сессия жива"))
    monkeypatch.setattr(limit_probe, "capture_pane",
                        lambda *a, **k: "❯ \n  Haiku 4.5 | 5h 18.0% | 7d 15.0%")
    monkeypatch.setattr(cli.time, "time", lambda: 10000)

    assert cli.main(["probe-limit", "--if-active"]) == 0
    assert len(sent) == 1
    conn = store.connect(tmp_path / "cc-usage" / "usage.sqlite")
    assert float(store.get_meta(conn, "last_probe_ts")) == 10000


def test_probe_limit_sends_nothing_into_a_blocking_screen(
        tmp_path, monkeypatch, capsys):
    """Сессия могла застрять на первичном экране ещё до этого запуска.

    send_ping шлёт «ping» и Enter вслепую, а Enter отвечает на любой открытый
    диалог: подтверждает доверие к каталогу, выбирает тему. Оба случая
    случились на бою. Проверять готовность только при создании сессии
    недостаточно — cron ответит за пользователя на следующем же такте.
    """
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    _no_tmux(monkeypatch)
    sent = []
    monkeypatch.setattr(limit_probe, "send_ping",
                        lambda *a, **k: sent.append(a) or True)
    monkeypatch.setattr(limit_probe, "ensure_session",
                        lambda *a, **k: (False, "сессия жива"))
    monkeypatch.setattr(limit_probe, "capture_pane",
                        lambda *a, **k: " Choose your theme\n 5. Dark mode")

    assert cli.main(["probe-limit"]) != 0
    assert sent == []
    out = capsys.readouterr().out
    assert "не готова" in out
    assert "Dark mode" in out          # в логе видно, чем именно заблокирована


# --- poll-limits: замер через /api/oauth/usage ------------------------------
#
# В отличие от probe-limit, это не инференс-запрос — эндпоинт бесплатен
# (проверяется отдельным гейтом после раскатки). Ретраев нет ни при каком
# коде ответа, поэтому все ожидаемые исходы (401/429/сеть) возвращают 0 —
# крон не должен слать почту из-за протухшего токена.

import json as _json  # noqa: E402
import usage_api      # noqa: E402


def _write_creds(tmp_path, expires_at_ms):
    (tmp_path / ".credentials.json").write_text(
        _json.dumps({"claudeAiOauth": {"accessToken": "tok123",
                                       "expiresAt": expires_at_ms}}),
        encoding="utf-8")


FIXTURE_PAYLOAD = {
    "five_hour": {"utilization": 1.0, "resets_at": "2026-09-09T06:49:59+00:00"},
    "seven_day": {"utilization": 9.0, "resets_at": "2026-09-09T05:59:59+00:00"},
    "limits": [],
    "extra_usage": {"is_enabled": False, "utilization": 0.0},
}


def test_poll_limits_fetches_and_stores_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(cli.time, "time", lambda: 10000.0)
    _write_creds(tmp_path, expires_at_ms=10000_000 + 3600_000)
    monkeypatch.setattr(usage_api, "claude_version", lambda: "2.1.234")
    monkeypatch.setattr(usage_api, "fetch",
                        lambda url, headers, timeout: (200, _json.dumps(FIXTURE_PAYLOAD)))

    assert cli.main(["poll-limits"]) == 0

    conn = store.connect(tmp_path / "cc-usage" / "usage.sqlite")
    row = conn.execute("SELECT * FROM usage_snapshots WHERE ts = 10000.0").fetchone()
    assert row["five_hour_pct"] == 1.0
    assert row["seven_day_pct"] == 9.0
    assert float(store.get_meta(conn, "last_usage_poll_ts")) == 10000.0


def test_poll_limits_second_call_too_soon_is_skipped(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    _write_creds(tmp_path, expires_at_ms=10000_000 + 3600_000)
    monkeypatch.setattr(usage_api, "claude_version", lambda: "2.1.234")
    calls = []
    monkeypatch.setattr(usage_api, "fetch",
                        lambda url, headers, timeout: calls.append(1) or (200, _json.dumps(FIXTURE_PAYLOAD)))

    monkeypatch.setattr(cli.time, "time", lambda: 10000.0)
    assert cli.main(["poll-limits", "--min-interval", "300"]) == 0
    monkeypatch.setattr(cli.time, "time", lambda: 10100.0)
    assert cli.main(["poll-limits", "--min-interval", "300"]) == 0

    assert len(calls) == 1
    conn = store.connect(tmp_path / "cc-usage" / "usage.sqlite")
    assert conn.execute("select count(*) from usage_snapshots").fetchone()[0] == 1


def test_poll_limits_missing_credentials_returns_zero(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(cli.time, "time", lambda: 10000.0)
    calls = []
    monkeypatch.setattr(usage_api, "fetch", lambda *a, **k: calls.append(1))

    assert cli.main(["poll-limits"]) == 0
    assert calls == []
    assert "credentials" in capsys.readouterr().out


def test_poll_limits_expired_token_returns_zero(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(cli.time, "time", lambda: 10000.0)
    _write_creds(tmp_path, expires_at_ms=1000)   # в прошлом относительно now
    calls = []
    monkeypatch.setattr(usage_api, "fetch", lambda *a, **k: calls.append(1))

    assert cli.main(["poll-limits"]) == 0
    assert calls == []
    assert "протух" in capsys.readouterr().out


def test_poll_limits_401_does_not_store_and_returns_zero(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(cli.time, "time", lambda: 10000.0)
    _write_creds(tmp_path, expires_at_ms=10000_000 + 3600_000)
    monkeypatch.setattr(usage_api, "claude_version", lambda: "2.1.234")
    monkeypatch.setattr(usage_api, "fetch", lambda url, headers, timeout: (401, ""))

    assert cli.main(["poll-limits"]) == 0
    assert "401" in capsys.readouterr().out
    conn = store.connect(tmp_path / "cc-usage" / "usage.sqlite")
    assert conn.execute("select count(*) from usage_snapshots").fetchone()[0] == 0


def test_poll_limits_429_does_not_store_and_returns_zero(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(cli.time, "time", lambda: 10000.0)
    _write_creds(tmp_path, expires_at_ms=10000_000 + 3600_000)
    monkeypatch.setattr(usage_api, "claude_version", lambda: "2.1.234")
    monkeypatch.setattr(usage_api, "fetch", lambda url, headers, timeout: (429, ""))

    assert cli.main(["poll-limits"]) == 0
    assert "429" in capsys.readouterr().out
    conn = store.connect(tmp_path / "cc-usage" / "usage.sqlite")
    assert conn.execute("select count(*) from usage_snapshots").fetchone()[0] == 0


def test_poll_limits_network_error_returns_zero(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(cli.time, "time", lambda: 10000.0)
    _write_creds(tmp_path, expires_at_ms=10000_000 + 3600_000)
    monkeypatch.setattr(usage_api, "claude_version", lambda: "2.1.234")
    monkeypatch.setattr(usage_api, "fetch", lambda url, headers, timeout: (0, ""))

    assert cli.main(["poll-limits"]) == 0
    conn = store.connect(tmp_path / "cc-usage" / "usage.sqlite")
    assert conn.execute("select count(*) from usage_snapshots").fetchone()[0] == 0
