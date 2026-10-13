"""Выгрузка метрик для передачи и приём чужой выгрузки."""
import gzip
import hashlib
import json
import time
import uuid
from pathlib import Path

import store

BUNDLE_VERSION = 2

# Поля, которые могут выдать, над чем работает человек.
SENSITIVE_FIELDS = ("project", "cwd", "git_branch")


def anonymize_value(value, salt):
    if value is None:
        return None
    digest = hashlib.sha256((salt + "|" + str(value)).encode("utf-8"))
    return digest.hexdigest()[:12]


def export_bundle(conn, out_path, days=None, anonymize=True):
    where, params = "", []
    if days:
        where = " WHERE ts >= ?"
        params = [time.time() - days * 86400]

    salt = uuid.uuid4().hex if anonymize else ""
    requests = []
    for row in conn.execute("SELECT * FROM requests" + where, params):
        item = dict(row)
        if anonymize:
            for field in SENSITIVE_FIELDS:
                item[field] = anonymize_value(item.get(field), salt)
        requests.append(item)

    ratelimits = [dict(r) for r in
                  conn.execute("SELECT * FROM ratelimits" + where, params)]

    # Тот же оконный where/params, что и у requests: без него session_meta
    # (заголовок сессии — по сути пользовательский промпт) утекал за пределы
    # запрошенного --days окна (см. Finding 5, финальное ревью). При
    # days=None where пуст и подзапрос естественно матчит все сессии — как
    # и раньше.
    session_meta = []
    sm_sql = ("SELECT * FROM session_meta WHERE session_id IN"
             " (SELECT DISTINCT session_id FROM requests%s)" % where)
    for row in conn.execute(sm_sql, params):
        item = dict(row)
        if anonymize and item.get("title") and item.get("session_id"):
            item["title"] = "«сессия %s»" % anonymize_value(item["session_id"], salt)[:6]
        session_meta.append(item)

    bundle = {
        "version": BUNDLE_VERSION,
        "exported_at": time.time(),
        "anonymized": anonymize,
        "requests": requests,
        "ratelimits": ratelimits,
        "session_meta": session_meta,
    }
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(out_path, "wt", encoding="utf-8") as fh:
        json.dump(bundle, fh, ensure_ascii=False)
    return out_path


def import_bundle(conn, path):
    path = Path(path)
    with open(path, "rb") as fh:
        magic = fh.read(2)
    opener = gzip.open if magic == b"\x1f\x8b" else open
    with opener(path, "rt", encoding="utf-8") as fh:
        bundle = json.load(fh)
    if bundle.get("version") not in (1, BUNDLE_VERSION):
        raise ValueError("неподдерживаемая версия выгрузки: %r"
                         % bundle.get("version"))
    n = store.insert_requests(conn, bundle.get("requests") or [])
    store.insert_ratelimits(conn, bundle.get("ratelimits") or [])
    for item in bundle.get("session_meta") or []:      # отсутствует в версии 1 — ок
        if not item.get("session_id"):                # защита от битого/враждебного бандла
            continue
        store.set_session_title(conn, item.get("session_id"), item.get("title"),
                                item.get("title_source") or "last-prompt",
                                item.get("updated_at") or 0.0)
    store.rebuild_sessions(conn)
    return n
