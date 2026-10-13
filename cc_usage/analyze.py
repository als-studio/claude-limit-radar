"""Аналитика: паузы, спайки, агрегаты."""
import bisect
import datetime
import statistics
import time

import pricing

SPIKE_ABS = 20000        # нижняя абсолютная граница записи
BASELINE_FACTOR = 3
BASELINE_MIN_N = 5       # меньше — база ненадёжна
ACTIVE_GAP_S = 60        # что считаем активной работой

COLD_CACHE_5M_S = 300        # TTL пятиминутного кэша
COLD_CACHE_1H_S = 3600       # TTL часового кэша
COLD_CACHE_DEFAULT_S = 3600  # консервативный TTL, когда разбивка предыдущего запроса нулевая


def compute_gaps(rows):
    """Добавить паузу с предыдущего запроса. Порядок — по времени."""
    rows = sorted(rows, key=lambda r: r["ts"])
    prev = None
    for r in rows:
        r["gap"] = None if prev is None else r["ts"] - prev["ts"]
        prev = r
    return rows


def session_baseline(rows):
    """Медиана записи в кэш при активной работе внутри одной сессии."""
    active = [r["cache_write"] for r in rows
              if r.get("gap") is not None and r["gap"] < ACTIVE_GAP_S]
    if len(active) < BASELINE_MIN_N:
        return None
    return statistics.median(active)


def spike_threshold(baseline):
    if baseline is None:
        return SPIKE_ABS
    return max(int(BASELINE_FACTOR * baseline), SPIKE_ABS)


def _prev_ttl_s(prev_row):
    """Время жизни кэша, которое ЗАПИСАЛ предыдущий запрос."""
    if (prev_row.get("cache_1h") or 0) > 0:
        return COLD_CACHE_1H_S
    if (prev_row.get("cache_5m") or 0) > 0:
        return COLD_CACHE_5M_S
    return COLD_CACHE_DEFAULT_S


def find_cold_cache(rows):
    """Запросы, перезалившие контекст после того, как кэш предыдущего запроса остыл.

    Порог паузы берётся из TTL, который записал ПРЕДЫДУЩИЙ запрос (5 или 60
    минут), а не фиксирован — старый детектор считал потерей нормальную
    работу с часовым кэшем при паузе всего в 6 минут.
    """
    baseline = session_baseline(rows)
    threshold = spike_threshold(baseline)
    out = []
    for prev, cur in zip(rows, rows[1:]):
        if cur.get("gap") is None:
            continue
        ttl = _prev_ttl_s(prev)
        if cur["gap"] > ttl and cur["cache_write"] > threshold:
            item = dict(cur)
            item["threshold"] = threshold
            item["baseline"] = baseline
            item["ttl_s"] = ttl
            out.append(item)
    return out


def session_rows(conn, session_id):
    cur = conn.execute(
        "SELECT * FROM requests WHERE session_id = ? ORDER BY ts", (session_id,))
    return compute_gaps([dict(r) for r in cur])


def cold_cache_summary(conn, since=None):
    """Остывший кэш по всем сессиям. since — unix-время нижней границы."""
    sql = "SELECT DISTINCT session_id FROM requests WHERE session_id IS NOT NULL"
    params = []
    if since is not None:
        sql += " AND ts >= ?"
        params.append(since)
    all_events, total = [], 0
    for (sid,) in conn.execute(sql, params):
        rows = session_rows(conn, sid)
        events = find_cold_cache(rows)
        if since is not None:
            rows_in_window = [r for r in rows if r["ts"] >= since]
            events_in_window = [e for e in events if e["ts"] >= since]
        else:
            rows_in_window = rows
            events_in_window = events
        total += sum(r["cache_write"] for r in rows_in_window)
        all_events.extend(events_in_window)
    wasted = sum(r["cache_write"] for r in all_events)
    return {
        "n_events": len(all_events),
        "wasted": wasted,
        "total_cache_write": total,
        "share": (wasted / total) if total else 0.0,
        "top": sorted(all_events, key=lambda r: -r["cache_write"])[:10],
    }


def session_ramp(conn, session_id, window_s=300):
    """Разгон в начале сессии: на что уходят первые минуты."""
    rows = session_rows(conn, session_id)
    if not rows:
        return {"entry_cost": 0, "window_total": 0, "n_requests": 0,
                "n_subagents": 0, "subagent_tokens": 0,
                "per_minute": [0] * (window_s // 60)}
    start = rows[0]["ts"]
    window = [r for r in rows if r["ts"] - start <= window_s]
    subs = [r for r in window if r["is_sidechain"]]
    per_minute = [0] * (window_s // 60)
    for r in window:
        idx = min(int((r["ts"] - start) // 60), len(per_minute) - 1)
        per_minute[idx] += r["cache_write"] + r["output"]
    return {
        "entry_cost": rows[0]["cache_write"],
        "window_total": sum(r["cache_write"] for r in window),
        "n_requests": len(window),
        "n_subagents": len(subs),
        "subagent_tokens": sum(r["cache_write"] for r in subs),
        "per_minute": per_minute,
    }


def session_cost(conn, session_id):
    """Итоговая стоимость сессии в $, None если ни для одной модели сессии нет цены."""
    sql = "SELECT model, %s FROM requests WHERE session_id = ? GROUP BY model" % _COST_FIELDS_SQL
    total = 0.0
    any_priced = False
    for row in conn.execute(sql, (session_id,)):
        d = dict(row)
        c = pricing.cost_of_row(_synthetic_row(d["model"], d))
        if c is None:
            continue
        any_priced = True
        total += c
    return total if any_priced else None


def _where_since(since):
    return (" WHERE ts >= ?", [since]) if since is not None else ("", [])


def cache_efficiency(conn, since=None):
    where, params = _where_since(since)
    row = conn.execute(
        "SELECT COALESCE(SUM(cache_read), 0), COALESCE(SUM(cache_write), 0)"
        " FROM requests" + where, params).fetchone()
    read, write = row[0], row[1]
    total = read + write
    return {"cache_read": read, "cache_write": write,
            "hit_ratio": (read / total) if total else 0.0}


# --- денежная агрегация -----------------------------------------------------

_COST_FIELDS_SQL = (
    "SUM(input) i, SUM(output) o, SUM(cache_read) cr,"
    " SUM(CASE WHEN cache_5m = 0 AND cache_1h = 0 THEN cache_write ELSE cache_5m END) c5m,"
    " SUM(CASE WHEN cache_5m = 0 AND cache_1h = 0 THEN 0 ELSE cache_1h END) c1h,"
    " COUNT(*) n"
)


def _window_where(since, until):
    clauses, params = [], []
    if since is not None:
        clauses.append("ts >= ?")
        params.append(since)
    if until is not None:
        clauses.append("ts < ?")
        params.append(until)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def _synthetic_row(model, agg):
    """Агрегат SQL-группы -> dict в формате, который понимает pricing.*.

    cache_write=0 намеренно: TTL-разбивка (c5m/c1h) уже скорректирована на
    уровне SQL (см. _COST_FIELDS_SQL) для старых записей без разбивки —
    повторное применение fallback в pricing.cost_components не нужно и не
    сработает при cache_write=0.
    """
    return {"model": model, "input": agg["i"], "output": agg["o"],
            "cache_read": agg["cr"], "cache_5m": agg["c5m"], "cache_1h": agg["c1h"],
            "cache_write": 0}


def cost_by_model(conn, since=None, until=None):
    """Стоимость и число запросов по каждому точному имени модели."""
    where, params = _window_where(since, until)
    sql = "SELECT model, %s FROM requests%s GROUP BY model" % (_COST_FIELDS_SQL, where)
    rows = [dict(r) for r in conn.execute(sql, params)]
    total_n = sum(r["n"] for r in rows) or 1
    out = []
    for r in rows:
        cost = pricing.cost_of_row(_synthetic_row(r["model"], r))
        out.append({"model": r["model"], "family": pricing.family(r["model"]),
                    "n": r["n"], "cost": cost, "share_requests": r["n"] / total_n})
    out.sort(key=lambda r: (r["cost"] is None, -(r["cost"] or 0)))
    return out


def cost_by_project(conn, since=None, until=None):
    """Стоимость по проектам (топ-20): группировка (project, model), как в
    cost_by_model — цена привязана к точной модели внутри каждого проекта.
    Неучтённые (вне прайса) подгруппы пропускаются, как и в cost_by_model.

    project — мангленное Claude Code имя каталога (нечитаемо), MAX(cwd)
    внутри той же группы даёт исходный путь для человекочитаемого "k" (см.
    readable_project_label и bug-log 2026-08-21).
    """
    where, params = _window_where(since, until)
    sql = ("SELECT project, MAX(cwd) AS cwd, model, %s FROM requests%s GROUP BY project, model"
          % (_COST_FIELDS_SQL, where))
    cost_by_proj = {}
    cwd_by_proj = {}
    for row in conn.execute(sql, params):
        d = dict(row)
        cwd_by_proj.setdefault(d["project"], d["cwd"])
        cost = pricing.cost_of_row(_synthetic_row(d["model"], d))
        if cost is None:
            continue
        cost_by_proj[d["project"]] = cost_by_proj.get(d["project"], 0.0) + cost
    out = [{"k": readable_project_label(cwd_by_proj.get(project), project), "cost": cost}
          for project, cost in cost_by_proj.items()]
    out.sort(key=lambda r: -r["cost"])
    return out[:20]


def spend_by_bucket(conn, since=None, until=None):
    """Расход в $ по корзинам времени, отдельно запись кэша (устранимое при
    частых паузах) и остальное (реальная работа: input/output/чтение кэша).
    Неучтённые модели не входят в сумму.
    """
    since_eff = since if since is not None else 0.0
    until_eff = until if until is not None else time.time()
    window_s = max(1.0, until_eff - since_eff)
    step, _label_fmt = bucket_step(window_s)
    sql = ("SELECT CAST((ts - ?) / ? AS INTEGER) AS bucket, model, %s"
          " FROM requests WHERE ts >= ? AND ts < ? GROUP BY bucket, model"
          % _COST_FIELDS_SQL)
    cur = conn.execute(sql, [since_eff, step, since_eff, until_eff])
    by_bucket = {}
    for row in cur:
        d = dict(row)
        comps = pricing.cost_components(_synthetic_row(d["model"], d))
        if comps is None:
            continue
        b = by_bucket.setdefault(d["bucket"], {"cache_write": 0.0, "other": 0.0})
        b["cache_write"] += comps["cache_write"]
        b["other"] += comps["input"] + comps["output"] + comps["cache_read"]
    n_buckets = int(window_s // step) + 1
    out = []
    for i in range(n_buckets):
        b = by_bucket.get(i, {"cache_write": 0.0, "other": 0.0})
        out.append({"ts": since_eff + i * step, "cache_write": b["cache_write"],
                    "other": b["other"], "cost": b["cache_write"] + b["other"]})
    return out


def limit_curve_for_buckets(conn, buckets, pct_field="five_hour_pct"):
    """Кривая процента лимита нарастающим итогом, выровненная по корзинам
    spend_by_bucket — для линии поверх графика "Расход по времени".

    Значение корзины — последний свежий замер на момент её начала (сам
    процент уже накопленный, отдельного суммирования не требует). None,
    если на момент начала корзины свежих замеров ещё не было — ряд не
    экстраполируется назад дальше первого известного замера. Замеры не
    ограничены окном отчёта нарочно: усечение по since обрубило бы начало
    кривой ровно там, где могла быть последняя известная точка ДО первой
    корзины.
    """
    marks = _fresh_marks(conn, pct_field=pct_field)
    ts_list = [m["ts"] for m in marks]
    pct_list = [m["pct"] for m in marks]
    out = []
    for b in buckets:
        i = bisect.bisect_right(ts_list, b["ts"])
        out.append(pct_list[i - 1] if i else None)
    return out


# --- имена сессий ------------------------------------------------------------

def readable_project_label(cwd, project):
    """project — мангленное Claude Code имя каталога (слэши -> дефисы,
    нечитаемо, см. bug-log). cwd хранит путь как есть — берём последний
    сегмент как более узнаваемое имя, если он похож на путь, а не на диск
    без пути (например "C:").
    """
    if cwd:
        leaf = cwd.replace("\\", "/").rstrip("/").split("/")[-1]
        if leaf and ":" not in leaf:
            return leaf
    return project


def session_display_name(session_row, meta_row):
    """Каскад: ai-title/last-prompt из session_meta -> cwd/project+время -> id[:8]."""
    if meta_row and meta_row.get("title"):
        return meta_row["title"], meta_row["title_source"]
    label = readable_project_label(session_row.get("cwd"), session_row.get("project"))
    first_ts = session_row.get("first_ts")
    if label:
        when = (datetime.datetime.fromtimestamp(first_ts).strftime("%Y-%m-%d %H:%M")
                if first_ts else "?")
        return "%s — %s" % (label, when), "project"
    sid = session_row.get("session_id") or ""
    return sid[:8], "id"


def resolve_session(conn, query):
    """--session: полный id, префикс id, либо подстрока имени (без регистра)."""
    row = conn.execute("SELECT session_id FROM sessions WHERE session_id = ?",
                       (query,)).fetchone()
    if row:
        return row[0]
    prefix_rows = conn.execute("SELECT session_id FROM sessions WHERE session_id LIKE ?",
                               (query + "%",)).fetchall()
    if len(prefix_rows) == 1:
        return prefix_rows[0][0]
    if len(prefix_rows) > 1:
        raise ValueError("неоднозначный префикс id сессии: %r (%d совпадений)"
                         % (query, len(prefix_rows)))
    name_rows = conn.execute(
        "SELECT sessions.session_id FROM sessions JOIN session_meta"
        " ON sessions.session_id = session_meta.session_id"
        " WHERE session_meta.title LIKE ? COLLATE NOCASE",
        ("%" + query + "%",)).fetchall()
    if len(name_rows) == 1:
        return name_rows[0][0]
    if len(name_rows) > 1:
        raise ValueError("неоднозначное имя сессии: %r (%d совпадений)"
                         % (query, len(name_rows)))
    raise ValueError("сессия не найдена: %r" % query)


# --- диагнозы -----------------------------------------------------------------

DIAGNOSIS_MIN_SHARE = 0.01

COLD_CACHE_ASSUMPTION = (
    "оценка устранимого = стоимость записи минус стоимость медианной активной "
    "записи в этой сессии, по цене того же запроса; часть контекста пришлось "
    "бы записать в любом случае"
)
EXPENSIVE_MODEL_ASSUMPTION = (
    "оценка устранимого = разница со стоимостью тех же токенов по цене %s; "
    "предполагает 'та же работа теми же токенами', что для сложных задач неверно"
    % pricing.CHEAPEST_CURRENT_MODEL
)


def _session_name_map(conn, session_ids):
    if not session_ids:
        return {}
    marks = ", ".join("?" for _ in session_ids)
    sess_rows = {r["session_id"]: dict(r) for r in conn.execute(
        "SELECT * FROM sessions WHERE session_id IN (%s)" % marks, list(session_ids))}
    meta_rows = {r["session_id"]: dict(r) for r in conn.execute(
        "SELECT * FROM session_meta WHERE session_id IN (%s)" % marks, list(session_ids))}
    out = {}
    for sid in session_ids:
        sess = sess_rows.get(sid, {"session_id": sid, "project": None, "first_ts": None})
        name, _source = session_display_name(sess, meta_rows.get(sid))
        out[sid] = name
    return out


def _where_entries(conn, cost_by_session):
    """[{session_id, name, cost}] отсортировано по cost убыв., топ-5."""
    names = _session_name_map(conn, list(cost_by_session))
    items = [{"session_id": sid, "name": names.get(sid, sid[:8]), "cost": cost}
            for sid, cost in cost_by_session.items()]
    items.sort(key=lambda x: -x["cost"])
    return items[:5]


def _diagnose_cold_cache(conn, since, until, total_spend):
    sql = "SELECT DISTINCT session_id FROM requests WHERE session_id IS NOT NULL"
    params = []
    if since is not None:
        sql += " AND ts >= ?"
        params.append(since)
    if until is not None:
        sql += " AND ts < ?"
        params.append(until)
    events = []
    for (sid,) in conn.execute(sql, params):
        rows = session_rows(conn, sid)      # полная история — TTL и база нужны целиком
        found = find_cold_cache(rows)
        if since is not None:
            found = [e for e in found if e["ts"] >= since]
        if until is not None:
            found = [e for e in found if e["ts"] < until]
        events.extend(found)
    if not events:
        return None, "нет событий остывшего кэша в окне"
    fact = 0.0
    avoidable = 0.0
    cost_by_session = {}
    for e in events:
        cost = pricing.cost_of_row(e)
        if cost is None:
            continue
        fact += cost
        comps = pricing.cost_components(e)
        write_tokens = e.get("cache_write") or 0
        baseline = e.get("baseline")
        if comps and write_tokens and baseline:
            avoidable_tokens = max(0, write_tokens - baseline)
            avoidable += comps["cache_write"] * (avoidable_tokens / write_tokens)
        cost_by_session[e["session_id"]] = cost_by_session.get(e["session_id"], 0.0) + cost
    if total_spend and fact / total_spend < DIAGNOSIS_MIN_SHARE:
        return None, "факт меньше 1% расхода окна"
    return {
        "id": "cold_cache",
        "title": "Остывший кэш после простоя",
        "fact_usd": fact,
        "avoidable_usd": avoidable,
        "confidence": "estimate",
        "where": _where_entries(conn, cost_by_session),
        "what_to_do": ("не бросать сессию на паузу дольше времени жизни кэша; "
                       "перед долгим перерывом закрывать сессию, а не оставлять открытой"),
        "assumption": COLD_CACHE_ASSUMPTION,
    }, None


def _diagnose_expensive_model(conn, since, until, total_spend):
    by_model = cost_by_model(conn, since=since, until=until)
    priced = [r for r in by_model if r["cost"] is not None]
    cheapest_price = pricing.price_for(pricing.CHEAPEST_CURRENT_MODEL)
    fact = 0.0
    avoidable = 0.0
    cost_by_session = {}
    where, params = _window_where(since, until)
    for r in priced:
        # Сравнение по разрешённой цене, а не по строке имени модели: Haiku
        # дешевле Sonnet-5 на каждой компоненте цены, но не совпадает с ним
        # по имени, а датированный алиас Sonnet-5 совпадает по цене, но не
        # по строке — оба случая ловятся сравнением price_for() (см. Task 8
        # review, Fix 3).
        if pricing.price_for(r["model"]) == cheapest_price:
            continue     # уже самая дешёвая — сравнивать не с чем
        sql = ("SELECT session_id, %s FROM requests%s%s model = ? GROUP BY session_id"
              % (_COST_FIELDS_SQL, where, " AND" if where else " WHERE"))
        for row in conn.execute(sql, params + [r["model"]]):
            d = dict(row)
            real_cost = pricing.cost_of_row(_synthetic_row(r["model"], d))
            cheap_cost = pricing.cost_of_row(_synthetic_row(r["model"], d), price=cheapest_price)
            if real_cost is None or cheap_cost is None:
                continue
            delta = real_cost - cheap_cost
            if delta <= 0:
                continue     # реального перерасхода нет — не считаем ни в fact, ни в avoidable
            fact += real_cost
            avoidable += delta
            if d["session_id"]:
                cost_by_session[d["session_id"]] = cost_by_session.get(d["session_id"], 0.0) + real_cost
    if fact <= 0:
        return None, "нет запросов на моделях дороже %s" % pricing.CHEAPEST_CURRENT_MODEL
    if total_spend and fact / total_spend < DIAGNOSIS_MIN_SHARE:
        return None, "факт меньше 1% расхода окна"
    return {
        "id": "expensive_model",
        "title": "Дорогая модель",
        "fact_usd": fact,
        "avoidable_usd": avoidable,
        "confidence": "estimate",
        "where": _where_entries(conn, cost_by_session),
        "what_to_do": "перевести рутину (сбор, правки по шаблону, механические преобразования) на Sonnet/Haiku",
        "assumption": EXPENSIVE_MODEL_ASSUMPTION,
    }, None


def _diagnose_context_cost(conn, since, until, total_spend):
    where, params = _window_where(since, until)
    sql = ("SELECT session_id, model, %s FROM requests%s%s session_id IS NOT NULL"
          " GROUP BY session_id, model"
          % (_COST_FIELDS_SQL, where, " AND" if where else " WHERE"))
    fact = 0.0
    cost_by_session = {}
    context_tokens_by_session = {}
    n_by_session = {}
    for row in conn.execute(sql, params):
        d = dict(row)
        comps = pricing.cost_components(_synthetic_row(d["model"], d))
        if comps is None:
            continue
        fact += comps["cache_read"]
        sid = d["session_id"]
        cost_by_session[sid] = cost_by_session.get(sid, 0.0) + comps["cache_read"]
        context_tokens_by_session[sid] = context_tokens_by_session.get(sid, 0) + d["cr"]
        n_by_session[sid] = n_by_session.get(sid, 0) + d["n"]
    if fact <= 0:
        return None, "нет чтения кэша в окне"
    if total_spend and fact / total_spend < DIAGNOSIS_MIN_SHARE:
        return None, "факт меньше 1% расхода окна"
    where_entries = _where_entries(conn, cost_by_session)
    for entry in where_entries:
        sid = entry["session_id"]
        n = n_by_session.get(sid) or 1
        entry["avg_context_tokens"] = context_tokens_by_session.get(sid, 0) / n
    return {
        "id": "context_cost",
        "title": "Цена размера контекста",
        "fact_usd": fact,
        "avoidable_usd": None,     # не потеря, а плата за размер — контрфактического числа нет
        "confidence": "fact",
        "where": where_entries,
        "what_to_do": "чистить контекст (/clear), дробить длинные сессии, не держать в контексте прочитанные целиком крупные файлы",
        "assumption": "",
    }, None


def _diagnose_subagents(conn, since, until, total_spend):
    where, params = _window_where(since, until)
    extra = "is_sidechain = 1"
    sql = ("SELECT session_id, model, %s FROM requests%s%s %s GROUP BY session_id, model"
          % (_COST_FIELDS_SQL, where, " AND" if where else " WHERE", extra))
    fact = 0.0
    cost_by_session = {}
    for row in conn.execute(sql, params):
        d = dict(row)
        cost = pricing.cost_of_row(_synthetic_row(d["model"], d))
        if cost is None:
            continue
        fact += cost
        sid = d["session_id"]
        if sid:
            cost_by_session[sid] = cost_by_session.get(sid, 0.0) + cost
    if fact <= 0:
        return None, "нет запросов субагентов в окне"
    if total_spend and fact / total_spend < DIAGNOSIS_MIN_SHARE:
        return None, "факт меньше 1% расхода окна"
    return {
        "id": "subagents",
        "title": "Субагенты",
        "fact_usd": fact,
        "avoidable_usd": None,
        "confidence": "fact",
        "where": _where_entries(conn, cost_by_session),
        "what_to_do": "помнить, что каждый субагент оплачивает свой контекст отдельно; для дешёвых механических задач указывать модель попроще",
        "assumption": "",
    }, None


_DIAGNOSERS = [_diagnose_cold_cache, _diagnose_expensive_model,
              _diagnose_context_cost, _diagnose_subagents]


def _total_spend(conn, since, until):
    """Общий $ окна. Считается через cost_by_model (группировка по точному
    имени модели), а не одним SQL-запросом по всей таблице сразу: у разных
    моделей разная цена, единой ценой для смеси моделей это не выразить.
    """
    return sum(r["cost"] or 0.0 for r in cost_by_model(conn, since=since, until=until))


def diagnoses(conn, since=None, until=None):
    total_spend = _total_spend(conn, since, until)
    out = []
    for fn in _DIAGNOSERS:
        d, _reason = fn(conn, since, until, total_spend)
        if d is not None:
            out.append(d)
    out.sort(key=lambda d: -d["fact_usd"])
    return out


def diagnoses_insufficient_ids(conn, since=None, until=None):
    total_spend = _total_spend(conn, since, until)
    out = []
    for fn in _DIAGNOSERS:
        d, reason = fn(conn, since, until, total_spend)
        if d is None:
            diag_id = fn.__name__.replace("_diagnose_", "")
            out.append({"id": diag_id, "reason": reason})
    return out


def overview(conn, days=None, since=None, until=None):
    """Сводка для обзорного экрана и для explain."""
    since_eff = since
    if since_eff is None and days:
        since_eff = (datetime.datetime.now(datetime.timezone.utc).timestamp()
                    - days * 86400)
    where, params = _window_where(since_eff, until)

    totals = dict(zip(
        ("input", "output", "cache_read", "cache_write", "n_requests", "n_sessions"),
        conn.execute(
            "SELECT COALESCE(SUM(input), 0), COALESCE(SUM(output), 0),"
            " COALESCE(SUM(cache_read), 0), COALESCE(SUM(cache_write), 0),"
            " COUNT(*), COUNT(DISTINCT session_id) FROM requests" + where, params).fetchone()))

    bounds = conn.execute(
        "SELECT MIN(ts), MAX(ts) FROM requests" + where, params).fetchone()

    def grouped(expr, limit=None):
        sql = ("SELECT %s AS k, COUNT(*) n, SUM(cache_write) cw,"
               " SUM(cache_read) cr, SUM(output) out FROM requests%s"
               " GROUP BY k ORDER BY cw DESC" % (expr, where))
        if limit:
            sql += " LIMIT %d" % limit
        return [dict(r) for r in conn.execute(sql, params)]

    by_day = grouped("date(ts, 'unixepoch')")
    by_day.sort(key=lambda r: r["k"] or "")

    cost_rows = [dict(r) for r in conn.execute(
        "SELECT model, input, output, cache_read, cache_write, cache_1h, cache_5m"
        " FROM requests" + where, params)]
    cost = pricing.cost_breakdown(cost_rows)
    cbm = cost_by_model(conn, since=since_eff, until=until)
    now_ts = datetime.datetime.now(datetime.timezone.utc).timestamp()
    # Единый якорь окна для bucket_step здесь и внутри spend_by_bucket — иначе
    # у них разный шаг корзины и bucket_label_format не соответствует
    # фактическому числу корзин (см. Task 8 review, Fix 1).
    since_resolved = since_eff if since_eff is not None else (bounds[0] or now_ts)
    until_resolved = until if until is not None else now_ts
    window_s = max(1.0, until_resolved - since_resolved)
    _step, bucket_label_format = bucket_step(window_s)

    # sess_cost_map — окно применено тем же where/params, что и остальные
    # windowed-запросы этой функции (см. Task 8 review, Fix 2): иначе
    # "самые дорогие сессии" показывали all-time доллары рядом с windowed
    # итогом.
    sess_cost_map = {}
    context_tokens_by_session = {}
    n_by_session = {}
    sess_sql = ("SELECT session_id, model, %s FROM requests%s%s session_id IS NOT NULL"
               " GROUP BY session_id, model"
               % (_COST_FIELDS_SQL, where, " AND" if where else " WHERE"))
    for row in conn.execute(sess_sql, params):
        d = dict(row)
        sid = d["session_id"]
        # Копим средний размер контекста тем же проходом, что и денежный
        # итог по сессии, а не отдельным запросом — Д3 ("Цена размера
        # контекста") уже показывает это число в разборе диагноза, здесь
        # оно нужно и для "Самых дорогих сессий" (см. Finding 3, финальное
        # ревью).
        context_tokens_by_session[sid] = context_tokens_by_session.get(sid, 0) + d["cr"]
        n_by_session[sid] = n_by_session.get(sid, 0) + d["n"]
        c = pricing.cost_of_row(_synthetic_row(d["model"], d))
        if c is None:
            continue
        sess_cost_map[sid] = sess_cost_map.get(sid, 0.0) + c
    top_session_ids = sorted(sess_cost_map, key=lambda sid: -sess_cost_map[sid])[:200]
    sessions_raw = []
    meta_map = {}
    if top_session_ids:
        marks = ", ".join("?" for _ in top_session_ids)
        sessions_raw = [dict(r) for r in conn.execute(
            "SELECT * FROM sessions WHERE session_id IN (%s)" % marks, top_session_ids)]
        meta_map = {r["session_id"]: dict(r) for r in conn.execute(
            "SELECT * FROM session_meta WHERE session_id IN (%s)" % marks, top_session_ids)}
    sessions_by_cost = []
    for s in sessions_raw:
        name, _source = session_display_name(s, meta_map.get(s["session_id"]))
        sid = s["session_id"]
        n = n_by_session.get(sid) or 1
        avg_context_tokens = context_tokens_by_session.get(sid, 0) / n
        sessions_by_cost.append({**s, "name": name, "cost": sess_cost_map.get(sid),
                                 "avg_context_tokens": avg_context_tokens})
    sessions_by_cost.sort(key=lambda s: (s["cost"] is None, -(s["cost"] or 0)))

    return {
        "period": {"first_ts": bounds[0], "last_ts": bounds[1], "days": days},
        "totals": totals,
        "efficiency": cache_efficiency(conn, since_eff),
        "cold_cache": cold_cache_summary(conn, since_eff),
        "by_day": by_day,
        "by_model": grouped("model"),
        "by_project": grouped("project", limit=20),
        "sessions": [dict(r) for r in conn.execute(
            "SELECT * FROM sessions ORDER BY cache_write DESC LIMIT 100")],
        "cost": cost,
        "cost_by_model": cbm,
        "cost_by_project": cost_by_project(conn, since=since_eff, until=until),
        "spend_by_bucket": spend_by_bucket(conn, since=since_resolved, until=until_resolved),
        "bucket_label_format": bucket_label_format,
        "diagnoses": diagnoses(conn, since=since_eff, until=until),
        "diagnoses_insufficient": diagnoses_insufficient_ids(conn, since=since_eff, until=until),
        "sessions_by_cost": sessions_by_cost,
    }


TOKEN_KINDS = ("input", "output", "cache_read", "cache_write")

MIN_INTERVALS_LOW = 10
MIN_INTERVALS_MEDIUM = 100

WEIGHTS_NOTE = (
    "Оценка, а не измерение: пятичасовое окно скользящее, поэтому проценты "
    "убывают сами по мере выхода старых запросов, и в расчёт берутся только "
    "интервалы роста. Точность растёт с объёмом наблюдений."
)


def limit_series(conn, session_id):
    cur = conn.execute(
        "SELECT ts, five_hour_pct, seven_day_pct FROM ratelimits"
        " WHERE session_id = ? ORDER BY ts", (session_id,))
    return [dict(r) for r in cur]


def _ratelimits_snapshot(conn, since=None, until=None):
    """Последний СВЕЖИЙ замер ratelimits (хук statusLine) в окне; None, если
    записей нет.

    Раньше брался просто последний по ts, независимо от fresh — простаивающая
    сессия могла эхом повторять старое число позже по времени, чем реальный
    последний замер, и карточка "лимит на момент отчёта" показывала бы его
    как актуальный (fix-wave-2, находка 4). Если свежих замеров в окне нет
    вовсе, отдаём последний доступный, но с fresh=False — молча выдавать
    эхо за "сейчас" отчёт больше не должен.
    """
    where, params = _window_where(since, until)
    rows = [dict(r) for r in conn.execute(
        "SELECT ts, session_id, fresh, five_hour_pct, seven_day_pct"
        " FROM ratelimits%s ORDER BY ts DESC" % where, params)]
    if not rows:
        return None
    need = {r["session_id"] for r in rows if r["fresh"] is None}
    req_index = _request_index(conn, need)
    for r in rows:
        if _row_is_fresh(r, req_index):
            r["fresh"] = True
            r["source"] = "statusline"
            return r
    top = rows[0]
    top["fresh"] = False
    top["source"] = "statusline"
    return top


def _usage_api_snapshot(conn, since=None, until=None):
    """Последний замер usage_snapshots (poll `GET /api/oauth/usage`) в окне;
    None, если пусто. В отличие от ratelimits это не эхо статуслайна, а
    прямое чтение состояния аккаунта на сервере — своего "fresh" у него нет,
    поэтому считаем валидным всегда, когда попадает в окно."""
    where, params = _window_where(since, until)
    row = conn.execute(
        "SELECT ts, five_hour_pct, seven_day_pct FROM usage_snapshots%s"
        " ORDER BY ts DESC LIMIT 1" % where, params).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["session_id"] = None
    d["fresh"] = True
    d["source"] = "usage_api"
    return d


def limit_snapshot(conn, since=None, until=None):
    """Текущее состояние лимита для карточки "на момент отчёта".

    Стадия B: два источника
    разной природы. Приоритет:
      1. Свежий (fresh=True) замер ratelimits — реальная интерактивная
         сессия только что получила ответ API, точнее самого свежего poll.
      2. Последний poll usage_snapshots в окне — не требует интерактивной
         сессии/statusLine, покрывает VS Code/headless/CloudCLI.
      3. Устаревшее эхо ratelimits (fresh=False) — как и раньше, честно
         помечено, если больше взять неоткуда.
      4. None — оба источника пусты.
    Источник виден вызывающему через ключ "source" ("statusline" |
    "usage_api") — в отчёте должно быть видно, откуда взята цифра.
    """
    rl = _ratelimits_snapshot(conn, since=since, until=until)
    if rl is not None and rl["fresh"]:
        return rl
    us = _usage_api_snapshot(conn, since=since, until=until)
    if us is not None:
        return us
    return rl


FRESH_LAG = 120.0
"""Сек: для строк, собранных до появления колонки fresh, замер считается
свежим, если сессия обращалась к API не позже, чем за столько до него.

Порог временный по замыслу. Условие удаления: когда в рабочих окнах не
остаётся строк с fresh IS NULL, ветка мертва и удаляется вместе с
константой. До тех пор срабатывает на любом отчёте, захватывающем период
до обновления хука.
"""

_PCT_FIELDS = ("five_hour_pct", "seven_day_pct")


def _check_pct_field(pct_field):
    if pct_field not in _PCT_FIELDS:
        raise ValueError("pct_field must be one of %r, got %r"
                         % (_PCT_FIELDS, pct_field))


def _request_index(conn, session_ids):
    """ts всех запросов, по сессии — только для восстановления свежести."""
    return {sid: [x[0] for x in conn.execute(
                "SELECT ts FROM requests WHERE session_id = ? ORDER BY ts", (sid,))]
            for sid in session_ids}


def _row_is_fresh(row, req_index):
    """Свежесть строки ratelimits: собственный fresh либо сшивка с requests.

    Общая проверка для _fresh_marks() (события) и limit_snapshot()
    (карточка "сейчас") — обе не должны путать эхо простаивающей сессии с
    новым наблюдением (fix-wave-2, находка 4).
    """
    if row["fresh"] is not None:
        return bool(row["fresh"])
    seq = req_index.get(row["session_id"]) or []
    i = bisect.bisect_right(seq, row["ts"])
    return bool(i) and row["ts"] - seq[i - 1] <= FRESH_LAG


_PRECISION_DEADBAND = 1.0
"""пп: спад меньше этого между замерами разной точности (мод — десятые,
statusline и API — целые) считается расхождением округления, а не спадом.
Между источниками одной точности не применяется: там любой спад реален
(скользящее окно)."""


def _fresh_marks(conn, since=None, until=None, pct_field="five_hour_pct"):
    """Глобальный ряд свежих замеров процента, упорядоченный по времени.

    Ряд один на все сессии: счётчик лимита аккаунт-wide. Замер при этом —
    кэш последнего API-ответа конкретной сессии, поэтому простаивающая
    сессия повторяет застывшее значение; такие строки отбрасываются, иначе
    чередование сессий с разным возрастом кэша читается как рост.

    Снимки usage_snapshots (poll `GET /api/oauth/usage`) входят в ряд как
    замеры без сессии: session_id=None, source="usage_api", всегда свежие.
    Они ловят рост между ответами сессий — простой, claude.ai, другие
    машины (limit-radar-mod, шаг 4).
    """
    _check_pct_field(pct_field)
    where, params = _window_where(since, until)
    rows = [dict(r) for r in conn.execute(
        "SELECT ts, session_id, fresh, source, %s AS pct FROM ratelimits%s"
        % (pct_field, where), params)]
    rows = [r for r in rows if r["pct"] is not None]
    # У сессии с замерами мода statusline не берём: он отдаёт целые
    # проценты, мод — десятые, и их чередование читалось бы как рост.
    with_mod = {r["session_id"] for r in rows if r["source"] == "mod"}
    rows = [r for r in rows
            if r["source"] == "mod" or r["session_id"] not in with_mod]
    snap_sql = ("SELECT ts, NULL AS session_id, 1 AS fresh,"
                " 'usage_api' AS source, %s AS pct FROM usage_snapshots%s"
                % (pct_field, where))
    rows += [dict(r) for r in conn.execute(snap_sql, params)
             if r["pct"] is not None]

    if since is not None:
        # Рост, зафиксированный ДО since, но защёлкнувшийся замером ВНУТРИ
        # окна, иначе теряется целиком: первая пара строится только из
        # замеров внутри окна, а первый из них уже несёт в себе весь прирост
        # с произвольного момента до since (fix-wave-2, находка 6). Якорь —
        # последний замер ДО since из любого источника; если он не свежий,
        # якоря нет. Брать более старый свежий нельзя: снимок месячной
        # давности дал бы пару через несколько сбросов окна.
        cands = [conn.execute(
            "SELECT ts, session_id, fresh, source, %s AS pct FROM ratelimits"
            " WHERE ts < ? AND %s IS NOT NULL ORDER BY ts DESC LIMIT 1"
            % (pct_field, pct_field), (since,)).fetchone(),
                 conn.execute(
            "SELECT ts, NULL AS session_id, 1 AS fresh,"
            " 'usage_api' AS source, %s AS pct FROM usage_snapshots"
            " WHERE ts < ? AND %s IS NOT NULL ORDER BY ts DESC LIMIT 1"
            % (pct_field, pct_field), (since,)).fetchone()]
        cands = [dict(a) for a in cands if a is not None]
        if cands:
            rows.append(max(cands, key=lambda a: a["ts"]))

    # session_id вторым ключом: при совпадающих ts от разных сессий порядок
    # иначе не определён и может незаметно поменять знак d_pct. У снимка
    # session_id None — сравнивать его со строкой Python не умеет.
    rows.sort(key=lambda r: (r["ts"], r["session_id"] or ""))

    # Индекс запросов строим только для сессий, где свежесть неизвестна.
    need = {r["session_id"] for r in rows if r["fresh"] is None}
    req_index = _request_index(conn, need)
    rows = [r for r in rows if _row_is_fresh(r, req_index)]

    # Пила точности: мод 43.4, API 43, мод 43.4 читалось бы как +0.4 дважды.
    # Замер, который ниже последнего принятого меньше чем на
    # _PRECISION_DEADBAND и пришёл из источника другой точности, пропускаем:
    # следующий замер сравнивается с последним принятым.
    out = []
    for r in rows:
        if out:
            last = out[-1]
            drop = last["pct"] - r["pct"]
            if ((last["source"] == "mod") != (r["source"] == "mod")
                    and 0 < drop < _PRECISION_DEADBAND):
                continue
        out.append(r)
    return out


def stale_marks_count(conn, since=None, until=None):
    """Сколько замеров окна собрано до появления признака свежести."""
    where, params = _window_where(since, until)
    sql = ("SELECT COUNT(*) FROM ratelimits%s%s fresh IS NULL"
           % (where, " AND" if where else " WHERE"))
    return conn.execute(sql, params).fetchone()[0]


def limit_events(conn, since=None, until=None, pct_field="five_hour_pct"):
    """Моменты роста процента лимита на глобальном ряду свежих замеров.

    Отдельного порога "выброса" нет: API отдаёт целые проценты, поэтому
    минимальный наблюдаемый шаг в 1 пп уже является событием.
    """
    marks = _fresh_marks(conn, since=since, until=until, pct_field=pct_field)
    out = []
    for prev, cur in zip(marks, marks[1:]):
        d_pct = cur["pct"] - prev["pct"]
        if d_pct <= 0:
            continue      # сброс окна либо остаточное расхождение сессий
        ev = {"t0": prev["ts"], "t1": cur["ts"], "d_pct": d_pct}
        ev.update(event_attribution(conn, prev["ts"], cur["ts"], d_pct,
                                    t0_session_id=prev["session_id"]))
        out.append(ev)
    return out


EVENT_KINDS = {
    "attributed": "атрибутировано",
    "background": "только фоновые",
    "elsewhere": "расход не с этой машины",
    "unresolved": "атрибуция невозможна: интервал уже разрешения",
    "unexplained": "необъяснено",
}


def event_attribution(conn, t0, t1, d_pct, t0_session_id=None):
    """Разложить прирост события между сессиями, вёдшими работу внутри.

    Деление пропорционально токенам — это оценка, а не измерение: какая
    именно сессия израсходовала квоту, наблюдению недоступно. Поэтому
    рядом всегда едут width_s и n_candidates.
    """
    rows = [dict(r) for r in conn.execute(
        "SELECT session_id,"
        " SUM(input) i, SUM(output) o, SUM(cache_read) cr, SUM(cache_write) cw,"
        " MAX(input + cache_read) peak, MIN(is_sidechain) all_side"
        " FROM requests WHERE ts > ? AND ts <= ? GROUP BY session_id",
        (t0, t1))]

    tokens = {k: 0 for k in TOKEN_KINDS}
    for r in rows:
        tokens["input"] += r["i"] or 0
        tokens["output"] += r["o"] or 0
        tokens["cache_read"] += r["cr"] or 0
        tokens["cache_write"] += r["cw"] or 0
    grand = sum(tokens.values())

    base = {
        "kind": "elsewhere", "shares": [], "tokens": tokens,
        "cache_read_share": (tokens["cache_read"] / grand) if grand else None,
        "cache_write_share": (tokens["cache_write"] / grand) if grand else None,
        "peak_context": max((r["peak"] or 0) for r in rows) if rows else None,
        "models": [x[0] for x in conn.execute(
            "SELECT DISTINCT model FROM requests WHERE ts > ? AND ts <= ?"
            " AND model IS NOT NULL ORDER BY model", (t0, t1))],
        "width_s": t1 - t0,
        "n_candidates": len(rows),
    }
    if not rows:
        # Строгий интервал пуст — но прежде чем утверждать "расход не с этой
        # машины", смотрим окно шириной FRESH_LAG перед t0. Между завершением
        # запроса и защёлкиванием нового процента в статусной строке проходит
        # лаг; на боевых данных ВСЕ случаи elsewhere оказались интервалами в
        # секунды, где локальный запрос лежал на доли секунды раньше t0.
        # Это не другая машина, а интервал уже разрешения наблюдения.
        #
        # Запрос сессии, которая сама породила замер t0 (fresh=1 означает
        # "эта сессия только что сходила в API"), в этом окне присутствует
        # ВСЕГДА, структурно — он не свидетельствует ни о чём, кроме факта,
        # что t0 вообще стал свежим замером. Считать его признаком
        # неоднозначности значило бы называть "unresolved" каждый пустой
        # интервал подряд, топя реальные "не с этой машины" в шуме
        # (fix-wave-2, находка 3). Исключаем его из проверки; остаётся
        # только чужая, действительно неоднозначная активность рядом.
        near_params = [t0 - FRESH_LAG, t0]
        near_sql = "SELECT COUNT(*) FROM requests WHERE ts > ? AND ts <= ?"
        if t0_session_id is not None:
            near_sql += " AND session_id IS NOT ?"
            near_params.append(t0_session_id)
        near = conn.execute(near_sql, near_params).fetchone()[0]
        if near:
            base["kind"] = "unresolved"
        return base
    if all(r["all_side"] == 1 for r in rows):
        base["kind"] = "background"
        return base
    if not grand:
        base["kind"] = "unexplained"
        return base

    names = _session_name_map(conn, [r["session_id"] for r in rows])
    shares = []
    for r in rows:
        n = (r["i"] or 0) + (r["o"] or 0) + (r["cr"] or 0) + (r["cw"] or 0)
        if not n:
            continue
        shares.append({"session_id": r["session_id"],
                       "name": names.get(r["session_id"], (r["session_id"] or "?")[:8]),
                       "pct": d_pct * n / grand})
    shares.sort(key=lambda s: -s["pct"])
    base["kind"] = "attributed"
    base["shares"] = shares
    return base


def limit_intervals(conn, since=None, until=None, pct_field="five_hour_pct"):
    """Атрибутируемое подмножество событий — основание для весов.

    События без токенов внутри (расход с другой машины, только фоновые)
    возвращает limit_events(); сюда они не попадают: делить прирост на
    нулевые токены нельзя.
    """
    return [ev for ev in limit_events(conn, since=since, until=until,
                                      pct_field=pct_field)
            if ev["kind"] == "attributed"]


def limit_weights(conn, pct_field="five_hour_pct"):
    """Сколько процентов квоты стоит миллион токенов каждого типа.

    Наивная оценка: суммарный прирост процентов делится на суммарные
    токены, взвешенные по типам. Полноценная регрессия здесь избыточна —
    данных мало, а результат всё равно подаётся как оценка.
    """
    intervals = limit_intervals(conn, pct_field=pct_field)
    if not intervals:
        return {"weights": {}, "n_intervals": 0,
                "confidence": "none", "note": WEIGHTS_NOTE}
    totals = {k: 0 for k in TOKEN_KINDS}
    d_pct = 0.0
    for iv in intervals:
        d_pct += iv["d_pct"]
        for k in TOKEN_KINDS:
            totals[k] += iv["tokens"][k]
    grand = sum(totals.values())
    weights = {}
    if grand:
        per_million = d_pct / (grand / 1_000_000)
        for k in TOKEN_KINDS:
            share = totals[k] / grand
            weights[k] = round(per_million * share, 6)
    n = len(intervals)
    confidence = ("medium" if n >= MIN_INTERVALS_MEDIUM
                  else "low" if n >= MIN_INTERVALS_LOW else "none")
    return {"weights": weights, "n_intervals": n,
            "confidence": confidence, "note": WEIGHTS_NOTE}


def session_pct_share(conn, since, until, pct_field="five_hour_pct"):
    """Сумма долей прироста % лимита по сессиям.

    Доля внутри события делится пропорционально токенам, поэтому число —
    оценка. Сессии без атрибутированных событий отсутствуют в результате:
    это не то же самое, что нулевой расход, и рендерится как "—".
    """
    share = {}
    for ev in limit_intervals(conn, since=since, until=until,
                              pct_field=pct_field):
        for s in ev["shares"]:
            share[s["session_id"]] = share.get(s["session_id"], 0.0) + s["pct"]
    return share


LIMIT_SPIKE_TITLES = {
    "five_hour_pct": "Скачок 5-часового лимита",
    "seven_day_pct": "Скачок недельного лимита",
}


def _diagnose_limit_spike(conn, since, until, pct_field):
    """Крупнейший в окне прирост % лимита, привязанный к сессии.

    Валюта — процентные пункты лимита, не $: не входит в _DIAGNOSERS/
    diagnoses() (та связка сортирует по fact_usd). Показывается только при
    confidence "medium" — атрибуция к одной сессии при малых данных рискует
    указать не туда из-за шума.
    """
    weights = limit_weights(conn, pct_field=pct_field)
    if weights["confidence"] != "medium":
        return None
    ivs = limit_intervals(conn, since=since, until=until, pct_field=pct_field)
    ivs = [iv for iv in ivs if iv["shares"]]
    if len(ivs) < MIN_INTERVALS_MEDIUM:
        # confidence "medium" по всей истории БД не значит "medium" в ЭТОМ
        # окне отчёта — окно может быть намного уже. Гейт и показываемое
        # событие должны относиться к одному и тому же множеству данных
        # (fix-wave-2, находка 8), иначе диагноз рискует указать не туда
        # из-за шума ровно так, как гейт и должен был предотвратить.
        return None
    top = max(ivs, key=lambda iv: iv["d_pct"])
    lead = top["shares"][0]
    return {
        "id": "limit_spike_" + pct_field,
        "title": LIMIT_SPIKE_TITLES[pct_field],
        "fact_pct": top["d_pct"],
        "confidence": weights["confidence"],
        "where": [{"session_id": lead["session_id"],
                   "name": lead["name"],
                   "pct": lead["pct"]}],
        "what_to_do": ("если скачок пришёлся на сессию с признаками не-диалога "
                       "(много запросов за секунды-минуты без сопоставимого роста "
                       "$-расхода) — проверить, не работает ли там фоновая "
                       "автоматизация или интеграция"),
    }


def limit_summary(conn, since=None, until=None):
    """Снэпшот + веса + Д←5 по обоим лимитам. Общая точка входа для отчёта и explain."""
    snapshot = limit_snapshot(conn, since=since, until=until)
    out = {"snapshot": snapshot}
    for pct_field, key in (("five_hour_pct", "five_hour"), ("seven_day_pct", "seven_day")):
        out[key] = {
            "weights": limit_weights(conn, pct_field=pct_field),
            "diagnosis": _diagnose_limit_spike(conn, since, until, pct_field),
        }
    return out


_RU_MONTHS_SHORT = ["янв", "фев", "мар", "апр", "май", "июн",
                    "июл", "авг", "сен", "окт", "ноя", "дек"]


def bucket_step(window_s):
    """Шаг корзины графика и код формата подписи оси X по размеру окна."""
    if window_s <= 6 * 3600:
        return 300, "time"
    if window_s <= 48 * 3600:
        return 3600, "day_time"
    return 86400, "day"


def format_bucket_ts(ts, label_format):
    dt = datetime.datetime.fromtimestamp(ts)
    if label_format == "time":
        return dt.strftime("%H:%M")
    if label_format == "day_time":
        return "%d %s %02d:%02d" % (dt.day, _RU_MONTHS_SHORT[dt.month - 1], dt.hour, dt.minute)
    return "%02d.%02d" % (dt.day, dt.month)
