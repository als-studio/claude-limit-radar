import json
from pathlib import Path

import ingest
import store

LINE = json.dumps({
    "timestamp": "2026-08-18T02:06:11.949Z",
    "sessionId": "sess-1",
    "requestId": "req_1",
    "isSidechain": False,
    "cwd": "C:\\Projects\\demo",
    "gitBranch": "main",
    "version": "2.1.234",
    "effort": "high",
    "message": {
        "id": "msg_1",
        "model": "claude-opus-5",
        "usage": {
            "input_tokens": 2,
            "output_tokens": 1081,
            "output_tokens_details": {"thinking_tokens": 790},
            "cache_creation_input_tokens": 37771,
            "cache_read_input_tokens": 29244,
            "service_tier": "standard",
            "cache_creation": {
                "ephemeral_1h_input_tokens": 37771,
                "ephemeral_5m_input_tokens": 0,
            },
        },
    },
})


def test_parses_all_fields():
    r = ingest.parse_usage_line(LINE)
    assert r["request_id"] == "req_1"
    assert r["message_id"] == "msg_1"
    assert r["session_id"] == "sess-1"
    assert r["input"] == 2 and r["output"] == 1081 and r["thinking"] == 790
    assert r["cache_write"] == 37771 and r["cache_read"] == 29244
    assert r["cache_1h"] == 37771 and r["cache_5m"] == 0
    assert r["model"] == "claude-opus-5"
    assert r["git_branch"] == "main"
    # 2026-08-18T02:06:11.949Z в unix UTC
    assert abs(r["ts"] - 1787018771.949) < 1.0


def test_missing_cache_creation_gives_zero():
    """Старые версии Claude Code писали usage без cache_creation."""
    d = json.loads(LINE)
    del d["message"]["usage"]["cache_creation"]
    r = ingest.parse_usage_line(json.dumps(d))
    assert r["cache_1h"] == 0 and r["cache_5m"] == 0
    assert r["cache_write"] == 37771  # общее поле на месте


def test_line_without_usage_returns_none():
    assert ingest.parse_usage_line(json.dumps({"type": "user"})) is None


def test_broken_line_returns_none_and_does_not_raise():
    assert ingest.parse_usage_line("{not json") is None
    assert ingest.parse_usage_line("") is None


def test_subagent_path_detected():
    p = Path("projects/proj/sess/subagents/agent-abc.jsonl")
    assert ingest.is_subagent_path(p) is True
    r = ingest.parse_usage_line(LINE, source_path=p)
    assert r["is_sidechain"] == 1
    assert r["agent_file"] == "agent-abc.jsonl"


def test_parses_ai_title_line():
    line = json.dumps({"type": "ai-title", "sessionId": "sess-1",
                       "aiTitle": "Реализовать статистику по тегам"})
    r = ingest.parse_title_line(line)
    assert r == ("sess-1", "Реализовать статистику по тегам", "ai-title")


def test_parses_last_prompt_line_and_truncates_to_80():
    long_prompt = "Реализуй фичу «Статистика по тегам» " + "с деталями " * 20
    line = json.dumps({"type": "last-prompt", "sessionId": "sess-1",
                       "lastPrompt": long_prompt})
    sid, title, source = ingest.parse_title_line(line)
    assert sid == "sess-1"
    assert source == "last-prompt"
    assert len(title) <= 81   # 80 + многоточие
    assert title.endswith("…")


def test_title_line_without_session_id_is_none():
    line = json.dumps({"type": "ai-title", "aiTitle": "Заголовок"})
    assert ingest.parse_title_line(line) is None


def test_title_line_with_empty_value_is_none():
    line = json.dumps({"type": "ai-title", "sessionId": "s1", "aiTitle": ""})
    assert ingest.parse_title_line(line) is None


def test_unrelated_line_is_not_a_title_line():
    assert ingest.parse_title_line(json.dumps({"type": "user"})) is None
    assert ingest.parse_title_line("{not json") is None
    assert ingest.parse_title_line("") is None


def test_title_line_with_non_string_value_is_none_and_does_not_raise():
    line_number = json.dumps({"type": "ai-title", "sessionId": "s1", "aiTitle": 123})
    line_list = json.dumps({"type": "last-prompt", "sessionId": "s1",
                            "lastPrompt": ["not", "a", "string"]})
    assert ingest.parse_title_line(line_number) is None
    assert ingest.parse_title_line(line_list) is None


def test_ingest_jsonl_file_writes_ai_title_to_session_meta(tmp_path):
    p = tmp_path / "sess.jsonl"
    lines = [
        LINE,   # обычная usage-строка из фикстуры выше в файле
        json.dumps({"type": "ai-title", "sessionId": "sess-1",
                   "aiTitle": "Тестовая сессия"}),
    ]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    conn = store.connect(tmp_path / "u.sqlite")
    inserted, skipped = ingest.ingest_jsonl_file(conn, p)
    assert inserted == 1
    assert skipped == 0    # строка заголовка — не мусор, не считается пропущенной
    got = store.get_session_title(conn, "sess-1")
    assert got["title"] == "Тестовая сессия"
    assert got["title_source"] == "ai-title"


def test_ingest_ai_title_not_overwritten_by_later_last_prompt(tmp_path):
    p = tmp_path / "sess.jsonl"
    lines = [
        json.dumps({"type": "ai-title", "sessionId": "sess-1", "aiTitle": "Хорошее имя"}),
        json.dumps({"type": "last-prompt", "sessionId": "sess-1", "lastPrompt": "обрывок"}),
    ]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    conn = store.connect(tmp_path / "u.sqlite")
    ingest.ingest_jsonl_file(conn, p)
    got = store.get_session_title(conn, "sess-1")
    assert got["title"] == "Хорошее имя"
