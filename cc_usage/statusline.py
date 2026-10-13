#!/usr/bin/env python3
"""Statusline-хук Claude Code.

Делает три вещи за один вызов:
  1. копит историю rate_limits/контекста в ndjson (для анализа расхода лимита);
  2. пишет abtop-rate-limits.json в формате abtop (его собственный хук падает на python3);
  3. печатает строку статуса.

Только stdlib. Одинаково работает на Windows и Linux.
Подключение — settings.json:
    "statusLine": {"type": "command",
                   "command": "python /path/to/cc-usage/cc_usage/statusline.py",
                   "refreshInterval": 60}
refreshInterval обязателен: без него хук вызывается только по событиям,
а простой сессии — это как раз отсутствие событий.
"""
import sys, os, json, time

# Windows-консоль может быть в cp1251/cp866 — статусбар ждёт UTF-8
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

MIN_INTERVAL = 15   # сек: если значения не изменились, пишем не чаще
STATE_TTL = 86400   # сек: сколько держим состояние сессии в state.json


def cfg_dir():
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")


def write_atomic(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def _state_path(outdir, sid):
    """Файл состояния своей сессии — не общий на все сессии.

    Общий state.json читался и переписывался целиком (read-modify-write без
    блокировки); два параллельных хука разных сессий могли затереть
    обновление друг друга (fix-wave-2, находка 6). Отдельный файл на
    сессию устраняет гонку структурно: чужая параллельная запись физически
    не может задеть файл этой сессии.
    """
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in sid)[:200] or "unknown"
    return os.path.join(outdir, "state-%s.json" % safe)


def _prune_stale_state(outdir, now):
    """Удалить файлы состояния сессий, не писавших дольше STATE_TTL.

    Лучшее усилие: гонка при удалении (два хука подчищают одновременно)
    не опасна — второй просто не найдёт уже удалённый файл.
    """
    try:
        names = os.listdir(outdir)
    except OSError:
        return
    for name in names:
        if not (name.startswith("state-") and name.endswith(".json")):
            continue
        path = os.path.join(outdir, name)
        try:
            if now - os.path.getmtime(path) >= STATE_TTL:
                os.remove(path)
        except OSError:
            pass


def _daily_name(ts):
    """ratelimits-YYYYMMDD.ndjson по UTC.

    Дублирует ingest.ratelimits_filename намеренно: statusline автономен
    и не импортирует пакет, чтобы не задерживать статусную строку.
    """
    import datetime
    d = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)
    return "ratelimits-%04d%02d%02d.ndjson" % (d.year, d.month, d.day)


def main():
    d = json.loads(sys.stdin.read())

    rl = d.get("rate_limits") or {}
    fh = rl.get("five_hour") or {}
    sd = rl.get("seven_day") or {}
    cw = d.get("context_window") or {}
    cu = cw.get("current_usage") or {}
    now = time.time()

    rec = {
        "ts": now,
        "session": d.get("session_id"),
        "model": (d.get("model") or {}).get("id"),
        "five_hour_pct": fh.get("used_percentage"),
        "five_hour_reset": fh.get("resets_at"),
        "seven_day_pct": sd.get("used_percentage"),
        "seven_day_reset": sd.get("resets_at"),
        "ctx_pct": cw.get("used_percentage"),
        "ctx_in": cw.get("total_input_tokens"),
        "cache_read": cu.get("cache_read_input_tokens"),
        "cache_create": cu.get("cache_creation_input_tokens"),
        "cost_usd": (d.get("cost") or {}).get("total_cost_usd"),
        "cwd": d.get("workspace", {}).get("current_dir") if isinstance(d.get("workspace"), dict) else None,
    }

    cdir = cfg_dir()
    outdir = os.path.join(cdir, "cc-usage")
    os.makedirs(outdir, exist_ok=True)

    # --- 2. совместимость с abtop: он читает этот файл, свой хук у него не работает ---
    if fh or sd:
        ab = {"source": "claude", "updated_at": int(now)}
        if fh:
            ab["five_hour"] = {"used_percentage": fh.get("used_percentage", 0), "resets_at": fh.get("resets_at", 0)}
        if sd:
            ab["seven_day"] = {"used_percentage": sd.get("used_percentage", 0), "resets_at": sd.get("resets_at", 0)}
        try:
            write_atomic(os.path.join(cdir, "abtop-rate-limits.json"), json.dumps(ab))
        except Exception:
            pass

    # --- 1. история: пишем при изменении значений либо не реже MIN_INTERVAL ---
    sig = [rec["five_hour_pct"], rec["seven_day_pct"], rec["ctx_in"]]
    sid = rec["session"] or "unknown"
    statef = _state_path(outdir, sid)
    try:
        with open(statef, encoding="utf-8") as f:
            prev = json.load(f)
    except Exception:
        prev = {}

    prev_cost = prev.get("cost")
    # cost_usd растёт только при реально оплаченном обращении к API и не
    # меняется от /clear, /rewind или границы компакта — в отличие от
    # ctx_in, который меняется в этих случаях тоже, без ответа API. Раньше
    # sig (только на ctx_in) принимал такой сброс контекста за признак
    # свежести — ровно тот артефакт застывшего значения, ради устранения
    # которого гейт вводился (fix-wave-2, находка 5). Используем cost_usd
    # как основной сигнал, когда есть с чем сравнить (начиная со второго
    # тика сессии); иначе — прежняя логика по sig.
    cost_known = rec["cost_usd"] is not None and prev_cost is not None
    cost_changed = cost_known and rec["cost_usd"] != prev_cost
    sig_changed = prev.get("sig") != sig
    rec["fresh"] = 1 if (cost_changed if cost_known else sig_changed) else 0
    changed = sig_changed or cost_changed
    write = not (not changed and now - prev.get("ts", 0) < MIN_INTERVAL)

    if write:
        with open(os.path.join(outdir, _daily_name(now)), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        try:
            write_atomic(statef, json.dumps({"sig": sig, "ts": now, "cost": rec["cost_usd"]}))
        except Exception:
            pass
        _prune_stale_state(outdir, now)

    # --- 3. собственно статусная строка ---
    parts = []
    m = (d.get("model") or {}).get("display_name")
    if m:
        parts.append(m)
    if rec["ctx_pct"] is not None:
        parts.append(f"ctx {rec['ctx_pct']:.0f}%")
    if rec["five_hour_pct"] is not None:
        parts.append(f"5h {rec['five_hour_pct']:.1f}%")
    if rec["seven_day_pct"] is not None:
        parts.append(f"7d {rec['seven_day_pct']:.1f}%")
    print(" | ".join(parts) or "…")


try:
    main()
except Exception:
    print("…")   # статусбар не должен ломаться ни при каких данных
