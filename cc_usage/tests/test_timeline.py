import render
import store


def _seed(conn):
    rows = [dict(request_id="r%d" % i, message_id="m%d" % i, session_id="s1",
                 ts=1755500000 + i * 30, model="claude-sonnet-5", effort=None,
                 service_tier=None, input=0, output=10, thinking=0,
                 cache_read=900, cache_write=1000, cache_1h=1000, cache_5m=0,
                 is_sidechain=0, agent_file=None, project="p", cwd=None,
                 git_branch=None, cc_version=None) for i in range(8)]
    rows.append(dict(rows[0], request_id="rx", message_id="mx",
                     ts=1755500000 + 9000, cache_write=900000, cache_1h=900000))
    store.insert_requests(conn, rows)
    store.insert_ratelimits(conn, [
        dict(ts=1755500000, session_id="s1", five_hour_pct=10.0,
             five_hour_reset=0, seven_day_pct=1.0, seven_day_reset=0,
             ctx_pct=1.0, ctx_in=1, cost_usd=0.0),
        dict(ts=1755509000, session_id="s1", five_hour_pct=30.0,
             five_hour_reset=0, seven_day_pct=2.0, seven_day_reset=0,
             ctx_pct=2.0, ctx_in=2, cost_usd=0.0)])
    store.rebuild_sessions(conn)


def test_timeline_marks_cold_cache_and_limit_curve(tmp_path):
    conn = store.connect(tmp_path / "u.sqlite")
    _seed(conn)
    out = render.render_report(conn, tmp_path / "t.html", session_id="s1")
    html = out.read_text(encoding="utf-8")
    assert "<svg" in html
    assert "остывш" in html.lower()
    assert "спайк" not in html.lower()
    assert "полилиния" in html.lower() or "polyline" in html.lower()
    assert "150" in html                 # пауза 150 минут упомянута


def test_timeline_without_limits_still_renders(tmp_path):
    conn = store.connect(tmp_path / "u.sqlite")
    _seed(conn)
    conn.execute("DELETE FROM ratelimits")
    conn.commit()
    out = render.render_report(conn, tmp_path / "t.html", session_id="s1")
    assert "<svg" in out.read_text(encoding="utf-8")


def test_timeline_single_request_does_not_divide_by_zero(tmp_path):
    conn = store.connect(tmp_path / "u.sqlite")
    store.insert_requests(conn, [dict(
        request_id="r", message_id="m", session_id="s2", ts=1755500000,
        model="claude-sonnet-5", effort=None, service_tier=None, input=0, output=1,
        thinking=0, cache_read=0, cache_write=10, cache_1h=0, cache_5m=0,
        is_sidechain=0, agent_file=None, project="p", cwd=None,
        git_branch=None, cc_version=None)])
    out = render.render_report(conn, tmp_path / "t.html", session_id="s2")
    assert "<svg" in out.read_text(encoding="utf-8")


def test_timeline_cold_cache_bar_is_actually_red(tmp_path):
    """Столбик остывшего кэша обязан быть красным (var(--warn))."""
    conn = store.connect(tmp_path / "u.sqlite")
    rows = [dict(request_id="r%d" % i, message_id="m%d" % i, session_id="s1",
                 ts=1755500000 + i * 30, model="claude-sonnet-5", effort=None,
                 service_tier=None, input=0, output=10, thinking=0,
                 cache_read=900, cache_write=1000, cache_1h=1000, cache_5m=0,
                 is_sidechain=0, agent_file=None, project="p", cwd=None,
                 git_branch=None, cc_version=None) for i in range(8)]
    rows.append(dict(rows[0], request_id="rx", message_id="mx",
                     ts=1755500000 + 210 + 9000, cache_write=900000, cache_1h=900000))
    store.insert_requests(conn, rows)
    store.rebuild_sessions(conn)
    out = render.render_report(conn, tmp_path / "t.html", session_id="s1")
    html = out.read_text(encoding="utf-8")
    assert 'fill="var(--warn)"' in html
    assert "пауза 150 мин" in html


def test_session_subtitle_shows_human_name(tmp_path):
    """Подзаголовок разбора сессии должен показывать её имя, а не id[:8]."""
    conn = store.connect(tmp_path / "u.sqlite")
    _seed(conn)
    store.set_session_title(conn, "s1", "some human name", "ai-title", 1755500000)
    out = render.render_report(conn, tmp_path / "t.html", session_id="s1")
    html = out.read_text(encoding="utf-8")
    assert "some human name" in html
