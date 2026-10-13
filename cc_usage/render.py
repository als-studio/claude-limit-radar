"""Генерация самодостаточного HTML-отчёта."""
import datetime
import html
import json
import math
import time
from pathlib import Path

import analyze
import fmt
import pricing

_MONO = "ui-monospace,'SF Mono',Menlo,Consolas,monospace"

HTML_TEMPLATE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<title>Расход Claude Code</title>
<style>
  :root {
    --bg: oklch(98% 0.003 250); --card: oklch(99.3% 0.002 250);
    --fg: oklch(21% 0.012 255); --muted: oklch(50% 0.014 255);
    --line: oklch(90% 0.006 255);
    --accent: oklch(54% 0.16 260); --accent-soft: oklch(94% 0.025 260);
    --warn: oklch(56% 0.15 45); --warn-soft: oklch(94% 0.045 55);
    --ok: oklch(52% 0.13 155);
    --violet: oklch(52% 0.15 320); --violet-soft: oklch(94% 0.03 320);
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: oklch(17% 0.01 255); --card: oklch(21% 0.012 255);
      --fg: oklch(93% 0.006 255); --muted: oklch(64% 0.013 255);
      --line: oklch(31% 0.014 255);
      --accent: oklch(72% 0.12 260); --accent-soft: oklch(27% 0.05 260);
      --warn: oklch(74% 0.14 55); --warn-soft: oklch(30% 0.06 55);
      --ok: oklch(68% 0.12 155);
      --violet: oklch(74% 0.12 320); --violet-soft: oklch(30% 0.06 320);
    }
  }
  * { box-sizing:border-box; }
  html,body { margin:0; }
  body { background:var(--bg); color:var(--fg);
         font:15px/1.55 system-ui,-apple-system,'Segoe UI',sans-serif; }
  .wrap { max-width:1160px; margin:0 auto; padding:48px 24px 96px; }
  .eyebrow { font-size:12px; font-weight:600; letter-spacing:.08em; text-transform:uppercase;
             color:var(--accent); margin-bottom:8px; }
  h1 { font-size:32px; font-weight:650; letter-spacing:-.01em; margin:0 0 6px; }
  h2 { font-size:18px; font-weight:650; margin:4px 0 14px; }
  h3 { font-size:14px; font-weight:650; margin:1.75rem 0 .75rem; }
  .sub { font-size:14px; color:var(--muted); font-family:__MONO__; }
  .disclaimer { margin-top:18px; padding-top:14px; border-top:1px solid var(--line);
                font-size:12.5px; color:var(--muted); max-width:760px; line-height:1.55; }
  .section-n { margin-top:44px; font-size:12px; color:var(--muted); letter-spacing:.06em;
               text-transform:uppercase; font-weight:600; }
  .cards { display:grid; gap:14px; grid-template-columns:repeat(auto-fit,minmax(200px,1fr));
           margin-top:32px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:20px; }
  .card-head { display:flex; justify-content:space-between; align-items:flex-start; gap:10px; }
  .card .k { font-size:12px; color:var(--muted); text-transform:uppercase; letter-spacing:.06em;
             font-weight:600; }
  .card .v { font-size:30px; font-weight:650; margin-top:10px; font-family:__MONO__;
             font-variant-numeric:tabular-nums; }
  .card.alert { background:var(--warn-soft);
                border-color:color-mix(in oklch, var(--warn) 35%, var(--line)); }
  .card.alert .v { color:var(--warn); }
  .sub-pct { font-size:16px; font-weight:600; color:var(--muted); }
  .badge { display:inline-flex; align-items:center; gap:5px; background:var(--card);
           border:1px solid var(--line); border-radius:20px; padding:3px 8px 3px 6px;
           font-size:10.5px; color:var(--muted); font-weight:600; white-space:nowrap; }
  .badge .dot { width:6px; height:6px; border-radius:50%; background:var(--warn); }
  .note { color:var(--muted); font-size:12.5px; margin-top:.5rem; line-height:1.55; }
  table { border-collapse:collapse; width:100%; font-size:13.5px; }
  thead tr { border-bottom:1px solid var(--line); }
  th { text-align:left; padding:9px 12px 9px 0; color:var(--muted); font-weight:600;
       font-size:11.5px; text-transform:uppercase; letter-spacing:.05em; }
  td { padding:10px 12px 10px 0; border-bottom:1px solid var(--line); }
  tbody tr:last-child td { border-bottom:none; }
  tbody tr:hover { background:var(--accent-soft); }
  th.num,td.num { text-align:right; font-family:__MONO__; font-variant-numeric:tabular-nums; }
  .mono { font-family:__MONO__; }
  .bar-cell { display:inline-flex; align-items:center; gap:8px; }
  .bar-track { width:48px; height:4px; border-radius:2px; background:var(--line); display:inline-block; }
  .bar-fill { height:100%; border-radius:2px; background:var(--accent); display:block; }
  .ev-detail { display:flex; flex-wrap:wrap; align-items:center; gap:6px 14px; }
  .ev-chip { white-space:nowrap; }
  .ev-cand { color:var(--fg); }
  .ev-cand--multi { color:var(--warn); }
  .ev-meter { display:inline-flex; align-items:center; gap:6px; }
  .scroll { overflow-x:auto; }
  .diag-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(320px,1fr)); gap:14px; }
  .diag-card { background:var(--card); border:1px solid var(--line); border-radius:12px;
               overflow:hidden; }
  .diag-card.insufficient { border-style:dashed; }
  .diag-strip { height:3px; }
  .diag-body { padding:18px 20px 16px; }
  .diag-head { display:flex; justify-content:space-between; align-items:baseline; gap:10px; }
  .diag-title { font-size:14.5px; font-weight:650; }
  .diag-fact { font-family:__MONO__; font-variant-numeric:tabular-nums; font-weight:650;
               font-size:15px; white-space:nowrap; }
  .diag-save { margin-top:6px; font-size:13px; color:var(--ok); font-weight:600; }
  .diag-where { margin:12px 0 0; padding:0; list-style:none; font-size:12.5px;
                color:var(--muted); line-height:1.7; }
  .diag-todo { margin-top:12px; padding-top:12px; border-top:1px solid var(--line);
               font-size:12.5px; line-height:1.5; }
  .diag-assumption { margin-top:10px; font-size:11px; color:var(--muted); font-style:italic; }
  svg { max-width:100%; height:auto; display:block; }
  .chart-card { background:var(--card); border:1px solid var(--line); border-radius:12px;
                padding:20px 24px 12px; margin-top:10px; }
  .legend { display:flex; gap:22px; justify-content:center; padding:6px 0 10px;
            font-size:12.5px; color:var(--muted); flex-wrap:wrap; }
  .legend .item { display:flex; align-items:center; gap:7px; }
  .legend .swatch { width:10px; height:10px; border-radius:2px; }
</style></head><body>
<div class="wrap">
__HEADER__
__BODY__
</div>
<script id="payload" type="application/json">__PAYLOAD__</script>
</body></html>
""".replace("__MONO__", _MONO)


def _n(x):
    return format(int(x or 0), ",").replace(",", " ")


def _when(ts):
    if not ts:
        return "—"
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


_CONFIDENCE_RANK = {"none": 0, "low": 1, "medium": 2}


def build_payload(conn, since=None, until=None, session_id=None, debug=False):
    o = analyze.overview(conn, since=since, until=until)
    payload = {"overview": o, "generated_at": time.time(), "debug": debug,
              "prices_as_of": pricing.PRICES_AS_OF}
    if not o["totals"]["n_requests"]:
        # Различаем пустую БАЗУ от пустого ОКНА (см. Finding 4, финальное
        # ревью) — тот же разбор, что и в cli.explain_text.
        payload["db_total_requests"] = conn.execute(
            "SELECT COUNT(*) FROM requests").fetchone()[0]
    if debug:
        payload["weights"] = analyze.limit_weights(conn)

    payload["limit_summary"] = analyze.limit_summary(conn, since=since, until=until)
    five_hour_conf = payload["limit_summary"]["five_hour"]["weights"]["confidence"]
    seven_day_conf = payload["limit_summary"]["seven_day"]["weights"]["confidence"]
    payload["session_limit_pct_field"] = (
        "seven_day_pct"
        if _CONFIDENCE_RANK[seven_day_conf] > _CONFIDENCE_RANK[five_hour_conf]
        else "five_hour_pct")
    payload["session_limit_pct"] = analyze.session_pct_share(
        conn, since, until, pct_field=payload["session_limit_pct_field"])

    # Лента событий показывает оба поля лимита (5h и 7d), а не то, что
    # победило по confidence: у 5h интервалов заведомо больше (окно короче,
    # сбросов чаще), поэтому "поле с большей confidence" на практике почти
    # всегда 5h — и недельная лента, ради которой написан мотивирующий кейс
    # vision.md, в отчёте не показывалась бы никогда (fix-wave-2, находка 1).
    # Колонка в таблице сессий (session_limit_pct_field выше) остаётся одним
    # полем намеренно — там смешивать шкалы в одной колонке было бы плохо.
    payload["limit_events"] = {
        field: analyze.limit_events(conn, since=since, until=until, pct_field=field)
        for field in ("five_hour_pct", "seven_day_pct")}
    payload["limit_stale_rows"] = analyze.stale_marks_count(conn, since, until)
    # Кривая на графике расхода — одна линия, значит одно поле. Берём то же,
    # что и колонка в таблице сессий: две шкалы на одной оси не совместить,
    # а лента событий рядом показывает оба поля и закрывает недельный случай.
    payload["limit_curve"] = analyze.limit_curve_for_buckets(
        conn, o["spend_by_bucket"], pct_field=payload["session_limit_pct_field"])

    if session_id:
        payload["session"] = {
            "id": session_id,
            "cost_usd": analyze.session_cost(conn, session_id),
            "ramp": analyze.session_ramp(conn, session_id),
            "rows": analyze.session_rows(conn, session_id),
            "limits": analyze.limit_series(conn, session_id),
        }
    return payload


def _nice_step(raw):
    """Округление шага сетки графика вверх до "круглого" 1/2/5 * 10^n."""
    if raw <= 0:
        return 1.0
    exp = math.floor(math.log10(raw))
    base = raw / (10 ** exp)
    nice = 1 if base <= 1 else 2 if base <= 2 else 5 if base <= 5 else 10
    return nice * (10 ** exp)


def _spend_chart(buckets, label_format, limit_pct=None, width=780, height=220):
    if not buckets:
        return "<p>Недостаточно данных для графика.</p>"
    has_limit_line = limit_pct is not None and any(p is not None for p in limit_pct)
    top_raw = max(b["cost"] for b in buckets)
    if top_raw <= 0:
        y_max, step_y, n_ticks = 1.0, 1.0, 1
    else:
        step_y = _nice_step(top_raw / 4)
        n_ticks = max(1, math.ceil(top_raw / step_y))
        y_max = step_y * n_ticks
    n = len(buckets)
    pad_l, pad_r, pad_t, pad_b = 52, (34 if has_limit_line else 10), 14, 28
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    bx_step = plot_w / n
    bw = max(0.3, bx_step * 0.62)
    y_base = height - pad_b

    grid = []
    for i in range(n_ticks + 1):
        val = step_y * i
        y = y_base - (val / y_max * plot_h if y_max else 0.0)
        dash = ' stroke-dasharray="2,3"' if i else ""
        grid.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="var(--line)"%s/>'
                    % (pad_l, y, width - pad_r, y, dash))
        grid.append('<text x="%d" y="%.1f" text-anchor="end" font-size="11" '
                    'font-family="%s" fill="var(--muted)">%s</text>'
                    % (pad_l - 8, y + 4, _MONO, html.escape(fmt.money(val))))
    grid.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="var(--line)"/>'
               % (pad_l, pad_t, pad_l, y_base))

    bars = []
    n_labels = min(6, n)
    label_every = max(1, n // n_labels)
    for idx, b in enumerate(buckets):
        write_cost = b.get("cache_write", 0.0)
        other_cost = b.get("other", b["cost"] - write_cost)
        x = pad_l + idx * bx_step
        h_other = (other_cost / y_max * plot_h) if y_max else 0.0
        h_write = (write_cost / y_max * plot_h) if y_max else 0.0
        if h_other > 0:
            y = y_base - h_other
            title_txt = html.escape("%s — работа: %s" % (
                analyze.format_bucket_ts(b["ts"], label_format), fmt.money(other_cost)))
            bars.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" '
                        'fill="var(--accent)"><title>%s</title></rect>'
                        % (x, y, bw, max(0.5, h_other), title_txt))
        if h_write > 0:
            y = y_base - h_other - h_write
            title_txt = html.escape("%s — запись кэша: %s" % (
                analyze.format_bucket_ts(b["ts"], label_format), fmt.money(write_cost)))
            bars.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" rx="2" '
                        'fill="var(--warn)"><title>%s</title></rect>'
                        % (x, y, bw, max(0.5, h_write), title_txt))
        if idx % label_every == 0:
            bars.append('<text x="%.1f" y="%d" text-anchor="middle" font-size="11" '
                        'font-family="%s" fill="var(--muted)">%s</text>'
                        % (x + bw / 2, height - 6, _MONO,
                           html.escape(analyze.format_bucket_ts(b["ts"], label_format))))

    line = []
    if has_limit_line:
        line.append('<text x="%d" y="%.1f" text-anchor="start" font-size="11" '
                    'font-family="%s" fill="var(--violet)">100%%</text>'
                    % (width - pad_r + 6, pad_t + 4, _MONO))
        line.append('<text x="%d" y="%.1f" text-anchor="start" font-size="11" '
                    'font-family="%s" fill="var(--violet)">0%%</text>'
                    % (width - pad_r + 6, y_base + 4, _MONO))
        run = []
        for idx, pct in enumerate(limit_pct):
            if pct is None:
                if len(run) >= 2:
                    line.append('<polyline points="%s" fill="none" stroke="var(--violet)" '
                               'stroke-width="2"/>' % " ".join("%.1f,%.1f" % p for p in run))
                run = []
                continue
            x = pad_l + idx * bx_step + bw / 2
            y = y_base - (pct / 100 * plot_h)
            run.append((x, y))
        if len(run) >= 2:
            line.append('<polyline points="%s" fill="none" stroke="var(--violet)" '
                       'stroke-width="2"/>' % " ".join("%.1f,%.1f" % p for p in run))

    legend = (
        '<div class="legend">'
        '<div class="item"><span class="swatch" style="background:var(--accent)"></span>'
        'реальная работа (input / output / чтение кэша)</div>'
        '<div class="item"><span class="swatch" style="background:var(--warn)"></span>'
        'запись кэша, где можно сэкономить</div>'
        + ('<div class="item"><span class="swatch" style="background:var(--violet)"></span>'
           'лимит нарастающим итогом, %% (правая ось)</div>' if has_limit_line else '')
        + '</div>')
    svg = ('<svg viewBox="0 0 %d %d" role="img" '
          'aria-label="Расход по времени: работа, запись кэша%s">%s%s%s</svg>'
          % (width, height, ", рост лимита" if has_limit_line else "",
             "".join(grid), "".join(bars), "".join(line)))
    return svg + legend


_DIAGNOSIS_TITLES = {
    "cold_cache": "Остывший кэш после простоя",
    "expensive_model": "Дорогая модель",
    "context_cost": "Цена размера контекста",
    "subagents": "Субагенты",
}

_DIAGNOSIS_ACCENTS = {
    "cold_cache": "var(--warn)",
    "expensive_model": "var(--violet)",
    "context_cost": "var(--accent)",
    "subagents": "var(--ok)",
}


def _where_li(w):
    # Д3 ("Цена размера контекста") прикладывает средний размер
    # контекста к каждому пункту where — остальные диагнозы этого поля
    # не несут (см. Finding 3, финальное ревью).
    if w.get("avg_context_tokens") is not None:
        return "<li>%s — %s (в среднем %s контекста на запрос)</li>" % (
            html.escape(w["name"]), fmt.money(w["cost"]), fmt.tokens(w["avg_context_tokens"]))
    return "<li>%s — %s</li>" % (html.escape(w["name"]), fmt.money(w["cost"]))


def _diag_card(d):
    color = _DIAGNOSIS_ACCENTS.get(d["id"], "var(--line)")
    where_rows = "".join(_where_li(w) for w in d["where"])
    save_html = ('<div class="diag-save">можно сэкономить: %s</div>' % fmt.money(d["avoidable_usd"])
                if d["avoidable_usd"] is not None else
                '<div class="note" style="margin-top:6px">не потеря — плата за расход</div>')
    assumption_html = ('<div class="diag-assumption">%s</div>' % html.escape(d["assumption"])
                       if d["assumption"] else "")
    return """
<div class="diag-card">
<div class="diag-strip" style="background:%s"></div>
<div class="diag-body">
<div class="diag-head"><div class="diag-title">%s</div><div class="diag-fact">%s</div></div>
%s
<ul class="diag-where">%s</ul>
<div class="diag-todo">%s</div>
%s
</div></div>""" % (color, html.escape(d["title"]), fmt.money(d["fact_usd"]), save_html,
                   where_rows, html.escape(d["what_to_do"]), assumption_html)


def _insufficient_diag_card(i):
    title = _DIAGNOSIS_TITLES.get(i["id"], i["id"])
    return """
<div class="diag-card insufficient">
<div class="diag-strip" style="background:var(--line)"></div>
<div class="diag-body">
<div class="diag-head"><div class="diag-title" style="color:var(--muted)">%s</div>
<div class="diag-fact" style="color:var(--muted)">—</div></div>
<div class="note" style="margin-top:10px;font-style:italic">недостаточно данных: %s</div>
</div></div>""" % (html.escape(title), html.escape(i["reason"]))


def _metric_card(label, value_html, cls=""):
    return ('<div class="card %s"><div class="k">%s</div><div class="v">%s</div></div>'
           % (cls, html.escape(label), value_html))


def _alert_metric_card(label, value_html):
    return (
        '<div class="card alert">'
        '<div class="card-head"><div class="k">%s</div>'
        '<span class="badge"><span class="dot"></span>требует внимания</span></div>'
        '<div class="v">%s</div></div>'
        % (html.escape(label), value_html))


def _fmt_limit_pct(v):
    # five_hour_pct/seven_day_pct уже в шкале 0-100 — НЕ fmt.pct() (та ждёт 0.0-1.0).
    return ("%.1f%%" % v) if v is not None else "—"


def _limit_snapshot_value(snapshot):
    return "5h %s &middot; 7d %s" % (
        html.escape(_fmt_limit_pct(snapshot["five_hour_pct"])),
        html.escape(_fmt_limit_pct(snapshot["seven_day_pct"])))


def _limit_snapshot_note_suffix(snapshot):
    """Откуда взята цифра и насколько ей можно доверять — Стадия B
    (стадия B): два источника разной
    природы, в отчёте должно быть видно, какой из них сработал."""
    suffix = ""
    if snapshot.get("source") == "usage_api":
        suffix += " · через /api/oauth/usage"
    if not snapshot.get("fresh", True):
        suffix += " (нет свежих замеров в окне — возможно, устарело)"
    return suffix


def _limit_card_and_note(limit_summary):
    snapshot = limit_summary["snapshot"]
    if snapshot is None:
        card = _metric_card("Лимит", "нет данных")
        note = ('<p class="note">Данных о лимите нет: хук statusLine не настроен '
               'или ещё ни разу не сработал (см. cc_usage/README.md, «Statusline '
               'на новой машине»).</p>')
        return card, note

    value_html = _limit_snapshot_value(snapshot)
    card = _metric_card("Лимит", value_html)

    # Таймстамп — в примечание, не в крупное число карточки: "5h X% · 7d Y%"
    # уже длиннее значений соседних карточек, а добавление "на <дата время>"
    # тем же крупным моноширинным шрифтом переполняло карточку вбок
    # (обнаружено пользователем на реальном отчёте, не поймано ревью).
    when = "на %s%s" % (html.escape(_when(snapshot["ts"])),
                        _limit_snapshot_note_suffix(snapshot))
    parts = [when]
    for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
        info = limit_summary[key]
        if info["weights"]["confidence"] != "medium":
            # Не "разбивки нет" — разбивка (колонка "% лимита" в таблице
            # сессий) есть всегда, гейта на confidence не имеет (см.
            # tech-design.md). Здесь мало наблюдений именно для уверенного
            # указания САМОГО большого потребителя (Fix 2, финальное ревью).
            parts.append("%s: наблюдений мало, назвать главного потребителя лимита нельзя" % label)
        elif info["diagnosis"]:
            d = info["diagnosis"]
            top = d["where"][0]
            # "резкий одиночный скачок" — top-1 интервал по d_pct, отличается
            # от колонки таблицы (сумма всех growth-интервалов сессии) — это
            # не противоречие, а разные измерения (Fix 2b, финальное ревью).
            # Величина скачка (fact_pct) и доля лидера в нём (top["pct"]) —
            # разные числа: при нескольких кандидатах внутри события доля
            # меньше скачка, поэтому печатаем обе.
            parts.append("%s: самый резкий одиночный скачок +%.1f пп, "
                        "из них на сессию «%s» ~%.1f пп"
                        % (label, d["fact_pct"], html.escape(top["name"]), top["pct"]))
        else:
            # confidence medium ПО ВСЕЙ истории, но в этом окне (--since/--until)
            # growth-интервалов нет — не тот же случай, что "мало данных вообще".
            parts.append("%s: скачков лимита в этом окне нет" % label)
    note = '<p class="note">%s</p>' % " · ".join(parts)
    return card, note


def _fmt_width(seconds):
    if seconds < 1:
        # "0 с" читается как "данных нет"; здесь смысл другой — интервал
        # короче, чем можно измерить.
        return "<1 с"
    if seconds < 90:
        return "%d с" % int(seconds)
    if seconds < 5400:
        return "%d мин" % round(seconds / 60.0)
    return "%.1f ч" % (seconds / 3600.0)


def _ev_meter(label, value_html, fill_pct, color_var):
    """Мини-бар в ленте событий. Цвет — тот же, что у категории в общем графике расхода."""
    return ('<span class="ev-meter"><span>%s %s</span>'
            '<span class="bar-track"><span class="bar-fill" style="width:%.0f%%;background:var(%s)">'
            '</span></span></span>'
            % (label, value_html, max(0.0, min(100.0, fill_pct)), color_var))


def _limit_event_row(ev, field_label, max_peak_context):
    """Строка ленты: величина, кто, и чем набрано — с шириной интервала."""
    if ev["kind"] == "attributed":
        # Все кандидаты, не только лидер: без этого нельзя проверить
        # атрибуцию (fix-wave-2, находка 9).
        who = ", ".join("«%s» +%.1f пп" % (html.escape(s["name"]), s["pct"])
                        for s in ev["shares"])
    elif ev["kind"] == "elsewhere":
        who = ("расход не с этой машины — квота общая на аккаунт, "
               "а запросы в базе только локальные")
    elif ev["kind"] == "unresolved":
        who = ("рядом есть локальная активность, но не внутри интервала — "
               "не атрибутировано")
    elif ev["kind"] == "background":
        who = "работали только фоновые агенты"
    else:
        who = "запросы есть, но без токенов — объяснения нет"

    cand_cls = " ev-cand--multi" if ev["n_candidates"] > 1 else ""
    detail = ["интервал %s" % html.escape(_fmt_width(ev["width_s"])),
              "кандидатов <b class='ev-cand%s'>%d</b>" % (cand_cls, ev["n_candidates"])]
    if ev["cache_read_share"] is not None:
        pct = ev["cache_read_share"] * 100
        detail.append(_ev_meter("чтение кэша", "%.0f%%" % pct, pct, "--accent"))
    if ev["cache_write_share"] is not None:
        pct = ev["cache_write_share"] * 100
        detail.append(_ev_meter("запись кэша", "%.0f%%" % pct, pct, "--warn"))
    if ev["peak_context"]:
        # Абсолютного потолка окна контекста в данных нет (у моделей он
        # разный, включая бету на 1M) — бар относительный, к максимуму
        # пика среди событий в этой же ленте, а не к доле лимита.
        rel = (ev["peak_context"] / max_peak_context * 100) if max_peak_context else 0.0
        detail.append(_ev_meter("пик контекста", html.escape(fmt.tokens(ev["peak_context"])),
                                rel, "--violet"))
    if ev["models"]:
        detail.append("модели: %s" % ", ".join(ev["models"]))

    return ("<tr><td class='num'>%s</td><td class='num'>%s +%.1f пп</td>"
            "<td>%s</td><td class='note'><div class='ev-detail'>%s</div></td></tr>"
            % (_when(ev["t1"]), html.escape(field_label), ev["d_pct"],
               who, "".join("<span class='ev-chip'>%s</span>" % d for d in detail)))


_LIMIT_FIELD_LABEL = {"five_hour_pct": "5h", "seven_day_pct": "7d"}


def _limit_events_section(events_by_field, stale_rows, has_marks):
    """Лента событий по обоим полям лимита (5h и 7d), слитая по времени.

    Не выбирает одно поле по confidence (fix-wave-2, находка 1): у 5h
    интервалов заведомо больше, поэтому такой выбор на практике почти
    всегда прятал недельную ленту. Сироты — в той же ленте, с названной
    причиной.
    """
    merged = sorted(
        ((e, field) for field, events in events_by_field.items() for e in events),
        key=lambda pair: pair[0]["t1"])
    if not merged:
        if not has_marks:
            why = ("замеров лимита нет: хук statusLine не настроен или ещё "
                   "ни разу не сработал (см. cc_usage/README.md).")
        else:
            why = ("роста процента в этом окне не зафиксировано — "
                   "либо расхода не было, либо он меньше 1 пп, "
                   "а источник отдаёт целые проценты.")
        body = "<p>%s</p>" % why
    else:
        # Масштаб бара — по всей слитой ленте, а не по одному полю: иначе
        # события 5h и 7d в одной таблице мерились бы разными линейками.
        max_peak_context = max((e["peak_context"] or 0) for e, _ in merged)
        body = ('<div class="scroll"><table>'
                '<thead><tr><th class="num">Когда</th><th class="num">Скачок</th>'
                '<th>Кто</th><th>Чем набрано</th></tr></thead><tbody>%s'
                '</tbody></table></div>'
                % "".join(_limit_event_row(e, _LIMIT_FIELD_LABEL[field], max_peak_context)
                          for e, field in merged))
        body += ('<p class="note">Деление между сессиями — оценка по доле '
                 'токенов (чтение кэша считается наравне с остальными, '
                 'хотя по факту стоит иначе), а не измерение. На коротких '
                 'интервалах (секунды-минуты) прирост может ошибочно '
                 'приписаться соседнему запросу или сессии — точная привязка '
                 'к моменту списания квоты недоступна. Чем шире интервал и '
                 'чем больше кандидатов, тем меньше в нём смысла.</p>')
    if stale_rows:
        body += ('<p class="note">%d замеров в окне собраны до появления '
                 'признака свежести — разрешение там грубое, интервалы шире '
                 'реальных. Точная картина начинается с обновления хука.</p>'
                 % stale_rows)
    return body


def _overview_body(o, debug, weights=None, db_total_requests=None,
                   limit_summary=None, session_limit_pct=None,
                   session_limit_pct_field=None, limit_events=None,
                   limit_stale_rows=0, limit_curve=None):
    limit_events_html = _limit_events_section(
        limit_events or {},
        limit_stale_rows,
        has_marks=limit_summary is not None and limit_summary["snapshot"] is not None)

    if not o["totals"]["n_requests"]:
        # AC1 не требует наличия requests в окне — только хотя бы одного
        # замера ratelimits. Мотивирующий кейс Павла: хук statusLine
        # активен, окно collect'а пустое (см. Fix 1, финальное ревью).
        limit_html = ""
        if limit_summary is not None and limit_summary["snapshot"] is not None:
            snap = limit_summary["snapshot"]
            limit_html = ('<p class="note">Лимит на момент отчёта (%s)%s: %s.</p>'
                          % (html.escape(_when(snap["ts"])),
                             _limit_snapshot_note_suffix(snap),
                             _limit_snapshot_value(snap)))
        events_section = ('\n\n<div class="section-n">05</div>\n<h2>События лимита</h2>\n%s'
                          % limit_events_html)
        if db_total_requests:
            return ("<p>В выбранном окне нет запросов. Расширь --since/--until"
                    " или убери их, чтобы увидеть весь период.</p>" + limit_html
                    + events_section)
        return ("<p>Нет данных: база пуста. Запусти сбор командой collect.</p>" + limit_html
                + events_section)

    t = o["totals"]
    cost = o["cost"]
    cat = cost["by_category"]
    total = cost["total"]
    # Д1 и Д2 могут оценивать устранимое по частично одним и тем же токенам
    # (например, дорогая модель, у которой заодно остыл кэш) — наивная сумма
    # способна превысить общий расход. Ограничиваем сверху total (см.
    # Finding 1, финальное ревью).
    avoidable_total = min(sum((d["avoidable_usd"] or 0.0) for d in o["diagnoses"]), total)

    avoidable_value_html = html.escape(fmt.money(avoidable_total))
    if total:
        avoidable_value_html += (' <span class="sub-pct">· %s</span>'
                                 % html.escape(fmt.pct(avoidable_total / total)))
    cards_html = (
        _metric_card("Расход (оценка)", html.escape(fmt.money(total)))
        + _metric_card("Запросов", html.escape(str(t["n_requests"])))
        + _metric_card("Сессий", html.escape(str(t["n_sessions"])))
        + (_alert_metric_card("Можно сэкономить", avoidable_value_html) if avoidable_total
           else _metric_card("Можно сэкономить", avoidable_value_html))
    )
    limit_card_html, limit_note_html = (
        _limit_card_and_note(limit_summary) if limit_summary is not None else ("", ""))
    cards_html += limit_card_html
    avoidable_note = ('<p class="note">«Можно сэкономить» — не то, что вы уже потратили зря, а '
                      'оценка того, сколько ушло бы меньше, если избежать причин из диагнозов ниже '
                      '(остывший кэш, дорогая модель и т.д.). Оценки диагнозов могут пересекаться '
                      '(например, дорогая модель и остывший кэш — одни и те же токены), поэтому '
                      'сумма ограничена общим расходом и не складывается из диагнозов буквально.</p>')
    avoidable_note += limit_note_html

    def cat_row(name, key):
        val = cat[key]
        share = (val / total) if total else 0.0
        bar = ('<div class="bar-cell"><span>%s</span>'
              '<div class="bar-track"><div class="bar-fill" style="width:%.1f%%"></div></div>'
              '</div>' % (html.escape(fmt.pct(share)), max(0.0, min(100.0, share * 100))))
        return "<tr><td>%s</td><td class='num'>%s</td><td class='num'>%s</td></tr>" % (
            html.escape(name), fmt.money(val), bar)

    category_rows = (cat_row("Чтение кэша", "cache_read")
                     + cat_row("Запись кэша", "cache_write")
                     + cat_row("Output", "output")
                     + cat_row("Input", "input"))

    model_rows = "".join(
        "<tr><td class='mono'>%s</td><td class='num'>%s</td><td class='num'>%s</td>"
        "<td class='num'>%d</td><td class='num'>%s</td></tr>" % (
            html.escape(m["model"] or "?"),
            fmt.money(m["cost"]) if m["cost"] is not None else "—",
            fmt.pct(m["cost"] / total) if (m["cost"] is not None and total) else "—",
            m["n"], fmt.pct(m["share_requests"]))
        for m in o["cost_by_model"] if m["cost"] is not None)

    unpriced = [m for m in o["cost_by_model"] if m["cost"] is None]
    unpriced_html = ""
    if unpriced:
        n_unpriced = sum(m["n"] for m in unpriced)
        names = ", ".join(html.escape(m["model"] or "?") for m in unpriced)
        unpriced_html = ('<p class="note">не учтено: %d запросов на моделях вне прайса '
                         '(%s)</p>' % (n_unpriced, names))

    project_rows = "".join(
        "<tr><td>%s</td><td class='num'>%s</td></tr>" % (
            html.escape(p["k"] or "—"), fmt.money(p["cost"]))
        for p in o["cost_by_project"][:10])

    diag_cards_html = ("".join(_diag_card(d) for d in o["diagnoses"])
                       + "".join(_insufficient_diag_card(i) for i in o["diagnoses_insufficient"]))
    diag_section_html = ('<div class="diag-grid">%s</div>' % diag_cards_html if diag_cards_html
                         else "<p>Существенных перерасходов не найдено.</p>")

    sessions_shown = o["sessions_by_cost"][:50]
    sess_footnote = ""
    if len(o["sessions_by_cost"]) > len(sessions_shown):
        sess_footnote = ('<div class="note">показаны %d из %d, отсортированы по расходу</div>'
                         % (len(sessions_shown), len(o["sessions_by_cost"])))
    if session_limit_pct_field:
        # Колонка "% лимита" — сырая сумма, без confidence-гейта (в отличие
        # от Д←5), и видит только эту машину (см. Fix 2c, финальное ревью).
        sess_footnote += ('<div class="note">% лимита — сумма наблюдённого прироста процента по '
                          'growth-интервалам этой сессии, оценка; активность с других машин того же '
                          'аккаунта сюда не попадает.</div>')

    limit_col_label = ("%% лимита, %s" %
                       ("5h" if session_limit_pct_field == "five_hour_pct" else "7d"))
    session_limit_pct = session_limit_pct or {}

    def _limit_pct_cell(sid):
        v = session_limit_pct.get(sid)
        return ("+%.1f пп" % v) if v is not None else "—"

    sess_rows = "".join(
        "<tr><td>%s</td><td>%s</td><td class='num'>%s</td>"
        "<td class='num'>%s</td><td class='num'>%s</td><td class='num'>%s</td>"
        "<td class='num'>%s</td></tr>" % (
            html.escape(s["name"]),
            html.escape(analyze.readable_project_label(s.get("cwd"), s.get("project")) or "—"),
            fmt.money(s["cost"]) if s["cost"] is not None else "—",
            s["n_requests"], fmt.tokens(s.get("avg_context_tokens") or 0), _when(s["last_ts"]),
            _limit_pct_cell(s["session_id"]))
        for s in sessions_shown)

    debug_html = ""
    if debug and weights is not None:
        debug_html = """
<h2>Отладка</h2>
<h3>Попаданий в кэш</h3>
<p>%s</p>
<h3>Перевод в проценты лимита</h3>
<p>Достоверность: <b>%s</b>, интервалов: %d</p>
<p class="note">%s</p>
""" % (fmt.pct(o["efficiency"]["hit_ratio"]), html.escape(weights["confidence"]),
       weights["n_intervals"], html.escape(weights["note"]))

    return """
<div class="cards">%s</div>
%s

<div class="section-n">01</div>
<h2>Куда ушли деньги</h2>
<div class="scroll"><table>
<thead><tr><th>Статья</th><th class="num">$</th><th class="num">Доля</th></tr></thead>
<tbody>%s</tbody></table></div>

<div class="section-n">02</div>
<h2>По моделям</h2>
<div class="scroll"><table>
<thead><tr><th>Модель</th><th class="num">$</th><th class="num">Доля денег</th>
<th class="num">Запросов</th><th class="num">Доля запросов</th></tr></thead>
<tbody>%s</tbody></table></div>
%s

<div class="section-n">03</div>
<h2>По проектам (топ-10)</h2>
<div class="scroll"><table>
<thead><tr><th>Проект</th><th class="num">$</th></tr></thead>
<tbody>%s</tbody></table></div>

<div class="section-n">04</div>
<h2>Что можно исправить</h2>
%s

<div class="section-n">05</div>
<h2>События лимита</h2>
%s

<div class="section-n">06</div>
<h2>Самые дорогие сессии</h2>
<div class="scroll"><table>
<thead><tr><th>Сессия</th><th>Проект</th><th class="num">$</th><th class="num">Запросов</th>
<th class="num">Ср. контекст</th><th class="num">Последняя активность</th>
<th class="num">%s</th></tr></thead>
<tbody>%s</tbody></table></div>
%s

<div class="section-n">07</div>
<h2>Расход по времени</h2>
<div class="chart-card">%s</div>
%s
""" % (cards_html, avoidable_note, category_rows, model_rows, unpriced_html,
       project_rows or "<tr><td colspan='2'>Нет данных</td></tr>",
       diag_section_html, limit_events_html, limit_col_label,
       sess_rows or "<tr><td colspan='7'>Нет сессий</td></tr>", sess_footnote,
       _spend_chart(o["spend_by_bucket"], o["bucket_label_format"], limit_pct=limit_curve), debug_html)


def _timeline_svg(rows, limits, width=1000, height=300):
    """Расход по времени, простои и кривая процента лимита."""
    if not rows:
        return "<p>В сессии нет запросов.</p>"
    t0 = rows[0]["ts"]
    t1 = rows[-1]["ts"]
    span = (t1 - t0) or 1.0            # одна точка — не делим на ноль
    pad_l, pad_r, pad_b = 45, 45, 24
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_b - 20

    def x_of(ts):
        return pad_l + (ts - t0) / span * plot_w

    cold = {(e["request_id"], e["message_id"]) for e in analyze.find_cold_cache(rows)}
    top = max((r["cache_write"] + r["output"]) for r in rows) or 1

    parts = []
    for prev, cur in zip(rows, rows[1:]):
        gap = cur["ts"] - prev["ts"]
        if gap > 300:      # косметический порог отрисовки серой полосы, см. задачу 6
            x0, x1 = x_of(prev["ts"]), x_of(cur["ts"])
            parts.append(
                '<rect x="%.1f" y="20" width="%.1f" height="%.1f" '
                'fill="var(--line)" opacity=".55"><title>простой %.0f мин</title>'
                '</rect>' % (x0, max(1.0, x1 - x0), plot_h, gap / 60))

    for r in rows:
        v = r["cache_write"] + r["output"]
        h = v / top * plot_h
        x = x_of(r["ts"])
        is_cold = (r["request_id"], r["message_id"]) in cold
        color = "var(--warn)" if is_cold else "var(--accent)"
        label = ("остывший кэш: пауза %.0f мин, переписано %s"
                 % ((r["gap"] or 0) / 60, fmt.tokens(r["cache_write"]))
                 if is_cold else
                 "%s — запись %s" % (_when(r["ts"]), fmt.tokens(r["cache_write"])))
        parts.append(
            '<rect x="%.1f" y="%.1f" width="3" height="%.1f" fill="%s">'
            '<title>%s</title></rect>'
            % (x, 20 + plot_h - h, h, color, html.escape(label)))

    pts = [(x_of(m["ts"]), 20 + plot_h - (m["five_hour_pct"] or 0) / 100 * plot_h)
           for m in limits
           if m.get("five_hour_pct") is not None and t0 <= m["ts"] <= t1]
    if len(pts) >= 2:
        coords = " ".join("%.1f,%.1f" % p for p in pts)
        parts.append('<polyline points="%s" fill="none" stroke="var(--ok)" '
                     'stroke-width="2"><title>5-часовой лимит, %%</title>'
                     '</polyline>' % coords)

    pts7 = [(x_of(m["ts"]), 20 + plot_h - (m["seven_day_pct"] or 0) / 100 * plot_h)
            for m in limits
            if m.get("seven_day_pct") is not None and t0 <= m["ts"] <= t1]
    if len(pts7) >= 2:
        coords7 = " ".join("%.1f,%.1f" % p for p in pts7)
        parts.append('<polyline points="%s" fill="none" stroke="var(--violet)" '
                     'stroke-width="2"><title>недельный лимит, %%</title>'
                     '</polyline>' % coords7)

    parts.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="var(--line)"/>'
                 % (pad_l, 20 + plot_h, width - pad_r, 20 + plot_h))
    return ('<svg viewBox="0 0 %d %d" role="img" '
            'aria-label="Таймлайн расхода сессии">%s</svg>'
            % (width, height, "".join(parts)))


def _cold_write_cell(r):
    """$ стоимость записи кэша этого события остывшего кэша, токены — вторым
    числом в скобках. Деньги — основная видимая цифра, как и везде в отчёте
    (см. Finding 2, финальное ревью); если модель вне прайса, остаются
    только токены.
    """
    comps = pricing.cost_components(r)
    tok = fmt.tokens(r["cache_write"])
    if comps is None:
        return tok
    return "%s (%s)" % (fmt.money(comps["cache_write"]), tok)


def _session_body(session):
    ramp = session["ramp"]
    rows = session["rows"]
    cold = analyze.find_cold_cache(rows)
    cold_rows = "".join(
        "<tr><td>%s</td><td class='num'>%.0f мин</td><td class='num'>%s</td>"
        "</tr>" % (_when(r["ts"]), (r["gap"] or 0) / 60, _cold_write_cell(r))
        for r in cold[:20])
    cost_usd = session.get("cost_usd")
    cards = [
        ("Итого по сессии", fmt.money(cost_usd) if cost_usd is not None else "—"),
        ("Токены входа", fmt.tokens(ramp["entry_cost"])),
        ("За первые 5 минут", fmt.tokens(ramp["window_total"])),
        ("Запросов в окне", str(ramp["n_requests"])),
        ("Субагентов", str(ramp["n_subagents"])),
        ("Токенов субагентов", fmt.tokens(ramp["subagent_tokens"])),
    ]
    cards_html = "".join(
        '<div class="card"><div class="k">%s</div><div class="v">%s</div></div>'
        % (html.escape(k), html.escape(v)) for k, v in cards)
    return """
<h2>Разгон сессии</h2>
<div class="cards">%s</div>

<h2>Таймлайн</h2>
%s
<p class="note">Серые полосы — простои дольше 5 минут. Красные столбики —
переписывание остывшего кэша (пауза оказалась длиннее времени жизни кэша:
контекст пришлось залить заново и оплатить повторно). Зелёная линия —
процент 5-часового лимита, фиолетовая — процент недельного.</p>

<h2>Остывший кэш в этой сессии</h2>
<div class="scroll"><table>
<tr><th>Когда</th><th class="num">Пауза</th><th class="num">Переписано</th></tr>
%s</table></div>
""" % (cards_html, _timeline_svg(rows, session["limits"]),
       cold_rows or "<tr><td colspan='3'>Не найдено</td></tr>")


def render_report(conn, out_path, since=None, until=None, session_id=None, debug=False):
    payload = build_payload(conn, since=since, until=until, session_id=session_id, debug=debug)
    o = payload["overview"]
    subtitle = "Период: %s — %s" % (_when(o["period"]["first_ts"]),
                                    _when(o["period"]["last_ts"]))
    eyebrow = "Claude Code · отчёт"
    if "session" in payload:
        body = _session_body(payload["session"])
        sess_row = conn.execute("SELECT * FROM sessions WHERE session_id = ?",
                                (session_id,)).fetchone()
        meta_row = conn.execute("SELECT * FROM session_meta WHERE session_id = ?",
                                (session_id,)).fetchone()
        name, _source = analyze.session_display_name(
            dict(sess_row) if sess_row else {"session_id": session_id, "project": None, "first_ts": None},
            dict(meta_row) if meta_row else None)
        subtitle = "Сессия «%s»" % name
        eyebrow = "Claude Code · сессия"
    else:
        body = _overview_body(o, debug, payload.get("weights"), payload.get("db_total_requests"),
                              payload["limit_summary"], payload["session_limit_pct"],
                              payload["session_limit_pct_field"],
                              payload["limit_events"],
                              payload["limit_stale_rows"], payload["limit_curve"])
    header = ('<div class="eyebrow">%s</div>\n<h1>Расход Claude Code</h1>\n'
             '<div class="sub">%s</div>\n'
             '<div class="disclaimer">Расчётная оценка по публичному прайсу на %s — '
             'не выписка по факту оплаты и может отличаться от реального счёта.</div>'
             % (html.escape(eyebrow), html.escape(subtitle), html.escape(payload["prices_as_of"])))
    doc = (HTML_TEMPLATE
           .replace("__HEADER__", header)
           .replace("__BODY__", body)
           .replace("__PAYLOAD__",
                    json.dumps(payload, ensure_ascii=False, default=str)
                    .replace("</", "<\\/")))
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(doc, encoding="utf-8")
    return out_path
