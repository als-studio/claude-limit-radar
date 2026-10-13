import io
import json
import os

import usage_api

FIXTURE = os.path.join(
    os.path.dirname(__file__), "..", "..",
    "docs", "02-features", "limit-via-oauth-usage", "sample-response.json")


def _load_fixture():
    # На Windows дефолтная локаль cp1252 роняет json.load на кириллице
    # в _comment — открывать строго как utf-8 (поймано при подготовке спеки).
    with io.open(FIXTURE, encoding="utf-8") as f:
        return json.load(f)


# --- read_token: чтение accessToken из credentials.json ---------------------
#
# Токен не рефрешим сами (свой refresh может разлогинить Claude Code) —
# протухший токен просто пропускает такт с причиной в логе, а не падает.

NOW_MS = 1788936599000  # 2026-09-09T06:49:59Z в мс


def _creds(access_token="tok123", expires_at=NOW_MS + 3600_000):
    return json.dumps({"claudeAiOauth": {
        "accessToken": access_token,
        "refreshToken": "rt",
        "expiresAt": expires_at,
        "scopes": ["user:inference"],
    }})


def test_read_token_missing_file_returns_reason():
    token, reason = usage_api.read_token(None, NOW_MS)
    assert token is None
    assert "файл" in reason


def test_read_token_missing_key_returns_reason():
    token, reason = usage_api.read_token(json.dumps({}), NOW_MS)
    assert token is None
    assert "ключ" in reason


def test_read_token_expired_returns_reason():
    token, reason = usage_api.read_token(
        _creds(expires_at=NOW_MS - 1000), NOW_MS)
    assert token is None
    assert "протух" in reason


def test_read_token_valid_returns_token():
    token, reason = usage_api.read_token(_creds(access_token="abc"), NOW_MS)
    assert token == "abc"
    assert reason is None


# --- iso_to_unix: resets_at из ответа API в unix UTC -------------------------
#
# Инвариант проекта: время в БД unix UTC, в локальную зону — только при
# рендере. resets_at приходит строкой ISO 8601, конвертируем один раз здесь.

def test_iso_to_unix_with_timezone():
    assert usage_api.iso_to_unix("2026-09-09T06:49:59.823775+00:00") == 1788936599


def test_iso_to_unix_without_timezone_assumes_utc():
    assert usage_api.iso_to_unix("2026-09-09T06:49:59") == 1788936599


def test_iso_to_unix_garbage_returns_none():
    assert usage_api.iso_to_unix("не дата") is None


def test_iso_to_unix_none_returns_none():
    assert usage_api.iso_to_unix(None) is None


# --- parse_usage: разбор ответа /api/oauth/usage -----------------------------
#
# Поля нестабильны (в фикстуре уже видны кодовые имена нераскатанных фич) —
# отсутствие любого поля даёт NULL, а не исключение. Поэтому сырой ответ
# всегда сохраняется целиком в raw.

TS = 1788936600.0


def test_parse_usage_full_fixture():
    payload = _load_fixture()
    row = usage_api.parse_usage(payload, TS)
    assert row["ts"] == TS
    assert row["five_hour_pct"] == 1.0
    assert row["five_hour_reset"] == 1788936599
    assert row["seven_day_pct"] == 9.0
    assert row["seven_day_reset"] == 1788933599
    assert row["scoped_pct"] == 12
    assert row["scoped_model"] == "Fable"
    assert row["scoped_reset"] is not None
    assert row["severity"] == "normal"
    assert row["extra_enabled"] == 0
    assert row["extra_pct"] == 0.0
    assert json.loads(row["raw"]) == payload


def test_parse_usage_without_limits():
    payload = _load_fixture()
    del payload["limits"]
    row = usage_api.parse_usage(payload, TS)
    assert row["scoped_pct"] is None
    assert row["scoped_model"] is None
    assert row["scoped_reset"] is None
    assert row["severity"] is None
    # окна без разбивки по модели разбираются независимо от limits[]
    assert row["five_hour_pct"] == 1.0
    assert row["seven_day_pct"] == 9.0


def test_parse_usage_without_extra_usage():
    payload = _load_fixture()
    del payload["extra_usage"]
    row = usage_api.parse_usage(payload, TS)
    assert row["extra_enabled"] is None
    assert row["extra_pct"] is None


def test_parse_usage_empty_payload_is_all_null_but_ts():
    row = usage_api.parse_usage({}, TS)
    assert row["ts"] == TS
    assert row["five_hour_pct"] is None
    assert row["seven_day_pct"] is None
    assert row["scoped_pct"] is None
    assert row["extra_enabled"] is None
    assert row["severity"] is None
    assert json.loads(row["raw"]) == {}


# --- should_poll: защита от слишком частого крона ---------------------------

def test_should_poll_first_run():
    ok, reason = usage_api.should_poll(now=1000.0, last_ts=None, min_interval=300)
    assert ok is True
    assert "первый" in reason


def test_should_poll_refuses_before_min_interval():
    ok, reason = usage_api.should_poll(now=1000.0, last_ts=900.0, min_interval=300)
    assert ok is False
    assert "рано" in reason


def test_should_poll_allows_after_min_interval():
    ok, reason = usage_api.should_poll(now=1000.0, last_ts=600.0, min_interval=300)
    assert ok is True


# --- build_headers: User-Agent обязателен, иначе вечный 429 -----------------

def test_build_headers_has_all_four_headers():
    headers = usage_api.build_headers("tok123", "2.1.234")
    assert headers["Authorization"] == "Bearer tok123"
    assert headers["anthropic-beta"] == "oauth-2025-04-20"
    assert headers["User-Agent"] == "claude-code/2.1.234"
    assert headers["Content-Type"] == "application/json"


# --- I/O-слой: claude_version, fetch -----------------------------------------
#
# Транспорт подменяемый, чтобы тесты не ходили в сеть и не зависели от того,
# установлен ли claude на машине, где гоняются тесты.

def test_claude_version_reads_from_run():
    run = lambda argv: (0, "2.1.263 (Claude Code)\n")
    assert usage_api.claude_version(run=run) == "2.1.263"


def test_claude_version_falls_back_when_binary_missing():
    run = lambda argv: (1, "")
    assert usage_api.claude_version(run=run) == usage_api.FALLBACK_VERSION


def test_fetch_returns_status_and_body(monkeypatch):
    class FakeResponse:
        status = 200

        def read(self):
            return b'{"ok": true}'

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(usage_api.urllib.request, "urlopen",
                        lambda req, timeout: FakeResponse())
    status, body = usage_api.fetch("https://x", {}, timeout=10)
    assert status == 200
    assert body == '{"ok": true}'


def test_fetch_returns_status_code_and_body_on_http_error(monkeypatch):
    def raise_http_error(req, timeout):
        raise usage_api.urllib.error.HTTPError(
            "https://x", 401, "Unauthorized", {}, io.BytesIO(b"denied"))

    monkeypatch.setattr(usage_api.urllib.request, "urlopen", raise_http_error)
    status, body = usage_api.fetch("https://x", {}, timeout=10)
    assert status == 401
    assert body == "denied"


def test_fetch_network_error_is_not_an_exception(monkeypatch):
    """По образцу limit_probe._run: сбой транспорта — не трейсбек, а (0, "")."""
    def raise_url_error(req, timeout):
        raise usage_api.urllib.error.URLError("boom")

    monkeypatch.setattr(usage_api.urllib.request, "urlopen", raise_url_error)
    status, body = usage_api.fetch("https://x", {}, timeout=10)
    assert status == 0
    assert body == ""
