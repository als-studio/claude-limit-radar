"""Замер лимита через GET /api/oauth/usage — без траты токенов.

Зачем: зонд `limit_probe` мерил лимит настоящим инференс-запросом и тратил
0,26-0,31 п.п. недельного лимита за пинг. Этот эндпоинт читает состояние
аккаунта на сервере — то же, что показывает `/usage` в самом Claude Code, —
и не двигает окна лимита.

Структура — как в `limit_probe.py`: чистые функции отдельно (тестируются без
сети), тонкий I/O отдельно.
"""
import datetime
import json
import re
import subprocess
import urllib.error
import urllib.request


def read_token(creds_text, now_ms):
    """(accessToken, None) либо (None, причина).

    Свой refresh токена не делаем (может разлогинить Claude Code) — протухший
    токен просто пропускает такт, а не чинится.
    """
    if creds_text is None:
        return None, "файл credentials.json не найден"
    try:
        data = json.loads(creds_text)
    except ValueError:
        return None, "credentials.json повреждён"
    oauth = data.get("claudeAiOauth")
    if not oauth:
        return None, "нет ключа claudeAiOauth в credentials.json"
    token = oauth.get("accessToken")
    if not token:
        return None, "нет accessToken в claudeAiOauth"
    expires_at = oauth.get("expiresAt")
    if expires_at is not None and expires_at < now_ms:
        return None, "токен протух"
    return token, None


def iso_to_unix(text):
    """ISO 8601 -> unix UTC (int), либо None, если строки нет или она мусор.

    Без временнОй зоны в строке (не встречалось на бою, но API не
    документирован) считаем её уже UTC — так же, как остальной ответ.
    """
    if not text:
        return None
    try:
        dt = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return int(dt.timestamp())


def _scoped_model(scoped):
    if not scoped:
        return None
    scope = scoped.get("scope") or {}
    model = scope.get("model") or {}
    return model.get("display_name")


def parse_usage(payload, ts):
    """Ответ /api/oauth/usage -> строка под USAGE_SNAPSHOT_FIELDS.

    Набор ключей в ответе нестабилен (в ответе уже видны кодовые имена
    нераскатанных фич), поэтому отсутствующее поле даёт NULL, а не
    исключение — а сырой ответ целиком сохраняется в raw.
    """
    payload = payload or {}
    five_hour = payload.get("five_hour") or {}
    seven_day = payload.get("seven_day") or {}
    limits = payload.get("limits") or []
    extra = payload.get("extra_usage") or {}

    scoped = next((l for l in limits if l.get("kind") == "weekly_scoped"), None)
    weekly_all = next((l for l in limits if l.get("kind") == "weekly_all"), None)

    return {
        "ts": ts,
        "five_hour_pct": five_hour.get("utilization"),
        "five_hour_reset": iso_to_unix(five_hour.get("resets_at")),
        "seven_day_pct": seven_day.get("utilization"),
        "seven_day_reset": iso_to_unix(seven_day.get("resets_at")),
        "scoped_pct": scoped.get("percent") if scoped else None,
        "scoped_model": _scoped_model(scoped),
        "scoped_reset": iso_to_unix(scoped.get("resets_at")) if scoped else None,
        "extra_enabled": int(bool(extra["is_enabled"])) if "is_enabled" in extra else None,
        "extra_pct": extra.get("utilization"),
        "severity": weekly_all.get("severity") if weekly_all else None,
        "raw": json.dumps(payload, ensure_ascii=False),
    }


def should_poll(now, last_ts, min_interval):
    """Слать ли запрос. (решение, причина), без побочных эффектов.

    В отличие от зонда `limit_probe`, гейта по активности нет — сам запрос
    бесплатен, экономить нечего, важно только не долбить эндпоинт чаще
    расписания (429 при частом опросе, ретраев нет).
    """
    if last_ts is None:
        return True, "первый запуск"
    idle = now - last_ts
    if idle < min_interval:
        return False, "слишком рано (%d с из %d)" % (idle, min_interval)
    return True, "по расписанию"


def build_headers(token, version):
    """User-Agent обязателен — без него запрос попадает в жёстко
    ограниченное ведро и получает вечный 429 (claude-code#31637, #30930)."""
    return {
        "Authorization": "Bearer %s" % token,
        "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": "claude-code/%s" % version,
        "Content-Type": "application/json",
    }


def _run(argv):
    """(код возврата, stdout). По образцу limit_probe._run: сбой запуска —
    не исключение, а ненулевой код."""
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return 1, ""
    return p.returncode, p.stdout


# Заголовок должен просто присутствовать и выглядеть как claude-code/*, точная
# версия в фолбэке не проверяется сервером отдельно — важно не остаться без
# User-Agent, если бинарь claude недоступен на машине.
FALLBACK_VERSION = "0.0.0"


def claude_version(run=_run):
    rc, out = run(["claude", "--version"])
    if rc != 0:
        return FALLBACK_VERSION
    m = re.match(r"(\S+)", out.strip())
    return m.group(1) if m else FALLBACK_VERSION


def fetch(url, headers, timeout):
    """(код ответа, тело). Сетевая ошибка — не исключение, а (0, "").

    Ретраев нет ни при каком коде ответа (риск 429 при частом опросе —
    долбить эндпоинт нельзя).
    """
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError):
        return 0, ""
