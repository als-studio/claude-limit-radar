# cc-usage — установка и отчёт: плейбук

Пошаговая инструкция для двух сценариев:

- **A. Локальная машина** (Windows/Linux/macOS) — cc-usage анализирует
  собственные логи Claude Code текущего пользователя.
- **B. Сервер с несколькими пользователями** (Linux) — код разворачивается
  один раз в общее место, отчёт строится отдельно по каждому аккаунту.

Требования в обоих случаях: **Python 3.9+**, сам Claude Code уже
использовался на машине (есть `~/.claude/projects` с `.jsonl`-логами).
Сторонних зависимостей нет — только stdlib.

---

## A. Локальная машина, один пользователь

Код уже получен (`git clone`/скачан) — команды ниже запускаются из корня
репозитория.

### 1. Собрать данные

```bash
python cc_usage/cli.py collect
```

Читает `~/.claude/projects/**/*.jsonl` (на Windows — `%USERPROFILE%\.claude`),
пишет в `~/.claude/cc-usage/usage.sqlite`. Идемпотентно: повторный запуск не
дублирует запросы (ключ дедупликации — `request_id` + `message_id`).

### 2. Получить отчёт

```bash
python cc_usage/cli.py report --since 7d --out report.html
```

Открыть `report.html` в браузере — файл самодостаточен (без CDN и сети).

Форматы `--since`/`--until`: относительные `2h`, `90m`, `3d`, `2w` **или**
ISO-дата/время `2026-08-19`, `2026-08-19T14:00`. **`today` не работает** —
для «с начала сегодняшнего дня по местному времени машины» указывать
сегодняшнюю ISO-дату явно, например `--since 2026-08-21`.

Без `--out` отчёт печатается текстовой сводкой в терминал — быстрая проверка,
что данные вообще есть, без открытия HTML.

### 3. Быстрая текстовая сводка (для себя или для модели)

```bash
python cc_usage/cli.py explain --since 24h
```

Диагнозы «куда ушли деньги» и что можно исправить — без открытия отчёта.

### 4. (опционально) Автоматизировать сбор

Разовый `collect` не обновляется сам — на боевой машине лучше повесить на
расписание.

**Windows (Task Scheduler):** использовать `start-cc-usage.ps1` из корня
репозитория как действие задачи, интервал — раз в 15 минут. Скрипт сам
выставляет `PYTHONUTF8=1` (без этого планировщик может подхватить не-UTF8
консоль и уронить процесс на кириллице).

**Linux/macOS (cron):**

```cron
*/15 * * * * PYTHONUTF8=1 /usr/bin/python3 /path/to/cc-usage/cc_usage/cli.py collect >> ~/.claude/cc-usage/collect.log 2>&1
```

### 5. (опционально) Статистика лимита через statusLine

Отдельно от `collect` — сбор `%` пятичасового/недельного лимита требует
statusLine-хука, который Claude Code дёргает во время живой сессии.

Прописать в `~/.claude/settings.json` (Windows —
`%USERPROFILE%\.claude\settings.json`), **не перезаписывая остальные ключи**:

```json
"statusLine": {
  "type": "command",
  "command": "python /path/to/cc-usage/cc_usage/statusline.py",
  "refreshInterval": 60
}
```

`refreshInterval` обязателен — без него хук вызывается только по событиям, а
простой сессии (когда лимит и «утекает» незаметно) — это как раз отсутствие
событий. `statusline.py` автономен: не импортирует пакет, не открывает
SQLite, вызывается раз в секунду и не должен тормозить — только stdlib.

Эффект виден сразу: замена дефолтной статусной строки на
`Модель | ctx NN% | 5h NN.N% | 7d NN.N%`. После этого `collect` начнёт
засчитывать «замеров лимита: N > 0» в логе — данные копятся в
`~/.claude/cc-usage/ratelimits-YYYYMMDD.ndjson` и попадают в отчёт.

---

## B. Сервер с несколькими пользователями Claude Code (Linux)

Сценарий: на сервере несколько человек, каждый работает под своим unix-логином,
у каждого свой `~/.claude`. Нужно: один раз развернуть код, потом снимать
отчёт по любому из аккаунтов, не трогая их окружение сверх необходимого.

### 1. Узнать, кто на сервере вообще пользуется Claude Code

```bash
awk -F: '($3>=1000 && $3<60000) || $7 ~ /sh$/' /etc/passwd   # обычные логины
for u in <кандидаты>; do
  echo "=== $u ==="; ls -d /home/$u/.claude/projects 2>&1
done
```

Наличие `~/.claude/projects` с непустым содержимым — признак реальной
активности.

### 2. Развернуть код в общее место

Один экземпляр кода, читаемый (но не обязательно писать) всеми:

```bash
mkdir -p /opt/cc-usage
# перенести содержимое репозитория, например архивом:
git archive --format=tar HEAD | ssh root@<host> 'tar xf - -C /opt/cc-usage'
ssh root@<host> "chown -R root:root /opt/cc-usage && chmod -R a+rX /opt/cc-usage"
```

`chmod a+rX` обязателен — иначе `statusline.py`/`cli.py` не запустятся из-под
других логинов (по умолчанию `git archive` восстанавливает права `644`/`755`
только для владельца-группы, что на сервере с разными Unix-пользователями
недостаточно).

### 3. Собрать данные и построить отчёт по каждому пользователю

Ключевой момент: `cc_usage` определяет, чьи логи читать, через переменную
`CLAUDE_CONFIG_DIR` (по умолчанию — `~/.claude` того, кто запускает). Флаг
`--db` — **до** подкоманды (`cli.py --db ... collect`, не наоборот).

Два варианта:

**Вариант 1 — сам пользователь запускает под своим логином** (проще,
предпочтительно, если у него есть shell-доступ): база пишется по умолчанию в
его же `~/.claude/cc-usage/usage.sqlite`, `--db` не нужен.

```bash
su - <user> -c 'python3 /opt/cc-usage/cc_usage/cli.py collect'
su - <user> -c 'python3 /opt/cc-usage/cc_usage/cli.py report --since 2026-08-21 --out ~/report-today.html'
```

**Вариант 2 — аудит от root, не трогая домашний каталог пользователя**:
явно указать `CLAUDE_CONFIG_DIR` на чужой `.claude` (root может читать чужие
файлы), а `--db`/`--out` — в свою рабочую директорию, чтобы ничего не
дописывать в чужой `$HOME`:

```bash
mkdir -p /root/cc-usage-data /root/cc-usage-reports
for u in <user1> <user2> ...; do
  CLAUDE_CONFIG_DIR=/home/$u/.claude PYTHONUTF8=1 python3 /opt/cc-usage/cc_usage/cli.py \
    --db /root/cc-usage-data/$u.sqlite collect
  PYTHONUTF8=1 python3 /opt/cc-usage/cc_usage/cli.py \
    --db /root/cc-usage-data/$u.sqlite report --since 2026-08-21 \
    --out /root/cc-usage-reports/$u-today.html
done
```

Вариант 2 ничего не пишет в `/home/<user>` — удобно для разового аудита,
результат легко удалить целиком.

### 4. Забрать отчёты локально

```bash
scp root@<host>:/root/cc-usage-reports/*.html ./reports/
```

### 5. (опционально) Статистика лимита через statusLine — для всех сразу

**Меняет то, что пользователь видит внизу терминала** (см. шаг A.5) — на
сервере с реальными людьми сначала стоит спросить их/владельца сервера, а не
включать молча.

Патчить `settings.json` руками рискованно — файл большой, легко сломать JSON
или затереть чужие ключи (`permissions.allow`, `hooks` и т.д.). Правильный
способ — точечный merge, не полная перезапись:

```python
# add_statusline.py <путь-к-settings.json>
import json, os, shutil, sys
path = sys.argv[1]
with open(path, encoding="utf-8") as f:
    data = json.load(f)
if "statusLine" in data:
    print("уже настроен:", path); sys.exit(0)
st = os.stat(path)
shutil.copy2(path, path + ".bak-cc-usage")   # бэкап перед правкой
data["statusLine"] = {
    "type": "command",
    "command": "python3 /opt/cc-usage/cc_usage/statusline.py",
    "refreshInterval": 60,
}
tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=2); f.write("\n")
os.chown(tmp, st.st_uid, st.st_gid)   # сохранить владельца — иначе Claude Code
os.chmod(tmp, st.st_mode)             # у пользователя не увидит свой конфиг
os.replace(tmp, path)
print("OK:", path)
```

```bash
for u in <user1> <user2> ...; do
  python3 add_statusline.py /home/$u/.claude/settings.json
done
```

Проверить, что JSON не сломан и владелец файла не съехал на root:

```bash
for u in <user1> <user2> ...; do
  python3 -c "import json; print(json.load(open('/home/$u/.claude/settings.json'))['statusLine'])"
  ls -l /home/$u/.claude/settings.json
done
```

Изменение подхватится в живой сессии на следующем срабатывании хука —
перезапуск Claude Code не обязателен.

---

## Приватность и что не публиковать

- `cc_usage/cli.py export` анонимизирует данные **по умолчанию** — это
  единственный безопасный способ поделиться выгрузкой наружу
  (`--no-anonymize` — осознанное решение, не значение по умолчанию).
- `report.html`/`explain` — **не анонимизированы**: реальные названия
  сессий, пути проектов, иногда фрагменты личных/клиентских данных в
  заголовках. Не публиковать как есть (Artifact, публичный чат, тикет) — эти
  файлы для тех, чьи данные там есть.
- На многопользовательском сервере — данные каждого читаются из его же
  `~/.claude`, но собранный `.sqlite`/`.html` может физически лежать в другом
  месте (см. вариант 2, шаг B.3) — считать этот файл настолько же
  чувствительным, как исходные логи.

## Диагностика типичных проблем

| Симптом | Причина | Что делать |
|---|---|---|
| `unrecognized arguments: --db ...` | `--db` указан после подкоманды | `cli.py --db <path> collect`, не `cli.py collect --db <path>` |
| `не распознан формат времени: 'today'` | `today` не поддерживается | использовать `2026-08-21` (ISO) или `24h`/`7d` |
| `В выбранном окне нет запросов` | у пользователя правда не было активности в это окно, либо неверный `CLAUDE_CONFIG_DIR` | расширить `--since`/`--until`, проверить `CLAUDE_CONFIG_DIR` |
| `collect` пишет «замеров лимита 0» всегда | `statusLine`-хук не настроен | шаг A.5 / B.5 |
| Отчёт пустой при непустой `~/.claude/projects` | `collect` не запускался после сбора логов | сначала `collect`, потом `report` |
