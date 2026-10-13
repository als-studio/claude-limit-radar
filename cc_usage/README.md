# cc_usage — мониторинг расхода токенов Claude Code

Собирает статистику API-запросов из JSONL-логов `~/.claude/projects/`, дедуплицирует по `(request_id, message_id)` и складывает в SQLite. Показывает расход в долларах: куда ушли деньги, что можно исправить (остывший кэш после простоя, дорогая модель, цена размера контекста, субагенты), самые дорогие сессии по имени.

## Команды

```bash
python cc_usage/cli.py collect   # инкрементальный сбор из JSONL + замеров лимитов
python cc_usage/cli.py explain   # текстовая сводка для модели (в начало контекста)
python cc_usage/cli.py report    # HTML-отчёт в $: расход, диагнозы, дорогие сессии, график
python cc_usage/cli.py export --out bundle.json   # выгрузка для передачи (анонимизация по умолчанию)
python cc_usage/cli.py import bundle.json         # принять чужую выгрузку
python cc_usage/cli.py backfill-titles   # разовый проход по JSONL за именами сессий
```

Сводка `explain` печатается в UTF-8 — при запуске из скриптов/планировщика задавай `PYTHONUTF8=1`.

## Окно анализа

`--since`/`--until` принимают `2h`, `90m`, `3d`, `2w`, ISO-дату (`2026-08-19`)
или ISO-время (`2026-08-19T14:00`), в локальном времени:

```bash
python cc_usage/cli.py report --since 2h
python cc_usage/cli.py report --since 2026-08-19 --until 2026-08-20
```

`--days N` остаётся синонимом `--since Nd`. `--debug` показывает отладочные
разделы (попадания в кэш, перевод в проценты лимита).

## Где лежат данные

- База: `~/.claude/cc-usage/usage.sqlite`
- Лог сбора: `~/.claude/cc-usage/collect.log`
- Отчёт: `~/.claude/cc-usage/report.html`
- Замеры лимитов: `~/.claude/cc-usage/ratelimits-<дата>.ndjson` (вчерашние файлы удаляются при сборе)

## Автоматический сбор

**Windows:** задача Task Scheduler (имя любое), лаунчер `start-cc-usage.ps1` (раз в 15 минут).

**Linux:** cron:

```
*/15 * * * * cd /path/to/cc-usage && python3 cc_usage/cli.py collect >/dev/null 2>&1
```

## Statusline на новой машине

Хук в `~/.claude/settings.json` — показывает расход/лимит в строке состояния Claude Code:

```json
"statusLine": {
  "type": "command",
  "command": "python /path/to/cc-usage/cc_usage/statusline.py",
  "refreshInterval": 60
}
```

`command` — абсолютный путь к `statusline.py` в копии репозитория; строка состояния работает сразу — данные лимитов пишутся с первого вызова хука, `collect` только переносит их в базу.

## Как передать выгрузку

```bash
python cc_usage/cli.py export --out bundle.json      # имена проектов и веток анонимизируются
python cc_usage/cli.py export --out bundle.json --no-anonymize   # если нужны оригинальные имена
```

Получатель: `python cc_usage/cli.py import bundle.json` — данные сольются в его базу.
