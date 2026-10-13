"""Точка входа: python cc_usage/cli.py <команда>"""
import argparse
import datetime
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import json               # noqa: E402

import analyze          # noqa: E402
import fmt               # noqa: E402
import ingest            # noqa: E402
import limit_probe       # noqa: E402
import pricing           # noqa: E402
import render            # noqa: E402
import store              # noqa: E402
import usage_api          # noqa: E402

_REL_UNITS = {
    "h": 3600, "ч": 3600,
    "m": 60, "мин": 60,
    "d": 86400, "д": 86400,
    "w": 604800, "нед": 604800,
}
_REL_RE = re.compile(r"^(\d+)\s*(h|ч|m|мин|d|д|w|нед)$", re.IGNORECASE)


def parse_since(spec, now=None):
    """'2h' / '90m' / '3d' / '2w' / ISO-дата / ISO-время -> unix ts, локальное время.

    None -> None (граница не задана). Нераспознанный формат -> ValueError.
    """
    if spec is None:
        return None
    if now is None:
        now = time.time()
    spec = spec.strip()
    m = _REL_RE.match(spec)
    if m:
        n, unit = m.groups()
        return now - int(n) * _REL_UNITS[unit.lower()]
    try:
        dt = datetime.datetime.fromisoformat(spec)
    except ValueError:
        raise ValueError("не распознан формат времени: %r"
                         " (примеры: 2h, 90m, 3d, 2w, 2026-08-19, 2026-08-19T14:00)" % spec)
    return dt.timestamp()

MAX_SESSIONS = 20


def _n(x):
    return format(int(x or 0), ",").replace(",", " ")


def _when(ts):
    if not ts:
        return "—"
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def _money(x):
    return fmt.money(x)


def _limit_snapshot_line(snap):
    """- на момент отчёта (ts): 5h X%, 7d Y% — общая строка для explain_text,
    используется и в ветке "нет запросов в окне" (см. Fix 1, финальное
    ревью), и в основном разделе "## Лимит".

    Стадия B: цифра может
    прийти из двух источников (statusLine-хук или poll /api/oauth/usage) —
    какой сработал, помечаем суффиксом, чтобы не выдавать poll за то же
    самое "сейчас", что и живой fresh-замер интерактивной сессии.
    """
    suffix = " · через /api/oauth/usage" if snap.get("source") == "usage_api" else ""
    if not snap.get("fresh", True):
        suffix += " (нет свежих замеров в окне — возможно, устарело)"
    return "- на момент отчёта (%s)%s: 5h %s, 7d %s" % (
        _when(snap["ts"]), suffix,
        ("%.1f%%" % snap["five_hour_pct"]) if snap["five_hour_pct"] is not None else "—",
        ("%.1f%%" % snap["seven_day_pct"]) if snap["seven_day_pct"] is not None else "—")


def _limit_events_lines(conn, since_ts, until_ts):
    """Блок «События» для explain_text — по ОБОИМ полям лимита.

    Раньше блок был продублирован в двух ветках explain_text и оба раза
    брал только five_hour_pct по умолчанию, так что события недельного
    лимита не показывались вообще (финальное ревью limit-events).
    Пустой список, если событий нет ни по одному полю.
    """
    events = []
    for field, label in (("five_hour_pct", "5h"), ("seven_day_pct", "7d")):
        for ev in analyze.limit_events(conn, since=since_ts, until=until_ts,
                                       pct_field=field):
            events.append((label, ev))
    if not events:
        return []

    lines = ["События (рост % лимита, оценка по доле токенов):"]
    for label, ev in sorted(events, key=lambda p: -p[1]["d_pct"])[:10]:
        if ev["kind"] == "attributed":
            who = "сессия «%s» +%.1f пп" % (ev["shares"][0]["name"],
                                            ev["shares"][0]["pct"])
        elif ev["kind"] == "elsewhere":
            who = "расход не с этой машины (квота общая на аккаунт)"
        elif ev["kind"] == "unresolved":
            who = "локальная активность рядом, но не в интервале — не атрибутировано"
        elif ev["kind"] == "background":
            who = "только фоновые агенты"
        else:
            who = "объяснения нет"
        lines.append("- %s %s +%.1f пп за %d c, кандидатов %d — %s"
                     % (_when(ev["t1"]), label, ev["d_pct"], int(ev["width_s"]),
                        ev["n_candidates"], who))
    return lines


def explain_text(conn, session=None, since=None, until=None):
    """Сжатая сводка для модели, выстроенная вокруг диагнозов. Не выгрузка — сводка.

    since/until — строки в формате parse_since (2h, 90m, 3d, ISO-дата/время)
    либо None. Парсинг внутри функции, а не в cmd_explain: так explain_text
    остаётся вызываемой напрямую со строковыми окнами, без похода через CLI.
    """
    since_ts = parse_since(since) if since is not None else None
    until_ts = parse_since(until) if until is not None else None
    o = analyze.overview(conn, since=since_ts, until=until_ts)
    if not o["totals"]["n_requests"]:
        total_n = conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
        msg = ("В выбранном окне нет запросов. Расширь --since/--until"
              " или убери их, чтобы увидеть весь период." if total_n
              else "Нет данных: база пуста. Запусти сбор командой collect.")
        # AC1 не требует наличия requests в окне — только хотя бы одного
        # замера ratelimits (см. Fix 1, финальное ревью).
        limit = analyze.limit_summary(conn, since=since_ts, until=until_ts)
        if limit["snapshot"] is not None:
            lines = [msg, "", "## Лимит", _limit_snapshot_line(limit["snapshot"])]
            lines.extend(_limit_events_lines(conn, since_ts, until_ts))
            return "\n".join(lines)
        return msg

    t, cost = o["totals"], o["cost"]
    lines = [
        "# Расход Claude Code",
        "",
        "Период: %s — %s, запросов: %s, итого: %s (оценка по прайсу на %s)" % (
            _when(o["period"]["first_ts"]), _when(o["period"]["last_ts"]),
            _n(t["n_requests"]), _money(cost["total"]), pricing.PRICES_AS_OF),
        "",
        "## Куда ушли деньги",
    ]
    cat = cost["by_category"]
    for label, key in (("чтение кэша", "cache_read"), ("запись кэша", "cache_write"),
                       ("output", "output"), ("input", "input")):
        share = (cat[key] / cost["total"]) if cost["total"] else 0.0
        lines.append("- %s: %s (%.1f%%)" % (label, _money(cat[key]), 100 * share))
    if cost["unpriced_n"]:
        lines.append("- не учтено: %d запросов на моделях вне прайса (%s)"
                    % (cost["unpriced_n"], ", ".join(cost["unpriced_models"])))

    lines += ["", "## По моделям"]
    for m in o["cost_by_model"][:10]:
        if m["cost"] is None:
            continue
        lines.append("- %s: %s (%.1f%% денег, %.1f%% запросов)" % (
            m["model"], _money(m["cost"]),
            100 * (m["cost"] / cost["total"]) if cost["total"] else 0,
            100 * m["share_requests"]))

    lines += ["", "## Что можно исправить"]
    if not o["diagnoses"]:
        lines.append("- существенных перерасходов не найдено")
    for d in o["diagnoses"]:
        avoidable = (_money(d["avoidable_usd"]) if d["avoidable_usd"] is not None
                    else "не потеря")
        lines.append("- %s: факт %s, устранимо ~%s" % (d["title"], _money(d["fact_usd"]), avoidable))
        for w in d["where"][:3]:
            lines.append("  - %s: %s" % (w["name"], _money(w["cost"])))
        lines.append("  что делать: %s" % d["what_to_do"])
    for insuff in o["diagnoses_insufficient"]:
        lines.append("- недостаточно данных для диагноза «%s»: %s" % (insuff["id"], insuff["reason"]))

    if session:
        sid = analyze.resolve_session(conn, session)
        ramp = analyze.session_ramp(conn, sid)
        cost = analyze.session_cost(conn, sid)
        sess_row = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (sid,)).fetchone()
        meta_row = conn.execute("SELECT * FROM session_meta WHERE session_id = ?", (sid,)).fetchone()
        name, _source = analyze.session_display_name(
            dict(sess_row) if sess_row else {"session_id": sid, "project": None, "first_ts": None},
            dict(meta_row) if meta_row else None)
        lines += ["", "## Разгон сессии «%s»" % name,
                  "- итого по сессии: %s" % _money(cost) if cost is not None else "- итого по сессии: не оценено",
                  "- стоимость входа: %s" % _n(ramp["entry_cost"]),
                  "- за первые 5 минут: %s (запросов %d)" % (_n(ramp["window_total"]), ramp["n_requests"]),
                  "- субагентов: %d, ими потрачено: %s" % (ramp["n_subagents"], _n(ramp["subagent_tokens"]))]

    lines += ["", "## Самые дорогие сессии"]
    for s in o["sessions_by_cost"][:MAX_SESSIONS]:
        lines.append("- %s (%s): %s, запросов %d" % (
            s["name"], s["project"] or "—",
            _money(s["cost"]) if s["cost"] is not None else "—", s["n_requests"]))

    lines += ["", "## Лимит"]
    limit = analyze.limit_summary(conn, since=since_ts, until=until_ts)
    snap = limit["snapshot"]
    if snap is None:
        lines.append("- данных нет: statusLine-хук не настроен или ещё не сработал"
                     " (см. cc_usage/README.md, «Statusline на новой машине»)")
    else:
        lines.append(_limit_snapshot_line(snap))
        for key, human in (("five_hour", "5h"), ("seven_day", "7d")):
            info = limit[key]
            if info["weights"]["confidence"] != "medium":
                # Не "разбивки нет" — колонка "% лимита" в таблице сессий не
                # гейтится по confidence и есть всегда. Мало наблюдений
                # именно для уверенного указания главного потребителя
                # (Fix 2, финальное ревью).
                lines.append("- %s: наблюдений мало, назвать главного потребителя лимита нельзя" % human)
            elif info["diagnosis"]:
                d = info["diagnosis"]
                top = d["where"][0]
                # Величина скачка и доля лидера в нём — разные числа: при
                # нескольких кандидатах внутри события доля меньше скачка.
                lines.append("- %s: самый резкий одиночный скачок +%.1f пп,"
                             " из них на сессию «%s» ~%.1f пп; что делать: %s" % (
                                 human, d["fact_pct"], top["name"], top["pct"],
                                 d["what_to_do"]))
            else:
                lines.append("- %s: скачков лимита в этом окне нет" % human)

    lines.extend(_limit_events_lines(conn, since_ts, until_ts))

    return "\n".join(lines)


def _open(args):
    return store.connect(args.db or store.default_db_path())


def cmd_collect(args):
    conn = _open(args)
    cfg = store.config_dir()
    res = ingest.collect(conn, cfg / "projects", cfg / "cc-usage")
    print("файлов %(files)d, новых запросов %(inserted)d, пропущено %(skipped)d,"
          " замеров лимита %(ratelimits)d, замеров мода %(limit_samples)d,"
          " сессий %(sessions)d, за %(seconds)ss"
          % res)
    return 0


def cmd_explain(args):
    since = args.since or (("%dd" % args.days) if args.days else None)
    print(explain_text(_open(args), session=args.session, since=since, until=args.until))
    return 0


def cmd_report(args):
    conn = _open(args)
    cfg = store.config_dir()
    ingest.collect(conn, cfg / "projects", cfg / "cc-usage")   # ленивый догон
    out = Path(args.out) if args.out else cfg / "cc-usage" / "report.html"
    since = parse_since(args.since or (("%dd" % args.days) if args.days else None))
    until = parse_since(args.until)
    session_id = analyze.resolve_session(conn, args.session) if args.session else None
    render.render_report(conn, out, since=since, until=until,
                         session_id=session_id, debug=args.debug)
    print("отчёт: %s" % out)
    return 0


def cmd_export(args):
    from export import export_bundle
    out = export_bundle(_open(args), Path(args.out), days=args.days,
                        anonymize=not args.no_anonymize)
    print("экспорт: %s" % out)
    return 0


def cmd_import(args):
    from export import import_bundle
    n = import_bundle(_open(args), Path(args.file))
    print("импортировано записей: %d" % n)
    return 0


def cmd_backfill_titles(args):
    conn = _open(args)
    cfg = store.config_dir()
    res = ingest.backfill_titles(conn, cfg / "projects")
    print("файлов %(files)d, заголовков найдено %(titles)d" % res)
    return 0


PROBE_SESSION = "cc-probe"
PROBE_MAX_SESSION_AGE = 7 * 86400  # неделя: рост контекста от пингов линеен
                                    # и мал (~44 токена/пинг, замерено на
                                    # проде Павла) — окно (200k) заполнится
                                    # только через месяц с лишним
PROBE_STARTUP_WAIT = 15         # сек на загрузку claude в новой сессии


def probe_cwd():
    """Отдельный каталог для сессии зонда.

    Отдельный по двум причинам: его собственные ping-и отличаются от работы
    человека по полю cwd (иначе гейт по активности видел бы свой же
    предыдущий ping и запускал себя бесконечно), и в контекст сессии не
    попадают CLAUDE.md со скиллами рабочего проекта.
    """
    return store.config_dir() / "cc-usage" / "probe-cwd"


def cmd_probe_limit(args):
    if not limit_probe.tmux_available():
        print("probe-limit: нужен tmux — сессия зонда живёт в нём,"
              " чтобы переживать запуски из cron")
        return 1

    outdir = store.config_dir() / "cc-usage"
    outdir.mkdir(parents=True, exist_ok=True)
    cwd = probe_cwd()
    cwd.mkdir(parents=True, exist_ok=True)

    if not limit_probe.acquire_lock(str(outdir), max_age=args.min_interval):
        print("probe-limit: предыдущий запуск ещё идёт (или завис) — пропуск")
        return 0
    try:
        conn = _open(args)
        now = time.time()
        stored = store.get_meta(conn, "last_probe_ts")
        ok, reason = limit_probe.should_probe(
            now=now,
            last_probe_ts=float(stored) if stored else None,
            last_request_ts=store.last_request_ts(conn, exclude_cwd=cwd),
            min_interval=args.min_interval,
            max_interval=args.max_interval,
            if_active=args.if_active,
        )
        if not ok:
            print("probe-limit: пропуск — %s" % reason)
            return 0

        started, why = limit_probe.ensure_session(
            PROBE_SESSION, str(cwd), args.model,
            max_age=PROBE_MAX_SESSION_AGE, now=now)
        if started:
            time.sleep(PROBE_STARTUP_WAIT)
        # Перед КАЖДЫМ ping, а не только при создании сессии: сессия могла
        # застрять на первичном экране и в прошлый раз. Ping уходит вслепую,
        # и Enter ответил бы за пользователя на любой открытый диалог —
        # подтвердил бы доверие к каталогу, выбрал тему и так далее.
        pane = limit_probe.capture_pane(PROBE_SESSION)
        if not limit_probe.session_ready(pane):
            print("probe-limit: сессия не готова — на экране не видно"
                  " приглашения ввода, ping не отправлен (трат нет)."
                  " Разобраться: tmux attach -t %s\n%s"
                  % (PROBE_SESSION, limit_probe.pane_tail(pane)))
            return 1
        sent = limit_probe.send_ping(PROBE_SESSION)
        store.set_meta(conn, "last_probe_ts", now)
    finally:
        limit_probe.release_lock(str(outdir))
    print("probe-limit: %s — %s (сессия: %s)"
          % ("ping отправлен" if sent else "ping не ушёл", reason, why))
    return 0 if sent else 1


USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CREDENTIALS_FILENAME = ".credentials.json"


def _read_credentials_text():
    path = store.config_dir() / CREDENTIALS_FILENAME
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def _pct_str(x):
    return ("%.1f%%" % x) if x is not None else "—"


def cmd_poll_limits(args):
    conn = _open(args)
    now = time.time()
    stored = store.get_meta(conn, "last_usage_poll_ts")
    ok, reason = usage_api.should_poll(
        now=now, last_ts=float(stored) if stored else None,
        min_interval=args.min_interval)
    if not ok:
        print("poll-limits: пропуск — %s" % reason)
        return 0

    token, why = usage_api.read_token(_read_credentials_text(), now_ms=int(now * 1000))
    if token is None:
        print("poll-limits: %s" % why)
        return 0

    headers = usage_api.build_headers(token, usage_api.claude_version())
    status, body = usage_api.fetch(USAGE_URL, headers, timeout=args.timeout)

    if status == 200:
        try:
            payload = json.loads(body)
        except ValueError:
            print("poll-limits: не удалось разобрать ответ (не JSON)")
            return 0
        row = usage_api.parse_usage(payload, now)
        store.insert_usage_snapshots(conn, [row])
        store.set_meta(conn, "last_usage_poll_ts", now)
        print("poll-limits: замер сохранён (5h %s, 7d %s)"
              % (_pct_str(row["five_hour_pct"]), _pct_str(row["seven_day_pct"])))
        return 0
    if status == 401:
        print("poll-limits: токен не принят (401) — нужен вход в Claude Code")
        return 0
    if status == 429:
        print("poll-limits: эндпоинт ограничил частоту (429) — следующий такт по расписанию")
        return 0
    print("poll-limits: неожиданный ответ (код %d)" % status)
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="cc-usage",
                                description="Мониторинг расхода токенов Claude Code")
    p.add_argument("--db", help="путь к файлу базы (по умолчанию ~/.claude/cc-usage/usage.sqlite)")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("collect", help="собрать данные")
    c.set_defaults(func=cmd_collect)

    e = sub.add_parser("explain", help="сводка для модели")
    e.add_argument("--session")
    e.add_argument("--since", help="2h, 90m, 3d, 2w, ISO-дата/время")
    e.add_argument("--until")
    e.add_argument("--days", type=int, help="устаревший синоним --since Nd")
    e.set_defaults(func=cmd_explain)

    r = sub.add_parser("report", help="HTML-отчёт")
    r.add_argument("--session")
    r.add_argument("--since", help="2h, 90m, 3d, 2w, ISO-дата/время")
    r.add_argument("--until")
    r.add_argument("--days", type=int, help="устаревший синоним --since Nd")
    r.add_argument("--out")
    r.add_argument("--debug", action="store_true", help="показать отладочные разделы")
    r.set_defaults(func=cmd_report)

    x = sub.add_parser("export", help="выгрузка для передачи")
    x.add_argument("--out", required=True)
    x.add_argument("--days", type=int)
    x.add_argument("--no-anonymize", action="store_true",
                   help="оставить имена проектов, веток и заголовки/имена сессий как есть")
    x.set_defaults(func=cmd_export)

    i = sub.add_parser("import", help="принять чужую выгрузку")
    i.add_argument("file")
    i.set_defaults(func=cmd_import)

    bt = sub.add_parser("backfill-titles",
                        help="разовый проход по JSONL за именами сессий")
    bt.set_defaults(func=cmd_backfill_titles)

    pl = sub.add_parser("probe-limit",
                        help="ping в долгоживущую сессию claude ради fresh-замера"
                             " лимита (для cron, нужен tmux)")
    pl.add_argument("--model", default="claude-haiku-4-5-20251001",
                    help="модель для зонда — самая дешёвая по умолчанию")
    pl.add_argument("--if-active", action="store_true",
                    help="слать ping только если с прошлого зонда была работа;"
                         " в простое лимит не двигается и мерить нечего")
    pl.add_argument("--min-interval", type=int,
                    default=limit_probe.DEFAULT_MIN_INTERVAL,
                    help="не чаще одного ping в N секунд (по умолчанию 900)")
    pl.add_argument("--max-interval", type=int,
                    default=limit_probe.DEFAULT_MAX_INTERVAL,
                    help="контрольный замер не реже раза в N секунд — расход"
                         " бывает и по каналу, которого нет в транскриптах")
    pl.set_defaults(func=cmd_probe_limit)

    ul = sub.add_parser("poll-limits",
                        help="замер лимита через GET /api/oauth/usage —"
                             " бесплатно, без инференс-запроса (для крона)")
    ul.add_argument("--min-interval", type=int, default=300,
                    help="не чаще одного запроса в N секунд (по умолчанию 300)")
    ul.add_argument("--timeout", type=int, default=10,
                    help="таймаут HTTP-запроса в секундах (по умолчанию 10)")
    ul.set_defaults(func=cmd_poll_limits)

    args = p.parse_args(argv)
    try:
        return args.func(args)
    except ValueError as e:
        print(str(e))
        return 2


if __name__ == "__main__":
    sys.exit(main())
