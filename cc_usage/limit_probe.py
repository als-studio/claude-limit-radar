"""Пробник лимита: ping в долгоживущую сессию claude внутри tmux.

Зачем вообще зонд: headless `claude -p` не вызывает хук statusLine и не
отдаёт rate_limits в своём JSON-ответе (проверено на боевом сервере).
rate_limits приходит только в stdin статуслайна, а тот запускается по
событиям интерактивной сессии. Значит нужна настоящая интерактивная сессия.

Почему одна долгая, а не новая на каждый запуск (как было до 2026-09-01):
каждая новая сессия заново пишет в кэш всю обвязку — системный промпт,
определения инструментов, CLAUDE.md, скиллы. Замерено: 41 599 токенов записи
на холодном старте против 232 на следующем ping в той же сессии, то есть в
180 раз меньше. Прежний вариант с новым pty каждые 15 минут выжигал недельный
лимит Claude Pro за 2.6 суток и при этом не оставлял следов в транскриптах —
процесс добивался SIGKILL раньше, чем сессия успевала записаться на диск.

Сессия живёт в tmux, поэтому переживает завершение процесса из cron.
POSIX-only по факту наличия tmux; сам модуль импортируется где угодно.
"""
import os
import shlex
import subprocess
import time


def _lock_path(outdir):
    return os.path.join(outdir, "probe.lock")


def acquire_lock(outdir, max_age):
    """True — лок взят, можно пробовать. False — уже идёт другой запуск.

    Протухший лок (старше max_age — предыдущий запуск завис) снимается сам.
    """
    path = _lock_path(outdir)
    try:
        age = time.time() - os.path.getmtime(path)
        if age < max_age:
            return False
        os.remove(path)
    except OSError:
        pass
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
        return True
    except FileExistsError:
        return False


def release_lock(outdir):
    try:
        os.remove(_lock_path(outdir))
    except OSError:
        pass


# Обвязка сессии (системный промпт, инструменты) живёт в кэше промпта час.
# Ping внутри этого часа читает её из кэша — 33 токена записи; ping позже
# переписывает целиком — 24 000. Замерено на боевом Pro-аккаунте: холодный
# ping сдвинул пятичасовое окно на 21 п.п., четыре тёплых подряд не сдвинули
# вовсе. Отсюда жёсткое правило: ping либо внутри TTL, либо не ping вообще —
# редкий замер обходится дороже частого.
CACHE_TTL = 3600

# Шаг cron у нас 15 минут = ровно 900 с. Дефолт 900 отбрасывал каждый второй
# такт по недобору в секунду («слишком рано (899 с из 900)» в логе), поэтому
# порог заметно ниже шага.
DEFAULT_MIN_INTERVAL = 600

# Верхний порог — это шаг пингов в простое, и он обязан остаться внутри TTL.
# Прежние 4 часа делали каждый контрольный замер холодным.
DEFAULT_MAX_INTERVAL = 2400


def should_probe(now, last_probe_ts, last_request_ts, min_interval,
                 max_interval, if_active):
    """Слать ли ping. Возвращает (решение, причина), без побочных эффектов.

    Зонд не бесплатен: каждый ping — настоящий запрос к модели, который
    тратит лимит аккаунта. Поэтому решение принимается по данным, а не по
    расписанию: пока новой активности нет, лимит не двигается и мерить
    нечего. Контрольный замер раз в max_interval всё равно делается — расход
    бывает и по каналу, которого в транскриптах не видно (claude.ai, другая
    машина), и на него триггер по активности не сработает никогда.
    """
    if last_probe_ts is None:
        return True, "первый запуск"
    idle = now - last_probe_ts
    if idle < min_interval:
        return False, "слишком рано (%d с из %d)" % (idle, min_interval)
    if not if_active:
        return True, "по расписанию"
    if last_request_ts is not None and last_request_ts > last_probe_ts:
        return True, "новая активность"
    if idle >= max_interval:
        return True, "контрольный замер (%d с без активности)" % idle
    return False, "активности нет"


TMUX = "tmux"
EMPTY_MCP = '{"mcpServers":{}}'


def claude_argv(model):
    """Команда claude для долгоживущей сессии зонда.

    MCP отключены намеренно: определения инструментов — самая крупная и самая
    изменчивая часть контекста, из-за неё цена зонда зависела бы от того, что
    человек себе подключил. Зонду инструменты не нужны — он шлёт «ping».
    """
    return ["claude", "--model", model, "--strict-mcp-config",
            "--mcp-config", EMPTY_MCP]


def start_session_argv(name, cwd, model):
    """tmux new-session -d: сессия переживает завершение процесса из cron.

    cwd должен быть уже доверенной Claude Code папкой — иначе сессия упрётся
    в диалог workspace trust и до rate_limits не дойдёт.
    """
    return [TMUX, "new-session", "-d", "-s", name, "-c", cwd,
            shlex.join(claude_argv(model))]


def send_ping_argv(name, prompt):
    return [TMUX, "send-keys", "-t", name, prompt, "Enter"]


def session_age_from_created(text, now):
    """Возраст сессии в секундах из '#{session_created}' tmux, либо None.

    None означает «возраст неизвестен»: лучше оставить живую сессию, чем
    платить лишний холодный старт из-за нечитаемого ответа.
    """
    try:
        created = int((text or "").strip())
    except (TypeError, ValueError):
        return None
    return now - created


def needs_restart(alive, age_seconds, max_age):
    """Пересоздавать ли сессию зонда. Возвращает (решение, причина).

    Сессию держим долго ради кэша, но не вечно: каждый ping добавляет в
    историю ~130 токенов, за сутки это +12-15 тысяч. За неделю чтение кэша
    выросло бы в разы, а по дороге сработал бы auto-compact — сам по себе
    дорогая операция.
    """
    if not alive:
        return True, "сессии нет"
    if age_seconds is not None and age_seconds >= max_age:
        return True, "сессия старше %d ч" % (max_age // 3600)
    return False, "сессия жива"


def _run(argv):
    """(код возврата, stdout). Сбой запуска — не исключение, а ненулевой код:
    зонд по cron не должен падать трейсбеком из-за отсутствующего tmux.
    """
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return 1, ""
    return p.returncode, p.stdout


def tmux_available(run=_run):
    return run([TMUX, "-V"])[0] == 0


def session_alive(name, run=_run):
    return run([TMUX, "has-session", "-t", name])[0] == 0


def session_age(name, now, run=_run):
    rc, out = run([TMUX, "display-message", "-p", "-t", name,
                   "#{session_created}"])
    if rc != 0:
        return None
    return session_age_from_created(out, now)


def ensure_session(name, cwd, model, max_age, now, run=_run):
    """Поднимает сессию зонда, если её нет или она слишком стара.

    Возвращает (создана_ли_заново, причина). «Заново» значит, что следующий
    ping заплатит полную запись кэша — поэтому это происходит раз в сутки,
    а не на каждый запуск.
    """
    alive = session_alive(name, run=run)
    age = session_age(name, now, run=run) if alive else None
    need, reason = needs_restart(alive, age, max_age)
    if not need:
        return False, reason
    if alive:
        run([TMUX, "kill-session", "-t", name])
    ok = run(start_session_argv(name, cwd, model))[0] == 0
    return ok, reason


def send_ping(name, prompt="ping", run=_run):
    return run(send_ping_argv(name, prompt))[0] == 0


PROMPT_CHAR = "❯"   # ❯ — приглашение ввода Claude Code


def session_ready(pane_text):
    """Готова ли сессия принимать ping: виден ли ввод, а не диалог.

    Проверка белым списком, а не чёрным — и это принципиально. Ping уходит
    вслепую через send-keys, а Enter отвечает на любой открытый диалог. На
    бою поймано три разных экрана: доверие к каталогу, выбор темы, панель
    Rewind. Перечислять их заранее бесполезно — список задаёт Claude Code и
    он меняется между версиями.

    Признак — строка приглашения, за которой НЕ пункт меню: диалоги рисуют
    выбор тем же символом («❯ 1. Yes, I trust this folder»), поэтому одного
    приглашения мало.

    Статусная строка на признак не годится, хотя напрашивается: проценты
    лимита в ней появляются только после первого ответа API, а его и делает
    ping — ждать их означало бы вечную блокировку свежей сессии (поймано на
    бою). Не годится и «есть проценты» как дополнительное условие: модалка
    поверх экрана со статусной строкой прошла бы такую проверку.
    """
    for row in (pane_text or "").splitlines():
        _, sep, tail = row.partition(PROMPT_CHAR)
        if not sep:
            continue
        rest = tail.strip()
        if not rest or not rest[0].isdigit():
            return True
    return False


def pane_tail(pane_text, lines=6):
    """Последние непустые строки панели — чтобы в логе cron было видно, на
    чём именно застряла сессия, а не только что она не готова."""
    rows = [r.rstrip() for r in (pane_text or "").splitlines() if r.strip()]
    return "\n".join("    " + r for r in rows[-lines:])


def capture_pane(name, run=_run):
    rc, out = run([TMUX, "capture-pane", "-t", name, "-p"])
    return out if rc == 0 else ""
