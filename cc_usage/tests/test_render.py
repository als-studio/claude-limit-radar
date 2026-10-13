import re

import analyze
import render
import store


def _seed(conn):
    rows = [dict(request_id="r%d" % i, message_id="m%d" % i, session_id="s1",
                 ts=1755500000 + i * 10, model="claude-opus-5", effort=None,
                 service_tier=None, input=1, output=5, thinking=0,
                 cache_read=900, cache_write=1000, cache_1h=1000, cache_5m=0,
                 is_sidechain=0, agent_file=None, project="secretproject",
                 cwd="C:/secret/path", git_branch="secret-branch",
                 cc_version="2.1") for i in range(8)]
    store.insert_requests(conn, rows)
    store.rebuild_sessions(conn)


def test_report_is_self_contained(tmp_path):
    conn = store.connect(tmp_path / "u.sqlite")
    _seed(conn)
    out = render.render_report(conn, tmp_path / "r.html")
    html = out.read_text(encoding="utf-8")
    assert not re.search(r'(src|href)\s*=\s*["\']https?://', html)
    assert "<svg" in html
    assert "secretproject" in html


def test_report_on_empty_db_says_no_data(tmp_path):
    conn = store.connect(tmp_path / "u.sqlite")
    out = render.render_report(conn, tmp_path / "r.html")
    html = out.read_text(encoding="utf-8")
    assert "нет данных" in html.lower()


def test_payload_has_sections(tmp_path):
    conn = store.connect(tmp_path / "u.sqlite")
    _seed(conn)
    p = render.build_payload(conn)
    for key in ("overview", "generated_at"):
        assert key in p


def test_report_shows_money_not_raw_tokens(tmp_path):
    conn = store.connect(tmp_path / "u.sqlite")
    _seed(conn)
    out = render.render_report(conn, tmp_path / "r.html")
    html = out.read_text(encoding="utf-8")
    assert "$" in html
    assert "оценка по публичному прайсу" in html.lower()


def test_report_never_says_spike(tmp_path):
    conn = store.connect(tmp_path / "u.sqlite")
    _seed(conn)
    out = render.render_report(conn, tmp_path / "r.html")
    html = out.read_text(encoding="utf-8")
    assert "спайк" not in html.lower()


def test_report_shows_cold_cache_wording(tmp_path):
    conn = store.connect(tmp_path / "u.sqlite")
    _seed(conn)
    out = render.render_report(conn, tmp_path / "r.html")
    html = out.read_text(encoding="utf-8")
    assert "остывш" in html.lower()      # "остывший кэш" где-то в тексте


def test_report_hides_debug_sections_by_default(tmp_path):
    conn = store.connect(tmp_path / "u.sqlite")
    _seed(conn)
    out = render.render_report(conn, tmp_path / "r.html")
    html = out.read_text(encoding="utf-8")
    assert "перевод в проценты лимита" not in html.lower()
    assert "попаданий в кэш" not in html.lower()


def test_report_shows_debug_sections_with_flag(tmp_path):
    conn = store.connect(tmp_path / "u.sqlite")
    _seed(conn)
    out = render.render_report(conn, tmp_path / "r.html", debug=True)
    html = out.read_text(encoding="utf-8")
    assert "перевод в проценты лимита" in html.lower()


def test_report_shows_unpriced_line(tmp_path):
    conn = store.connect(tmp_path / "u.sqlite")
    rows = [dict(request_id="r1", message_id="m1", session_id="s1", ts=1755500000,
                model="k3", effort=None, service_tier=None, input=1, output=5,
                thinking=0, cache_read=0, cache_write=0, cache_1h=0, cache_5m=0,
                is_sidechain=0, agent_file=None, project="p", cwd=None,
                git_branch=None, cc_version=None)]
    store.insert_requests(conn, rows)
    store.rebuild_sessions(conn)
    out = render.render_report(conn, tmp_path / "r.html")
    html = out.read_text(encoding="utf-8")
    assert "не учтено" in html.lower()


def test_report_since_hours_window_renders(tmp_path):
    conn = store.connect(tmp_path / "u.sqlite")
    _seed(conn)
    # since считается от фиксированной метки фикстуры (не от time.time()) — иначе
    # тест зависит от того, насколько реальные часы разошлись с датой сида
    # (см. Task 10 review, Fix 4): все 8 строк сида лежат в
    # [1755500000, 1755500070], окно начинается за час до первой.
    since = 1755500000 - 3600
    out = render.render_report(conn, tmp_path / "r.html", since=since)
    html = out.read_text(encoding="utf-8")
    assert "<svg" in html


def test_render_report_wires_limit_curve_into_spend_chart(tmp_path):
    # Регрессия: build_payload() считал limit_curve корректно, но
    # render_report() не прокидывал payload["limit_curve"] дальше в
    # _overview_body — полотно с линией молча пустовало на реальных отчётах.
    conn = store.connect(tmp_path / "u.sqlite")
    _seed(conn)
    store.insert_ratelimits(conn, [
        dict(ts=1755500000 + i * 10, session_id="s1", five_hour_pct=float(i),
             five_hour_reset=0, seven_day_pct=float(i), seven_day_reset=0,
             ctx_pct=0.0, ctx_in=0, cost_usd=0.0, fresh=1)
        for i in range(8)])
    out = render.render_report(conn, tmp_path / "r.html")
    assert "polyline" in out.read_text(encoding="utf-8")


def test_spend_chart_draws_limit_line_when_given():
    buckets = [{"ts": 1755500000 + i * 3600, "cost": 1.0} for i in range(4)]
    without = render._spend_chart(buckets, "time")
    assert "polyline" not in without

    with_line = render._spend_chart(buckets, "time", limit_pct=[None, 10.0, 20.0, 30.0])
    assert "polyline" in with_line
    assert "правая ось" in with_line


def test_spend_chart_bars_stay_within_viewbox_for_long_history():
    # Task 10 review, Fix 1: bw floor 1.5px раньше расталкивал бары за пределы
    # фиксированного viewBox при большом числе корзин (дневные корзины на
    # истории дольше ~305 дней) без всякого визуального сигнала об этом.
    #
    # Round-1 фикс (bw floor 0.3 + фиксированный шаг bw+1) закрывал это лишь
    # частично: с фиксированным шагом суммарная ширина всё равно росла
    # неограниченно вместе с n, порог просто отодвинулся с ~305 до ~593
    # корзин ("тот же баг, дальше по оси n"). Round-2 фикс выводит и шаг, и
    # ширину бара из plot_w / n — тогда x ограничен конструктивно для
    # ЛЮБОГО n, а не до какого-то порога. Проверяем на n порядков за пределами
    # обоих старых порогов, чтобы не просто отодвинуть эту проверку туда же.
    width = 780
    for n in (400, 600, 2_000, 50_000):
        buckets = [{"ts": 1755500000 + i * 86400, "cost": 1.0} for i in range(n)]
        svg = render._spend_chart(buckets, "day", width=width)
        rects = re.findall(r'<rect x="([\d.]+)" y="[\d.]+" width="([\d.]+)"', svg)
        assert len(rects) == n
        rightmost_right_edge = max(float(x) + float(w) for x, w in rects)
        assert rightmost_right_edge <= width


def test_insufficient_diagnosis_note_uses_russian_title_not_raw_id(tmp_path):
    conn = store.connect(tmp_path / "u.sqlite")
    _seed(conn)
    out = render.render_report(conn, tmp_path / "r.html")
    html = out.read_text(encoding="utf-8")
    raw_ids = {"cold_cache", "expensive_model", "context_cost", "subagents"}
    titles = re.findall(
        r'<div class="diag-title" style="color:var\(--muted\)">([^<]+)</div>', html)
    assert titles          # в этом фикстуре есть недостаточные диагнозы
    assert not (set(titles) & raw_ids)


# --- Finding 1: "Оценка устранимого" не может быть больше "Расход" -------------

def _row(request_id, ts, cache_write, session_id="s1", model="claude-opus-5",
        cache_read=0, cache_1h=0, cache_5m=0):
    return dict(request_id=request_id, message_id=request_id, session_id=session_id,
               ts=ts, model=model, effort=None, service_tier=None, input=0, output=0,
               thinking=0, cache_read=cache_read, cache_write=cache_write,
               cache_1h=cache_1h, cache_5m=cache_5m, is_sidechain=0, agent_file=None,
               project="p", cwd=None, git_branch=None, cc_version=None)


def test_avoidable_total_capped_at_total_spend(tmp_path):
    # Д1 (остывший кэш) и Д2 (дорогая модель) считают устранимое частично по
    # одним и тем же токенам: дорогая модель, у которой заодно остыл кэш.
    # Наивная сумма их avoidable_usd может превысить общий расход окна —
    # карточка "Оценка устранимого" не должна показывать больше, чем "Расход".
    conn = store.connect(tmp_path / "u.sqlite")
    base_ts = 1755500000
    base_rows = [_row("b%d" % i, base_ts + i * 10, 100) for i in range(6)]
    cold_row = _row("cold", base_rows[-1]["ts"] + 4000, 50000)
    store.insert_requests(conn, base_rows + [cold_row])
    store.rebuild_sessions(conn)

    o = analyze.overview(conn)
    diag_by_id = {d["id"]: d for d in o["diagnoses"]}
    assert "cold_cache" in diag_by_id and "expensive_model" in diag_by_id
    naive_sum = (diag_by_id["cold_cache"]["avoidable_usd"]
                + diag_by_id["expensive_model"]["avoidable_usd"])
    # подтверждаем, что сценарий действительно даёт double-counting
    assert naive_sum > o["cost"]["total"]

    out = render.render_report(conn, tmp_path / "r.html")
    html_out = out.read_text(encoding="utf-8")
    spend = re.search(r'<div class="k">Расход \(оценка\)</div><div class="v">([^<]+)</div>', html_out).group(1)
    avoidable = re.search(
        r'<div class="k">Можно сэкономить</div>.*?<div class="v">(.*?)</div>',
        html_out, re.DOTALL).group(1)
    # avoidable ограничен total (см. Finding 1) — деньги те же, доля капается на 100%
    assert spend in avoidable
    assert "100.0%" in avoidable
    assert "могут пересекаться" in html_out.lower()


# --- Finding 2: разбор сессии показывает $, а не только токены ---------------

def test_session_report_shows_dollar_total_and_no_misleading_label(tmp_path):
    conn = store.connect(tmp_path / "u.sqlite")
    _seed(conn)
    out = render.render_report(conn, tmp_path / "r.html", session_id="s1")
    html_out = out.read_text(encoding="utf-8")
    assert re.search(r'<div class="k">Итого по сессии</div><div class="v">\$[\d.]', html_out)
    assert "Токены входа" in html_out
    assert "Стоимость входа" not in html_out


def test_session_cold_cache_table_shows_money(tmp_path):
    conn = store.connect(tmp_path / "u.sqlite")
    base_ts = 1755500000
    base_rows = [_row("b%d" % i, base_ts + i * 10, 100) for i in range(6)]
    cold_row = _row("cold", base_rows[-1]["ts"] + 4000, 50000)
    store.insert_requests(conn, base_rows + [cold_row])
    store.rebuild_sessions(conn)
    out = render.render_report(conn, tmp_path / "r.html", session_id="s1")
    html_out = out.read_text(encoding="utf-8")
    m = re.search(
        r'<tr><th>Когда</th><th class="num">Пауза</th><th class="num">Переписано</th></tr>(.*?)</table>',
        html_out, re.DOTALL)
    assert m is not None
    cold_table = m.group(1)
    assert "Не найдено" not in cold_table   # событие остывшего кэша реально попало в таблицу
    assert "$" in cold_table


# --- Finding 3: средний размер контекста (Д3) --------------------------------

def test_context_cost_diagnosis_shows_avg_context(tmp_path):
    conn = store.connect(tmp_path / "u.sqlite")
    _seed(conn)
    out = render.render_report(conn, tmp_path / "r.html")
    html_out = out.read_text(encoding="utf-8")
    assert "в среднем" in html_out.lower()
    assert "контекста на запрос" in html_out.lower()


def test_sessions_by_cost_table_has_avg_context_column(tmp_path):
    conn = store.connect(tmp_path / "u.sqlite")
    _seed(conn)
    out = render.render_report(conn, tmp_path / "r.html")
    html_out = out.read_text(encoding="utf-8")
    assert '<th class="num">Ср. контекст</th>' in html_out


# --- Finding 4: пустое ОКНО отчёта — не то же самое, что пустая БАЗА ---------

def test_report_empty_window_differs_from_empty_db(tmp_path):
    conn = store.connect(tmp_path / "u.sqlite")
    _seed(conn)
    # окно далеко в будущем: в базе есть данные, но не в этом окне
    out = render.render_report(conn, tmp_path / "r.html", since=1755500000 + 10**7)
    html_out = out.read_text(encoding="utf-8").lower()
    assert "база пуста" not in html_out
    assert "collect" not in html_out
    assert "окне" in html_out


def test_payload_has_limit_summary_without_debug_flag(tmp_path):
    conn = store.connect(tmp_path / "u.sqlite")
    _seed(conn)
    p = render.build_payload(conn)   # debug=False по умолчанию
    assert "limit_summary" in p
    assert p["limit_summary"]["snapshot"] is None   # в _seed нет ratelimits
    assert p["session_limit_pct_field"] in ("five_hour_pct", "seven_day_pct")
    assert p["session_limit_pct"] == {}


def test_report_shows_limit_card_no_data(tmp_path):
    conn = store.connect(tmp_path / "u.sqlite")
    _seed(conn)   # в _seed нет ratelimits
    out = render.render_report(conn, tmp_path / "r.html")
    html_out = out.read_text(encoding="utf-8")
    assert '<div class="k">Лимит</div><div class="v">нет данных</div>' in html_out


def test_report_shows_limit_snapshot_and_spike_note(tmp_path):
    # ВАЖНО: каждый growth-интервал засчитывается limit_intervals только при
    # непустых requests-токенах внутри (t0, t1] (см. analyze.py — иначе
    # "проценты выросли, а запросов нет, данные не сшиваются"). 100 фоновых
    # интервалов без соответствующих requests-строк дали бы confidence "none",
    # а не "medium" — на этом уже спотыкался черновик этого теста.
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
    # доминирующий скачок — отдельная сессия, должна выиграть атрибуцию Д←5
    store.insert_ratelimits(conn, [
        dict(ts=1755700000, session_id="spike", five_hour_pct=0.0, five_hour_reset=0,
             seven_day_pct=0.0, seven_day_reset=0, ctx_pct=0.0, ctx_in=0, cost_usd=0.0),
        dict(ts=1755700100, session_id="spike", five_hour_pct=50.0, five_hour_reset=0,
             seven_day_pct=0.0, seven_day_reset=0, ctx_pct=0.0, ctx_in=0, cost_usd=0.0),
    ])
    store.insert_requests(conn, [dict(
        request_id="sp1", message_id="sp1", session_id="spike", ts=1755700050,
        model="claude-opus-5", effort=None, service_tier=None, input=0, output=0,
        thinking=0, cache_read=0, cache_write=5_000_000, cache_1h=0, cache_5m=0,
        is_sidechain=0, agent_file=None, project="p", cwd=None, git_branch=None,
        cc_version=None)])

    out = render.render_report(conn, tmp_path / "r.html")
    html_out = out.read_text(encoding="utf-8")
    assert re.search(r"5h \d", html_out)          # снэпшот отрисован числом, не "нет данных"
    assert "самый резкий одиночный скачок" in html_out.lower()


def test_limit_spike_note_shows_jump_and_leader_share_separately(tmp_path):
    """Величина скачка и доля лидера в нём — разные числа, оба видны.

    В test_report_shows_limit_snapshot_and_spike_note кандидат внутри
    события один, поэтому доля совпадает со скачком и подмена одного
    числа другим незаметна (финальное ревью limit-events).
    """
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
    # скачок +50 пп, а внутри интервала ДВА кандидата: 4M и 1M токенов,
    # значит на лидера приходится 40 пп, а не все 50
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

    out = render.render_report(conn, tmp_path / "r.html")
    html_out = out.read_text(encoding="utf-8")
    assert "самый резкий одиночный скачок +50.0 пп" in html_out
    assert "~40.0 пп" in html_out


# --- Fix wave 1 (финальное ревью limit-visibility) ---------------------------

def test_report_shows_limit_snapshot_when_window_has_no_requests(tmp_path):
    # AC1 не требует наличия requests в окне — только хотя бы одного замера
    # ratelimits. Мотивирующий кейс Павла: хук statusLine активен, окно
    # collect'а пустое (Fix 1, финальное ревью).
    conn = store.connect(tmp_path / "u.sqlite")
    _seed(conn)
    since = 1755500000 + 10**7      # окно далеко в будущем — requests сида в него не попадают
    store.insert_ratelimits(conn, [
        dict(ts=since + 100, session_id="s1", five_hour_pct=42.0, five_hour_reset=0,
             seven_day_pct=7.0, seven_day_reset=0, ctx_pct=0.0, ctx_in=0, cost_usd=0.0),
    ])
    out = render.render_report(conn, tmp_path / "r.html", since=since)
    html_out = out.read_text(encoding="utf-8")
    assert "окне нет запросов" in html_out.lower()   # подтверждает, что мы в ветке раннего возврата
    assert re.search(r"5h 42\.0%", html_out)
    # лента событий обязана быть и в ветке раннего возврата — ради этого
    # limit_events_html считается ДО развилки (Задача 5)
    assert "События лимита" in html_out


def test_limit_note_low_confidence_wording_does_not_say_no_breakdown(tmp_path):
    # Fix 2a: "недостаточно данных для разбивки" читалось как "разбивки нет
    # вовсе" — противоречит непустой колонке "% лимита" в таблице сессий.
    conn = store.connect(tmp_path / "u.sqlite")
    _seed(conn)
    store.insert_ratelimits(conn, [
        dict(ts=1755500000 + 5, session_id="s1", five_hour_pct=10.0, five_hour_reset=0,
             seven_day_pct=0.0, seven_day_reset=0, ctx_pct=0.0, ctx_in=0, cost_usd=0.0),
        dict(ts=1755500000 + 15, session_id="s1", five_hour_pct=12.0, five_hour_reset=0,
             seven_day_pct=0.0, seven_day_reset=0, ctx_pct=0.0, ctx_in=0, cost_usd=0.0),
    ])
    out = render.render_report(conn, tmp_path / "r.html")
    html_out = out.read_text(encoding="utf-8")
    assert "наблюдений мало" in html_out.lower()
    assert "недостаточно данных для разбивки" not in html_out.lower()


def test_sessions_table_has_limit_column_caveat(tmp_path):
    # Fix 2c: колонка "% лимита" получает краткую сноску о том, что это
    # оценка и не видит активность с других машин того же аккаунта.
    conn = store.connect(tmp_path / "u.sqlite")
    _seed(conn)   # ratelimits нет, но session_limit_pct_field вычисляется всегда
    out = render.render_report(conn, tmp_path / "r.html")
    html_out = out.read_text(encoding="utf-8")
    assert "% лимита — сумма наблюдённого прироста" in html_out


def test_build_payload_prefers_seven_day_pct_field_when_higher_confidence(tmp_path):
    # Fix 8.2: ветка build_payload, где session_limit_pct_field разрешается
    # в "seven_day_pct" (раньше проверялся только дефолтный five_hour-путь).
    conn = store.connect(tmp_path / "u.sqlite")
    rl_rows, req_rows = [], []
    for i in range(11):
        ts = 1755600000 + i * 10
        # five_hour_pct не растёт — 0 growth-интервалов, confidence "none".
        rl_rows.append(dict(ts=ts, session_id="s7", five_hour_pct=0.0, five_hour_reset=0,
                            seven_day_pct=float(i), seven_day_reset=0,
                            ctx_pct=0.0, ctx_in=0, cost_usd=0.0, fresh=1))
        if i > 0:
            req_rows.append(dict(request_id="s7%d" % i, message_id="s7%d" % i,
                                 session_id="s7", ts=ts - 5, model="claude-opus-5",
                                 effort=None, service_tier=None, input=0, output=0,
                                 thinking=0, cache_read=0, cache_write=1000,
                                 cache_1h=0, cache_5m=0, is_sidechain=0, agent_file=None,
                                 project="p", cwd=None, git_branch=None, cc_version=None))
    store.insert_ratelimits(conn, rl_rows)
    store.insert_requests(conn, req_rows)
    p = render.build_payload(conn)
    assert p["limit_summary"]["five_hour"]["weights"]["confidence"] == "none"
    assert p["limit_summary"]["seven_day"]["weights"]["confidence"] in ("low", "medium")
    assert p["session_limit_pct_field"] == "seven_day_pct"


def test_sessions_table_limit_column_shows_value_and_dash(tmp_path):
    # Fix 8.3: AC7 (колонка "% лимита" — заголовок, реальное значение,
    # "—" для сессий без данных) до этого проверялось только вручную.
    conn = store.connect(tmp_path / "u.sqlite")
    _seed(conn)
    store.insert_ratelimits(conn, [
        dict(ts=1755500000 + 5, session_id="s1", five_hour_pct=10.0, five_hour_reset=0,
             seven_day_pct=0.0, seven_day_reset=0, ctx_pct=0.0, ctx_in=0, cost_usd=0.0),
        dict(ts=1755500000 + 15, session_id="s1", five_hour_pct=15.0, five_hour_reset=0,
             seven_day_pct=0.0, seven_day_reset=0, ctx_pct=0.0, ctx_in=0, cost_usd=0.0),
    ])
    store.insert_requests(conn, [_row("q1", 1755500200, 100, session_id="s2")])
    store.rebuild_sessions(conn)
    out = render.render_report(conn, tmp_path / "r.html")
    html_out = out.read_text(encoding="utf-8")
    assert '<th class="num">% лимита, 5h</th>' in html_out
    assert "+5.0 пп" in html_out
    assert re.search(r"<td class='num'>—</td></tr>", html_out)


def test_limit_card_flags_stale_snapshot(tmp_path):
    """Нет свежих замеров в окне — карточка честно помечает давность.

    Fix wave 2, находка 4 (Codex): раньше карточка "Лимит" молча показывала
    последний по времени замер, даже если это застывшее эхо простаивающей
    сессии. Теперь limit_snapshot() возвращает такой замер с fresh=False —
    отчёт обязан это показать, а не проглотить.
    """
    conn = store.connect(tmp_path / "u.sqlite")
    store.insert_ratelimits(conn, [
        dict(ts=1000, session_id="idle", five_hour_pct=13.0, five_hour_reset=0,
             seven_day_pct=13.0, seven_day_reset=0, ctx_pct=0.0, ctx_in=0,
             cost_usd=0.0, fresh=0),
    ])
    out = render.render_report(conn, tmp_path / "r.html")
    html_out = out.read_text(encoding="utf-8")
    assert "возможно, устарело" in html_out.lower()


def test_limit_card_shows_usage_api_source_when_no_ratelimits(tmp_path):
    """Стадия B: statusLine ни разу не сработал (нет ratelimits вообще, база
    запросов тоже пуста), но poll /api/oauth/usage есть — отчёт всё равно
    показывает лимит и честно называет источник, а не молчит про него
    (раньше при пустом ratelimits карточка лимита говорила "нет данных")."""
    conn = store.connect(tmp_path / "u.sqlite")
    store.insert_usage_snapshots(conn, [
        dict(ts=1000, five_hour_pct=5.0, five_hour_reset=0,
             seven_day_pct=20.0, seven_day_reset=0,
             scoped_pct=None, scoped_model=None, scoped_reset=None,
             extra_enabled=None, extra_pct=None, severity=None, raw="{}")])
    out = render.render_report(conn, tmp_path / "r.html")
    html_out = out.read_text(encoding="utf-8")
    assert "через /api/oauth/usage" in html_out
    assert "5.0%" in html_out and "20.0%" in html_out


def test_report_shows_all_candidates_and_models_not_just_leader(tmp_path):
    """Лента события показывает всех кандидатов и модели, не только лидера.

    Fix wave 2, находка 9 (Codex): event_attribution() уже вычисляет
    полный список долей и models, но HTML показывал только лидера и
    текст "и ещё N" — пользователь не мог проверить атрибуцию и не видел
    один из заявленных факторов расхода (модель).
    """
    conn = store.connect(tmp_path / "u.sqlite")
    store.insert_ratelimits(conn, [
        dict(ts=1000, session_id="a", five_hour_pct=10.0, five_hour_reset=0,
             seven_day_pct=10.0, seven_day_reset=0, ctx_pct=0.0, ctx_in=0,
             cost_usd=0.0, fresh=1),
        dict(ts=2000, session_id="a", five_hour_pct=20.0, five_hour_reset=0,
             seven_day_pct=20.0, seven_day_reset=0, ctx_pct=0.0, ctx_in=0,
             cost_usd=0.0, fresh=1)])
    store.insert_requests(conn, [
        dict(request_id="r1", message_id="m1", session_id="a", ts=1500,
             model="claude-opus-5", effort=None, service_tier=None,
             input=0, output=0, thinking=0, cache_read=0, cache_write=3_000_000,
             cache_1h=0, cache_5m=0, is_sidechain=0, agent_file=None,
             project="p", cwd=None, git_branch=None, cc_version=None),
        dict(request_id="r2", message_id="m2", session_id="b", ts=1600,
             model="claude-haiku-4-5", effort=None, service_tier=None,
             input=0, output=0, thinking=0, cache_read=0, cache_write=1_000_000,
             cache_1h=0, cache_5m=0, is_sidechain=0, agent_file=None,
             project="p", cwd=None, git_branch=None, cc_version=None)])
    store.rebuild_sessions(conn)
    out = tmp_path / "r.html"
    render.render_report(conn, out)
    html_text = out.read_text(encoding="utf-8")
    assert "и ещё" not in html_text
    assert "claude-opus-5" in html_text
    assert "claude-haiku-4-5" in html_text


def test_limit_events_note_names_off_by_one_and_cache_read_caveats(tmp_path):
    """Отчёт честно называет два ограничения атрибуции, а не молчит о них.

    Fix wave 2, decisions-log: сдвиг атрибуции на один запрос и вес
    cache_read наравне с остальными токенами — находки, которые сознательно
    НЕ чинятся в этой волне (для точного фикса нет данных, которые неоткуда
    взять без отклонённой регрессии токены->%). Раз не чиним — обязаны
    сказать вслух, а не оставить как непроверяемое умолчание.
    """
    conn = store.connect(tmp_path / "u.sqlite")
    store.insert_ratelimits(conn, [
        dict(ts=1000, session_id="a", five_hour_pct=10.0, five_hour_reset=0,
             seven_day_pct=10.0, seven_day_reset=0, ctx_pct=0.0, ctx_in=0,
             cost_usd=0.0, fresh=1),
        dict(ts=2000, session_id="a", five_hour_pct=20.0, five_hour_reset=0,
             seven_day_pct=20.0, seven_day_reset=0, ctx_pct=0.0, ctx_in=0,
             cost_usd=0.0, fresh=1)])
    store.insert_requests(conn, [
        dict(request_id="r1", message_id="m1", session_id="a", ts=1500,
             model="claude-opus-5", effort=None, service_tier=None,
             input=0, output=0, thinking=0, cache_read=0, cache_write=1_000_000,
             cache_1h=0, cache_5m=0, is_sidechain=0, agent_file=None,
             project="p", cwd=None, git_branch=None, cc_version=None)])
    store.rebuild_sessions(conn)
    out = tmp_path / "r.html"
    render.render_report(conn, out)
    html_text = out.read_text(encoding="utf-8").lower()
    assert "соседн" in html_text          # сдвиг на соседний запрос/сессию
    assert "чтение кэша" in html_text and "наравне" in html_text


def test_report_shows_limit_events(tmp_path):
    conn = store.connect(tmp_path / "u.sqlite")
    store.insert_ratelimits(conn, [
        dict(ts=1000, session_id="a", five_hour_pct=10.0, five_hour_reset=0,
             seven_day_pct=10.0, seven_day_reset=0, ctx_pct=0.0, ctx_in=0,
             cost_usd=0.0, fresh=1),
        dict(ts=2000, session_id="a", five_hour_pct=20.0, five_hour_reset=0,
             seven_day_pct=20.0, seven_day_reset=0, ctx_pct=0.0, ctx_in=0,
             cost_usd=0.0, fresh=1)])
    store.insert_requests(conn, [
        dict(request_id="r1", message_id="m1", session_id="a", ts=1500,
             model="claude-opus-5", effort=None, service_tier=None,
             input=0, output=0, thinking=0, cache_read=0, cache_write=1_000_000,
             cache_1h=0, cache_5m=0, is_sidechain=0, agent_file=None,
             project="p", cwd=None, git_branch=None, cc_version=None)])
    store.rebuild_sessions(conn)
    out = tmp_path / "r.html"
    render.render_report(conn, out)
    html_text = out.read_text(encoding="utf-8")
    assert "События лимита" in html_text
    assert "+10.0 пп" in html_text


def test_report_names_elsewhere_events(tmp_path):
    """Сирота называет причину, а не 'сессий не найдено'."""
    conn = store.connect(tmp_path / "u.sqlite")
    store.insert_ratelimits(conn, [
        dict(ts=1000, session_id="a", five_hour_pct=10.0, five_hour_reset=0,
             seven_day_pct=10.0, seven_day_reset=0, ctx_pct=0.0, ctx_in=0,
             cost_usd=0.0, fresh=1),
        dict(ts=2000, session_id="a", five_hour_pct=20.0, five_hour_reset=0,
             seven_day_pct=20.0, seven_day_reset=0, ctx_pct=0.0, ctx_in=0,
             cost_usd=0.0, fresh=1)])
    out = tmp_path / "r.html"
    render.render_report(conn, out)
    assert "не с этой машины" in out.read_text(encoding="utf-8")


def test_report_without_events_explains_why(tmp_path):
    """Пустая лента объясняет причину, а не показывает пустой блок."""
    conn = store.connect(tmp_path / "u.sqlite")
    out = tmp_path / "r.html"
    render.render_report(conn, out)
    text = out.read_text(encoding="utf-8")
    assert "События лимита" in text
    assert "замеров лимита нет" in text


def test_report_shows_events_for_both_limit_fields(tmp_path):
    """Лента событий не выбирает одно поле по confidence — оба видны.

    Fix wave 2, находка 1 (Kimi + Codex, независимо): confidence у 5h почти
    всегда выше, чем у 7d (больше сбросов → больше интервалов), поэтому
    выбор "поле с большей confidence" на практике почти всегда даёт только
    5h-ленту — и недельный лимит, ради которого написан vision.md, в HTML
    не виден никогда. explain (cli.py) уже показывает оба поля; отчёт
    должен делать то же самое.
    """
    conn = store.connect(tmp_path / "u.sqlite")
    store.insert_ratelimits(conn, [
        dict(ts=1000, session_id="a", five_hour_pct=10.0, five_hour_reset=0,
             seven_day_pct=10.0, seven_day_reset=0, ctx_pct=0.0, ctx_in=0,
             cost_usd=0.0, fresh=1),
        dict(ts=2000, session_id="a", five_hour_pct=20.0, five_hour_reset=0,
             seven_day_pct=15.0, seven_day_reset=0, ctx_pct=0.0, ctx_in=0,
             cost_usd=0.0, fresh=1)])
    store.insert_requests(conn, [
        dict(request_id="r1", message_id="m1", session_id="a", ts=1500,
             model="claude-opus-5", effort=None, service_tier=None,
             input=0, output=0, thinking=0, cache_read=0, cache_write=1_000_000,
             cache_1h=0, cache_5m=0, is_sidechain=0, agent_file=None,
             project="p", cwd=None, git_branch=None, cc_version=None)])
    store.rebuild_sessions(conn)
    out = tmp_path / "r.html"
    render.render_report(conn, out)
    html_text = out.read_text(encoding="utf-8")
    assert "5h +10.0 пп" in html_text
    assert "7d +5.0 пп" in html_text


def test_report_warns_about_stale_resolution(tmp_path):
    """Данные до появления fresh имеют грубое разрешение — сказать прямо."""
    conn = store.connect(tmp_path / "u.sqlite")
    store.insert_ratelimits(conn, [
        dict(ts=1000, session_id="a", five_hour_pct=10.0, five_hour_reset=0,
             seven_day_pct=10.0, seven_day_reset=0, ctx_pct=0.0, ctx_in=0,
             cost_usd=0.0, fresh=None)])
    out = tmp_path / "r.html"
    render.render_report(conn, out)
    assert "грубое" in out.read_text(encoding="utf-8")


def test_session_timeline_shows_seven_day_line(tmp_path):
    conn = store.connect(tmp_path / "u.sqlite")
    _seed(conn)
    store.insert_ratelimits(conn, [
        dict(ts=1755500000, session_id="s1", five_hour_pct=10.0, five_hour_reset=0,
             seven_day_pct=20.0, seven_day_reset=0, ctx_pct=0.0, ctx_in=0, cost_usd=0.0),
        dict(ts=1755500050, session_id="s1", five_hour_pct=15.0, five_hour_reset=0,
             seven_day_pct=25.0, seven_day_reset=0, ctx_pct=0.0, ctx_in=0, cost_usd=0.0),
    ])
    out = render.render_report(conn, tmp_path / "r.html", session_id="s1")
    html_out = out.read_text(encoding="utf-8")
    assert 'stroke="var(--violet)"' in html_out
    assert "недельный лимит" in html_out.lower()
