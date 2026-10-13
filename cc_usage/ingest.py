"""Разбор логов Claude Code и инкрементальный сбор."""
import datetime
import datetime as _dt
import json
import os
import time
from pathlib import Path

import fmt
import store


def _ts(value):
    """ISO-8601 с Z -> unix UTC. Возвращает None, если время нечитаемо."""
    if not value:
        return None
    try:
        dt = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.timestamp()


def is_subagent_path(path):
    return "subagents" in Path(path).parts


def project_from_path(path):
    """Каталог проекта: .../projects/<project>/<session>.jsonl"""
    parts = Path(path).parts
    if "projects" in parts:
        i = parts.index("projects")
        if i + 1 < len(parts):
            return parts[i + 1]
    return None


def parse_usage_line(line, source_path=None):
    """Строка JSONL -> строка для requests, либо None.

    Никогда не бросает исключение: битая строка в живом файле — норма.
    """
    if not line or '"usage"' not in line:
        return None
    try:
        d = json.loads(line)
    except (ValueError, TypeError):
        return None
    if not isinstance(d, dict):
        return None
    msg = d.get("message") or {}
    usage = msg.get("usage") or {}
    if not usage:
        return None
    ts = _ts(d.get("timestamp"))
    if ts is None:
        return None
    cc = usage.get("cache_creation") or {}
    details = usage.get("output_tokens_details") or {}
    sidechain = bool(d.get("isSidechain"))
    agent_file = None
    if source_path is not None and is_subagent_path(source_path):
        sidechain = True
        agent_file = Path(source_path).name
    return {
        "request_id": d.get("requestId"),
        "message_id": msg.get("id"),
        "session_id": d.get("sessionId"),
        "ts": ts,
        "model": msg.get("model"),
        "effort": d.get("effort"),
        "service_tier": usage.get("service_tier"),
        "input": usage.get("input_tokens") or 0,
        "output": usage.get("output_tokens") or 0,
        "thinking": details.get("thinking_tokens") or 0,
        "cache_read": usage.get("cache_read_input_tokens") or 0,
        "cache_write": usage.get("cache_creation_input_tokens") or 0,
        "cache_1h": cc.get("ephemeral_1h_input_tokens") or 0,
        "cache_5m": cc.get("ephemeral_5m_input_tokens") or 0,
        "is_sidechain": 1 if sidechain else 0,
        "agent_file": agent_file,
        "project": project_from_path(source_path) if source_path else None,
        "cwd": d.get("cwd"),
        "git_branch": d.get("gitBranch"),
        "cc_version": d.get("version"),
    }


TITLE_MAX_LEN = 80


def parse_title_line(line):
    """ai-title/last-prompt строка -> (session_id, title, source) либо None.

    Никогда не бросает исключение: битая строка в живом файле — норма.
    """
    if not line:
        return None
    try:
        d = json.loads(line)
    except (ValueError, TypeError):
        return None
    if not isinstance(d, dict):
        return None
    session_id = d.get("sessionId")
    if not session_id:
        return None
    t = d.get("type")
    if t == "ai-title":
        value = d.get("aiTitle")
        if not value or not isinstance(value, str):
            return None
        return session_id, " ".join(value.split()), "ai-title"
    if t == "last-prompt":
        value = d.get("lastPrompt")
        if not value or not isinstance(value, str):
            return None
        return session_id, fmt.title(value, TITLE_MAX_LEN), "last-prompt"
    return None


def read_new_lines(path, offset):
    """Полные строки начиная с offset и новый offset.

    Работаем в байтах, а не в символах: offset должен быть устойчив
    к докармливанию файла и одинаков на Windows и Linux.
    """
    path = Path(path)
    size = path.stat().st_size
    if size < offset:      # файл переписан
        offset = 0
    if size == offset:
        return [], offset
    with open(path, "rb") as fh:
        fh.seek(offset)
        blob = fh.read(size - offset)
    cut = blob.rfind(b"\n")
    if cut == -1:
        return [], offset          # ни одной полной строки
    complete = blob[: cut + 1]
    text = complete.decode("utf-8", errors="replace")
    lines = [ln for ln in text.split("\n") if ln.strip()]
    return lines, offset + len(complete)


def ingest_jsonl_file(conn, path):
    """Дочитать один файл. Возвращает (вставлено, пропущено)."""
    path = Path(path)
    st = store.get_file_state(conn, path)
    offset = st["offset"] if st else 0
    lines, new_offset = read_new_lines(path, offset)
    rows, titles, skipped = [], [], 0
    for line in lines:
        row = parse_usage_line(line, source_path=path)
        if row is not None:
            if not row.get("request_id") and not row.get("message_id"):
                skipped += 1      # без ключа дедупликации строка бесполезна
            else:
                rows.append(row)
            continue
        title_row = parse_title_line(line)
        if title_row is not None:
            titles.append(title_row)
        else:
            skipped += 1
    inserted = store.insert_requests(conn, rows)
    now = time.time()
    for session_id, title, source in titles:
        store.set_session_title(conn, session_id, title, source, now)
    stat = path.stat()
    store.set_file_state(conn, path, stat.st_size, stat.st_mtime, new_offset)
    return inserted, skipped


def ingest_all_jsonl(conn, projects_dir):
    """Обойти все сессии и субагентов."""
    projects_dir = Path(projects_dir)
    total = {"files": 0, "inserted": 0, "skipped": 0}
    for path in sorted(projects_dir.rglob("*.jsonl")):
        try:
            ins, skip = ingest_jsonl_file(conn, path)
        except OSError:
            continue          # файл исчез или занят — вернёмся следующим заходом
        total["files"] += 1
        total["inserted"] += ins
        total["skipped"] += skip
    return total


def backfill_titles(conn, projects_dir):
    """Полный проход по JSONL только за строками заголовков сессий.

    Не трогает requests и не сдвигает byte-offset файлов — идемпотентна,
    можно гонять повторно. Ограничение: находит имена только для сессий,
    чей JSONL ещё не удалён ретенцией (cleanupPeriodDays).
    """
    projects_dir = Path(projects_dir)
    if not projects_dir.exists():
        return {"files": 0, "titles": 0}
    files, found = 0, 0
    now = time.time()
    for path in sorted(projects_dir.rglob("*.jsonl")):
        try:
            blob = path.read_bytes()
        except OSError:
            continue
        files += 1
        text = blob.decode("utf-8", errors="replace")
        for line in text.split("\n"):
            line = line.strip()
            if not line:
                continue
            title_row = parse_title_line(line)
            if title_row is not None:
                session_id, title, source = title_row
                store.set_session_title(conn, session_id, title, source, now)
                found += 1
    return {"files": files, "titles": found}


def ratelimits_filename(ts):
    d = _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc)
    return "ratelimits-%04d%02d%02d.ndjson" % (d.year, d.month, d.day)


def parse_ratelimit_line(line):
    if not line or not line.strip():
        return None
    try:
        d = json.loads(line)
    except (ValueError, TypeError):
        return None
    if not isinstance(d, dict) or d.get("ts") is None:
        return None
    return {
        "ts": d.get("ts"),
        "session_id": d.get("session"),
        "five_hour_pct": d.get("five_hour_pct"),
        "five_hour_reset": d.get("five_hour_reset"),
        "seven_day_pct": d.get("seven_day_pct"),
        "seven_day_reset": d.get("seven_day_reset"),
        "ctx_pct": d.get("ctx_pct"),
        "ctx_in": d.get("ctx_in"),
        "cost_usd": d.get("cost_usd"),
        "fresh": d.get("fresh"),
        "source": d.get("source"),
    }


def ingest_ratelimits(conn, outdir):
    """Импортировать замеры лимитов. Вчерашние файлы удаляются."""
    import time
    outdir = Path(outdir)
    today = ratelimits_filename(time.time())
    res = {"files": 0, "inserted": 0, "removed": 0}
    for path in sorted(outdir.glob("ratelimits-*.ndjson")):
        st = store.get_file_state(conn, path)
        offset = st["offset"] if st else 0
        try:
            lines, new_offset = read_new_lines(path, offset)
        except OSError:
            continue
        rows = [r for r in (parse_ratelimit_line(ln) for ln in lines) if r]
        res["inserted"] += store.insert_ratelimits(conn, rows)
        res["files"] += 1
        stat = path.stat()
        store.set_file_state(conn, path, stat.st_size, stat.st_mtime, new_offset)
        if path.name != today:
            try:
                os.remove(path)
                res["removed"] += 1
            except OSError:
                pass     # занят — удалится следующим заходом
    return res


LIMIT_SAMPLES_DIR = "limit-samples"
LIMIT_SAMPLES_TTL = 24 * 3600
"""Сек: файл замеров мода, не менявшийся дольше этого, считается файлом
закончившейся сессии и после полного прочтения удаляется."""


def ingest_limit_samples(conn, outdir):
    """Импортировать замеры лимита от мода: limit-samples/<session>.ndjson.

    Мод переписывает файл целиком, но тот только растёт (префикс прежний),
    поэтому работает тот же byte-offset, что и у ratelimits. Файл удаляется,
    только если прочитан до конца и не менялся дольше LIMIT_SAMPLES_TTL.
    """
    folder = Path(outdir) / LIMIT_SAMPLES_DIR
    res = {"files": 0, "inserted": 0, "removed": 0}
    if not folder.is_dir():
        return res
    now = time.time()
    for path in sorted(folder.glob("*.ndjson")):
        st = store.get_file_state(conn, path)
        offset = st["offset"] if st else 0
        try:
            lines, new_offset = read_new_lines(path, offset)
            stat = path.stat()
        except OSError:
            continue
        rows = [r for r in (parse_ratelimit_line(ln) for ln in lines) if r]
        res["inserted"] += store.insert_ratelimits(conn, rows)
        res["files"] += 1
        store.set_file_state(conn, path, stat.st_size, stat.st_mtime, new_offset)
        if new_offset == stat.st_size and now - stat.st_mtime > LIMIT_SAMPLES_TTL:
            try:
                os.remove(path)
                res["removed"] += 1
            except OSError:
                pass     # занят — удалится следующим заходом
    return res


LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_GENERATIONS = 3


def log_line(outdir, text):
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    path = outdir / "collect.log"
    try:
        if path.exists() and path.stat().st_size > LOG_MAX_BYTES:
            for i in range(LOG_GENERATIONS - 2, 0, -1):
                src = outdir / ("collect.log.%d" % i)
                if src.exists():
                    src.replace(outdir / ("collect.log.%d" % (i + 1)))
            path.replace(outdir / "collect.log.1")
        stamp = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("%s  %s\n" % (stamp, text))
    except OSError:
        pass      # лог не должен ронять сбор


def collect(conn, projects_dir, outdir):
    """Полный проход: JSONL, ndjson лимитов, пересчёт сводки."""
    started = time.time()
    jsonl = ingest_all_jsonl(conn, projects_dir)
    rl = ingest_ratelimits(conn, outdir)
    ls = ingest_limit_samples(conn, outdir)
    sessions = store.rebuild_sessions(conn)
    res = {
        "files": jsonl["files"],
        "inserted": jsonl["inserted"],
        "skipped": jsonl["skipped"],
        "ratelimits": rl["inserted"],
        "limit_samples": ls["inserted"],
        "sessions": sessions,
        "seconds": round(time.time() - started, 2),
    }
    log_line(outdir, "collect: файлов %(files)d, новых запросов %(inserted)d, "
                     "пропущено %(skipped)d, замеров лимита %(ratelimits)d, "
                     "замеров мода %(limit_samples)d, "
                     "сессий %(sessions)d, за %(seconds)ss" % res)
    return res
