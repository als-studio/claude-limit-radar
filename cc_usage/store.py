"""Хранилище: схема, миграции, запись."""
import os
import sqlite3
from pathlib import Path

SCHEMA_VERSION = 3

REQUEST_FIELDS = [
    "request_id", "message_id", "session_id", "ts", "model", "effort",
    "service_tier", "input", "output", "thinking", "cache_read",
    "cache_write", "cache_1h", "cache_5m", "is_sidechain", "agent_file",
    "project", "cwd", "git_branch", "cc_version",
]

RATELIMIT_FIELDS = [
    "ts", "session_id", "five_hour_pct", "five_hour_reset",
    "seven_day_pct", "seven_day_reset", "ctx_pct", "ctx_in", "cost_usd",
    "fresh", "source",
]

USAGE_SNAPSHOT_FIELDS = [
    "ts", "five_hour_pct", "five_hour_reset",
    "seven_day_pct", "seven_day_reset",
    "scoped_pct", "scoped_model", "scoped_reset",
    "extra_enabled", "extra_pct", "severity", "raw",
]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    request_id TEXT, message_id TEXT, session_id TEXT, ts REAL,
    model TEXT, effort TEXT, service_tier TEXT,
    input INTEGER DEFAULT 0, output INTEGER DEFAULT 0, thinking INTEGER DEFAULT 0,
    cache_read INTEGER DEFAULT 0, cache_write INTEGER DEFAULT 0,
    cache_1h INTEGER DEFAULT 0, cache_5m INTEGER DEFAULT 0,
    is_sidechain INTEGER DEFAULT 0, agent_file TEXT,
    project TEXT, cwd TEXT, git_branch TEXT, cc_version TEXT,
    PRIMARY KEY (request_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_req_session ON requests(session_id, ts);
CREATE INDEX IF NOT EXISTS idx_req_ts ON requests(ts);

CREATE TABLE IF NOT EXISTS ratelimits (
    ts REAL, session_id TEXT,
    five_hour_pct REAL, five_hour_reset INTEGER,
    seven_day_pct REAL, seven_day_reset INTEGER,
    ctx_pct REAL, ctx_in INTEGER, cost_usd REAL,
    fresh INTEGER, source TEXT,
    PRIMARY KEY (session_id, ts)
);
CREATE INDEX IF NOT EXISTS idx_rl_ts ON ratelimits(ts);

CREATE TABLE IF NOT EXISTS usage_snapshots (
    ts REAL PRIMARY KEY,
    five_hour_pct REAL, five_hour_reset INTEGER,
    seven_day_pct REAL, seven_day_reset INTEGER,
    scoped_pct REAL, scoped_model TEXT, scoped_reset INTEGER,
    extra_enabled INTEGER, extra_pct REAL,
    severity TEXT,
    raw TEXT
);
CREATE INDEX IF NOT EXISTS idx_us_ts ON usage_snapshots(ts);

CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY, size INTEGER, mtime REAL,
    offset INTEGER DEFAULT 0, last_seen REAL
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY, project TEXT, cwd TEXT, first_ts REAL, last_ts REAL,
    models TEXT, n_requests INTEGER DEFAULT 0,
    input INTEGER DEFAULT 0, output INTEGER DEFAULT 0,
    cache_read INTEGER DEFAULT 0, cache_write INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS session_meta (
    session_id TEXT PRIMARY KEY,
    title TEXT,
    title_source TEXT,
    updated_at REAL
);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def config_dir():
    """Каталог Claude Code — одинаково работает на Windows и Linux."""
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(env) if env else Path.home() / ".claude"


def default_db_path():
    return config_dir() / "cc-usage" / "usage.sqlite"


def connect(db_path):
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    migrate(conn)
    return conn


def _ensure_column(conn, table, column, coltype):
    """ALTER TABLE ADD COLUMN идемпотентно — CREATE TABLE IF NOT EXISTS не
    добавляет колонку в уже существующую боевую таблицу.
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(%s)" % table)}
    if column not in cols:
        conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, column, coltype))


def migrate(conn):
    conn.executescript(_SCHEMA)
    _ensure_column(conn, "sessions", "cwd", "TEXT")
    # Боевая таблица уже существует — CREATE TABLE IF NOT EXISTS её не меняет.
    # NULL здесь значит "свежесть неизвестна", её восстанавливает анализ.
    _ensure_column(conn, "ratelimits", "fresh", "INTEGER")
    # Откуда замер: 'mod' — мод Claude Code (limit-samples/), NULL — хук
    # statusLine (он source не пишет).
    _ensure_column(conn, "ratelimits", "source", "TEXT")
    cur = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'")
    row = cur.fetchone()
    if row is None:
        conn.execute("INSERT INTO meta VALUES ('schema_version', ?)",
                     (str(SCHEMA_VERSION),))
    elif int(row[0]) < SCHEMA_VERSION:
        conn.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'",
                     (str(SCHEMA_VERSION),))
    conn.commit()


def _insert(conn, table, fields, rows):
    if not rows:
        return 0
    cols = ", ".join(fields)
    marks = ", ".join("?" for _ in fields)
    sql = "INSERT OR IGNORE INTO %s (%s) VALUES (%s)" % (table, cols, marks)
    before = conn.total_changes
    conn.executemany(sql, [[r.get(f) for f in fields] for r in rows])
    conn.commit()
    return conn.total_changes - before


def insert_requests(conn, rows):
    return _insert(conn, "requests", REQUEST_FIELDS, rows)


def insert_ratelimits(conn, rows):
    return _insert(conn, "ratelimits", RATELIMIT_FIELDS, rows)


def insert_usage_snapshots(conn, rows):
    return _insert(conn, "usage_snapshots", USAGE_SNAPSHOT_FIELDS, rows)


def last_request_ts(conn, exclude_cwd=None):
    """Время последнего запроса, либо None, если запросов нет.

    exclude_cwd убирает из сигнала собственные ping-и зонда лимита: он живёт
    в отдельном каталоге именно ради этого. Без исключения зонд видел бы свой
    же предыдущий ping как «новую активность» и запускал себя бесконечно.
    Строки без cwd (старые записи) остаются в сигнале — они точно не наши.
    """
    if exclude_cwd:
        row = conn.execute(
            "SELECT MAX(ts) FROM requests WHERE cwd IS NULL OR cwd <> ?",
            (str(exclude_cwd),)).fetchone()
    else:
        row = conn.execute("SELECT MAX(ts) FROM requests").fetchone()
    return row[0] if row else None


def get_meta(conn, key):
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(conn, key, value):
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                 (key, str(value)))
    conn.commit()


def get_file_state(conn, path):
    row = conn.execute(
        "SELECT size, mtime, offset FROM files WHERE path = ?", (str(path),)
    ).fetchone()
    return dict(row) if row else None


def set_file_state(conn, path, size, mtime, offset):
    import time
    conn.execute(
        "INSERT OR REPLACE INTO files (path, size, mtime, offset, last_seen)"
        " VALUES (?, ?, ?, ?, ?)",
        (str(path), size, mtime, offset, time.time()),
    )
    conn.commit()


def rebuild_sessions(conn):
    """Пересчитать сводку по сессиям из requests."""
    conn.execute("DELETE FROM sessions")
    conn.execute("""
        INSERT INTO sessions (session_id, project, cwd, first_ts, last_ts, models,
                              n_requests, input, output, cache_read, cache_write)
        SELECT session_id,
               MAX(project), MAX(cwd),
               MIN(ts), MAX(ts),
               GROUP_CONCAT(DISTINCT model),
               COUNT(*),
               SUM(input), SUM(output), SUM(cache_read), SUM(cache_write)
        FROM requests
        WHERE session_id IS NOT NULL
        GROUP BY session_id
    """)
    conn.commit()
    return conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]


_TITLE_SOURCE_RANK = {"last-prompt": 1, "ai-title": 2}


def get_session_title(conn, session_id):
    row = conn.execute(
        "SELECT title, title_source FROM session_meta WHERE session_id = ?",
        (session_id,)).fetchone()
    return dict(row) if row else None


def set_session_title(conn, session_id, title, source, updated_at):
    """INSERT OR REPLACE, но ai-title не перезаписывается более слабым source."""
    existing = get_session_title(conn, session_id)
    if existing and (_TITLE_SOURCE_RANK.get(existing["title_source"], 0)
                      > _TITLE_SOURCE_RANK.get(source, 0)):
        return
    conn.execute(
        "INSERT OR REPLACE INTO session_meta"
        " (session_id, title, title_source, updated_at) VALUES (?, ?, ?, ?)",
        (session_id, title, source, updated_at))
    conn.commit()
