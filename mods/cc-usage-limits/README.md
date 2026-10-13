# cc-usage-limits

Мод Claude Code (плагин с hooks module). После каждого хода основного потока и
при сдвиге лимита на 1 п.п. (`session.measure`) дописывает строку с процентами
окон `five_hour` / `seven_day` в

```
~/.claude/cc-usage/limit-samples/<session_id>.ndjson
```

Файл свой у каждой сессии. Формат строки тот же, что у ndjson от statusline,
поэтому `cc_usage collect` разбирает его тем же парсером:

```json
{"ts": 1760000000.5, "session": "<id>", "five_hour_pct": 23.5, "five_hour_reset": 1791817200,
 "seven_day_pct": 7, "seven_day_reset": 1792022400, "fresh": 1, "source": "mod"}
```

Те же цифры чаще раза в 60 секунд не пишутся. Пустой `rateLimits` (не
подписка, до первого ответа) не пишется. Работает и там, где statusline не
вызывается: панель VS Code, `claude -p`.

## Опрос лимита аккаунта раз в 10 минут

Если задан путь к `cli.py`, мод на старте сессии (через 2 с) и дальше раз в 10
минут, пока сессия жива, запускает

```
<python> <cli_path> poll-limits --min-interval 540
```

`poll-limits` берёт OAuth-токен из `~/.claude/.credentials.json`, делает
`GET /api/oauth/usage` и пишет строку в `usage_snapshots`. Гейт
`--min-interval 540` отсекает дубли: при нескольких открытых сессиях запрос
уходит не чаще раза в 9 минут. Таймаут процесса — 60 с; любой сбой
(нет python, нет файла, таймаут) только пишется в debug-лог (`claude --debug`),
сессию он не ломает. После `/clear` таймер продолжает идти; перезагрузка мода
перезапускает его.

Настройки (`userConfig`):

| Поле | Что это | По умолчанию |
| --- | --- | --- |
| `cli_path` | абсолютный путь к `cc_usage/cli.py` | пусто — опрос выключен |
| `python` | интерпретатор: имя в PATH или полный путь | `python` |

Для мода из `--plugin-dir` / `CLAUDE_CODE_PLUGIN_DIRS` значения лежат в
`~/.claude/settings.json`:

```json
"pluginConfigs": {
  "cc-usage-limits": {
    "options": {
      "cli_path": "C:\\Projects\\cc-usage\\cc_usage\\cli.py",
      "python": "python"
    }
  }
}
```

Их же можно поменять в `/config` — мод перезагрузится с новыми значениями.

Минимальная версия Claude Code — **2.1.287**.

## Попробовать (одна сессия)

```
claude --plugin-dir C:\Projects\cc-usage\mods\cc-usage-limits
```

Где флаг не передать (панель VS Code, десктоп), путь задаётся переменной
`CLAUDE_CODE_PLUGIN_DIRS`: в окружении процесса или в блоке `env` файла
`~/.claude/settings.json`. Несколько путей разделяются `;` на Windows и `:` на
Linux.

## Поставить насовсем

Добавить репозиторий как маркетплейс из папки и установить мод:

```
claude plugin marketplace add C:\Projects\cc-usage
claude plugin install cc-usage-limits@<имя маркетплейса>
```

Для этого в корне репо нужен `.claude-plugin/marketplace.json` с записью
`{"name": "cc-usage-limits", "source": "./mods/cc-usage-limits"}`. Его пока нет,
и эта установка не проверялась. Мод из папки-маркетплейса читается прямо из
папки: после правки хватает `/reload-plugins`.

## Проверка

```
claude plugin validate mods/cc-usage-limits
claude plugin test mods/cc-usage-limits
```
