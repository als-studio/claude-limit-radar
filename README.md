# claude-limit-radar (cc-usage)

Local usage analytics for [Claude Code](https://claude.com/claude-code): where
the tokens, dollars and rate-limit percent went. Reads the JSONL logs Claude
Code already writes, stores them in SQLite, and builds a self-contained HTML
report with diagnoses ("what burned the limit and what to change").

Python 3.9+, **standard library only**, no network except one optional call
(see below). Documentation is in Russian; the code identifiers are in English.

## Quick start

```bash
python cc_usage/cli.py collect                          # logs -> SQLite (idempotent)
python cc_usage/cli.py report --since 7d --out report.html
python cc_usage/cli.py explain --since 24h              # text summary
```

Data lives in `~/.claude/cc-usage/usage.sqlite`. Nothing leaves the machine.
Full setup (Windows scheduler, cron, multi-user servers): [INSTALL.md](INSTALL.md).
Command reference: [cc_usage/README.md](cc_usage/README.md).

## Why a mod for rate limits

The rate-limit percent is not in the logs. The classic source is the
`statusLine` hook, but Claude Code does not call it in the VS Code chat panel,
in `claude -p` or in the Agent SDK. On one real machine that left **95% of
requests without any limit reading**.

[`mods/cc-usage-limits`](mods/cc-usage-limits/README.md) is a Claude Code mod
(plugin with a hooks module, Claude Code 2.1.286+ tested) that:

1. writes a reading after every model response (`session.measure`) in every
   interface, one NDJSON file per session, picked up by `collect`;
2. optionally polls the account every 10 minutes (`poll-limits`,
   `GET /api/oauth/usage` with your own OAuth token) so idle periods and usage
   from other clients still show up as a rise in the series.

The limit is shared by the whole account, so a rise between two readings is
split between sessions by tokens spent in the interval. That is an estimate,
and the report labels it as one.

## Privacy

`export` anonymizes by default. `report.html` and `explain` are **not**
anonymized: they contain session titles and project paths. Do not publish them.

## Tests

```bash
python -m pytest cc_usage/tests -q     # pytest is the only dev dependency
```

## License

MIT, see [LICENSE](LICENSE).
