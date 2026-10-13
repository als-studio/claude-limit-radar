import limit_probe


def test_acquire_lock_blocks_second_caller(tmp_path):
    outdir = str(tmp_path)
    assert limit_probe.acquire_lock(outdir, max_age=60) is True
    assert limit_probe.acquire_lock(outdir, max_age=60) is False
    limit_probe.release_lock(outdir)
    assert limit_probe.acquire_lock(outdir, max_age=60) is True


def test_acquire_lock_clears_stale_lock(tmp_path):
    import os
    import time

    outdir = str(tmp_path)
    assert limit_probe.acquire_lock(outdir, max_age=60) is True
    old = time.time() - 3600
    os.utime(limit_probe._lock_path(outdir), (old, old))
    assert limit_probe.acquire_lock(outdir, max_age=60) is True


# --- политика «слать ли ping» ---------------------------------------------
#
# Зонд стоит денег: каждый ping — реальный запрос к модели, который тратит
# лимит аккаунта. Поэтому решение вынесено в чистую функцию: её видно целиком
# и она проверяется без запуска claude.

MIN = 900          # не чаще раза в 15 минут
MAX = 4 * 3600     # но не реже раза в 4 часа


def test_probes_on_first_run_when_never_probed():
    ok, _ = limit_probe.should_probe(now=1000.0, last_probe_ts=None,
                                     last_request_ts=None,
                                     min_interval=MIN, max_interval=MAX,
                                     if_active=True)
    assert ok is True


def test_refuses_before_min_interval_even_with_fresh_activity():
    """Серия ответов подряд не должна порождать шквал пингов."""
    now = 10000.0
    ok, reason = limit_probe.should_probe(now=now, last_probe_ts=now - 60,
                                          last_request_ts=now - 5,
                                          min_interval=MIN, max_interval=MAX,
                                          if_active=True)
    assert ok is False
    assert "рано" in reason


def test_probes_when_new_requests_appeared_since_last_probe():
    now = 10000.0
    ok, reason = limit_probe.should_probe(now=now, last_probe_ts=now - MIN - 1,
                                          last_request_ts=now - 100,
                                          min_interval=MIN, max_interval=MAX,
                                          if_active=True)
    assert ok is True
    assert "активность" in reason


def test_stays_silent_when_no_activity_since_last_probe():
    """Главная экономия: ночью, когда лимит не двигается, зонд молчит."""
    now = 10000.0
    ok, reason = limit_probe.should_probe(now=now, last_probe_ts=now - MIN - 1,
                                          last_request_ts=now - MIN - 500,
                                          min_interval=MIN, max_interval=MAX,
                                          if_active=True)
    assert ok is False
    assert "активности нет" in reason


def test_probes_after_max_interval_even_without_activity():
    """Страховка от расхода по каналу, которого не видно в транскриптах."""
    now = 100000.0
    ok, reason = limit_probe.should_probe(now=now, last_probe_ts=now - MAX - 1,
                                          last_request_ts=now - MAX - 5000,
                                          min_interval=MIN, max_interval=MAX,
                                          if_active=True)
    assert ok is True
    assert "контрольный" in reason


def test_ignores_activity_gate_when_disabled():
    now = 10000.0
    ok, _ = limit_probe.should_probe(now=now, last_probe_ts=now - MIN - 1,
                                     last_request_ts=None,
                                     min_interval=MIN, max_interval=MAX,
                                     if_active=False)
    assert ok is True


# --- команды tmux ----------------------------------------------------------
#
# Зонд живёт в одной долгой сессии: обвязка (системный промпт, инструменты)
# пишется в кэш один раз, а каждый следующий ping читает её оттуда. Прежний
# вариант — новый pty на каждый запуск — платил полную запись кэша 96 раз
# в сутки и выжигал недельный лимит Pro за 2.6 суток.


def test_claude_argv_disables_mcp_servers():
    """Цена зонда не должна зависеть от того, какие MCP подключил человек."""
    argv = limit_probe.claude_argv("claude-haiku-4-5-20251001")
    assert "--strict-mcp-config" in argv
    assert '{"mcpServers":{}}' in argv
    assert argv[argv.index("--model") + 1] == "claude-haiku-4-5-20251001"


def test_start_session_argv_launches_detached_in_given_cwd():
    argv = limit_probe.start_session_argv("cc-probe", "/home/u/work", "haiku")
    assert argv[:2] == ["tmux", "new-session"]
    assert "-d" in argv
    assert argv[argv.index("-s") + 1] == "cc-probe"
    assert argv[argv.index("-c") + 1] == "/home/u/work"
    assert "claude" in argv[-1]


def test_send_ping_argv_targets_named_session():
    argv = limit_probe.send_ping_argv("cc-probe", "ping")
    assert argv == ["tmux", "send-keys", "-t", "cc-probe", "ping", "Enter"]


DAY = 86400


def test_restarts_when_session_is_missing():
    need, _ = limit_probe.needs_restart(alive=False, age_seconds=None,
                                        max_age=DAY)
    assert need is True


def test_keeps_live_young_session():
    """Ради этого всё и затевалось — сессия должна переживать запуски cron."""
    need, _ = limit_probe.needs_restart(alive=True, age_seconds=3600,
                                        max_age=DAY)
    assert need is False


def test_restarts_session_older_than_max_age():
    """Граница `needs_restart` проверяется на суточном max_age — сам порог
    произвольный, важно лишь поведение на границе. Продовое значение
    (PROBE_MAX_SESSION_AGE в cli.py) — неделя: замерено на проде Павла, что
    ping добавляет ~44 токена в историю, то есть за неделю контекст растёт
    линейно и некритично (~30% от окна в 200k), а не «в разы», как
    предполагалось до замера."""
    need, reason = limit_probe.needs_restart(alive=True, age_seconds=DAY + 1,
                                             max_age=DAY)
    assert need is True
    assert "стар" in reason


def test_session_age_read_from_tmux_created_epoch():
    age = limit_probe.session_age_from_created("1788230000\n", now=1788233600.0)
    assert age == 3600


def test_session_age_unknown_when_tmux_answered_nothing():
    """Нечитаемый ответ tmux не должен выглядеть как «сессии ноль секунд» —
    иначе она никогда не пересоздастся."""
    assert limit_probe.session_age_from_created("", now=1788233600.0) is None
    assert limit_probe.session_age_from_created("мусор", now=1788233600.0) is None


class FakeTmux:
    """Подставной tmux: отвечает по сценарию и запоминает вызовы.

    Настоящий tmux в тестах не нужен — проверяется решение «поднимать ли
    сессию», а не то, умеет ли tmux её поднимать.
    """

    def __init__(self, alive, created=None):
        self.alive = alive
        self.created = created
        self.commands = []

    def __call__(self, argv):
        self.commands.append(argv[1])
        cmd = argv[1]
        if cmd == "has-session":
            return (0 if self.alive else 1), ""
        if cmd == "display-message":
            return 0, str(self.created)
        if cmd == "new-session":
            self.alive = True
        if cmd == "kill-session":
            self.alive = False
        return 0, ""


NOW = 1788233600.0


def test_ensure_session_starts_it_when_missing():
    tmux = FakeTmux(alive=False)
    started, _ = limit_probe.ensure_session("cc-probe", "/w", "haiku",
                                            max_age=DAY, now=NOW, run=tmux)
    assert started is True
    assert "new-session" in tmux.commands


def test_ensure_session_leaves_live_young_session_alone():
    """Если сессию пересоздавать на каждый запуск, вся экономия исчезнет."""
    tmux = FakeTmux(alive=True, created=int(NOW) - 3600)
    started, _ = limit_probe.ensure_session("cc-probe", "/w", "haiku",
                                            max_age=DAY, now=NOW, run=tmux)
    assert started is False
    assert "new-session" not in tmux.commands


def test_ensure_session_recreates_session_older_than_max_age():
    tmux = FakeTmux(alive=True, created=int(NOW) - DAY - 60)
    started, _ = limit_probe.ensure_session("cc-probe", "/w", "haiku",
                                            max_age=DAY, now=NOW, run=tmux)
    assert started is True
    assert tmux.commands.index("kill-session") < tmux.commands.index("new-session")


# --- готовность сессии ------------------------------------------------------
#
# Ping уходит вслепую через `tmux send-keys`, поэтому важно не что на экране
# нет чего-то плохого, а что на нём есть рабочий экран. Чёрный список
# («нет диалога доверия») на бою оказался недостаточным: у реального
# пользователя сессия застряла на выборе темы, и Enter от send-keys ответил
# бы и на него. Признак готовности — наша же статусная строка.

# Панели ниже сняты с боевых машин, не придуманы.

READY_PANE = """❯ Try "fix lint errors"
─────────
  Haiku 4.5      Auto-update failed"""

READY_PANE_WITH_LIMITS = """❯
  Haiku 4.5 | ctx 21% | 5h 18.0% | 7d 15.0%"""

TRUST_PANE = """ Quick safety check: Is this a project you created or one you trust?
 ❯ 1. Yes, I trust this folder
   2. No, exit
 Enter to confirm · Esc to cancel"""

THEME_PANE = """ ❯ 4. Light mode (colorblind-friendly)
   5. Dark mode (ANSI colors only)
 1 function greet() {"""

REWIND_PANE = """ Rewind
 Nothing to rewind to yet.
 Esc to exit"""


def test_prompt_without_limits_is_ready():
    """Свежая сессия ещё не знает rate_limits — статусная строка показывает
    только модель. Ждать в ней 5h означало бы вечную блокировку: проценты
    появляются лишь после первого ответа API, а его-то ping и делает."""
    assert limit_probe.session_ready(READY_PANE) is True


def test_prompt_with_limits_is_ready():
    assert limit_probe.session_ready(READY_PANE_WITH_LIMITS) is True


def test_trust_dialog_is_not_ready():
    assert limit_probe.session_ready(TRUST_PANE) is False


def test_theme_picker_is_not_ready():
    assert limit_probe.session_ready(THEME_PANE) is False


def test_rewind_panel_is_not_ready():
    """Панель без приглашения ввода: Enter от send-keys ушёл бы в неё."""
    assert limit_probe.session_ready(REWIND_PANE) is False


def test_unreadable_pane_is_not_ready():
    assert limit_probe.session_ready("") is False
    assert limit_probe.session_ready(None) is False


def test_pane_tail_keeps_last_meaningful_lines():
    assert "5. Dark mode (ANSI colors only)" in limit_probe.pane_tail(THEME_PANE)
    assert limit_probe.pane_tail("") == ""


# --- каденс против TTL кэша -------------------------------------------------
#
# Обвязка сессии живёт в кэше час. Ping внутри этого часа читает её (33 токена
# записи), ping позже — переписывает целиком (24 000). Замерено на боевом
# аккаунте: холодный сдвинул пятичасовое окно на 21 п.п., четыре тёплых подряд
# не сдвинули вовсе. Поэтому каденс обязан быть заметно короче TTL.

CACHE_TTL = 3600


def test_default_min_interval_leaves_room_inside_cache_ttl():
    """Дефолт должен обеспечивать несколько пингов внутри часа: если ping
    приходит раз в TTL или реже, кэш всегда остывший и экономии нет."""
    assert limit_probe.DEFAULT_MIN_INTERVAL * 3 <= CACHE_TTL


def test_default_min_interval_survives_cron_tick_arriving_a_second_early():
    """Cron с шагом 15 минут даёт ровно 900 с между тактами, и дрожание в одну
    секунду отбрасывало каждый второй запуск: в логе «слишком рано (899 с из
    900)». Дефолт 900 против шага 900 — гонка, пойманная на бою.
    """
    ok, _ = limit_probe.should_probe(
        now=899.0, last_probe_ts=0.0, last_request_ts=None,
        min_interval=limit_probe.DEFAULT_MIN_INTERVAL,
        max_interval=MAX, if_active=False)
    assert ok is True


def test_default_max_interval_stays_inside_cache_ttl():
    """Верхний порог — это и есть шаг пингов в простое. Дефолт 4 часа при
    часовом TTL гарантировал, что каждый контрольный замер приходит на
    остывший кэш: на бою это стоило ~21 п.п. пятичасового окна за замер,
    в двадцать раз дороже тёплого."""
    assert limit_probe.DEFAULT_MAX_INTERVAL < CACHE_TTL
