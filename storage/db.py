"""SQLite storage layer for the case-annotation-review platform.

Phase 1 = single local user. The schema already carries the columns needed for
Phase 2 multi-user collaboration (annotator_id / version / updated_at / audit_log)
so that the upgrade path is a migration, not a rewrite.

This module ONLY knows about connections and schema. All business logic lives in
``service/annotation_service.py``. The UI must never import this module directly.
"""

import logging
import os
import sqlite3
import threading

logger = logging.getLogger(__name__)

# Default DB lives next to the repo root (one level up from storage/).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB_PATH = os.path.join(_REPO_ROOT, "data", "annotations.db")

# Status / classification enums kept as plain strings (SQLite has no native enum).
STATUS_VALUES = ("CORRECT", "INCORRECT", "NEEDS_REVIEW", "UNREVIEWED")
CLASSIFICATION_VALUES = ("侵权", "非侵权", "案件不全")

_local = threading.local()


def get_connection(db_path: str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Return a per-thread SQLite connection with DELETE journal mode + sane pragmas.

    Streamlit reruns on threads, so we cache one connection per thread.
    ``check_same_thread=False`` is safe because each thread owns its own conn.

    DELETE journal mode (not WAL) is used because Streamlit Cloud clones the
    repo without -wal/-shm files; WAL mode requires those companion files and
    causes sqlite3.DatabaseError in that environment.
    """
    cache = getattr(_local, "conns", None)
    if cache is None:
        cache = {}
        _local.conns = cache
    conn = cache.get(db_path)
    if conn is None:
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=DELETE;")
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.execute("PRAGMA busy_timeout=5000;")
        cache[db_path] = conn
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    case_id                   TEXT PRIMARY KEY,
    case_name                 TEXT NOT NULL,
    case_text                 TEXT,
    source_file               TEXT,
    original_extracted_output TEXT,
    top_level_meta            TEXT,            -- JSON snapshot of original top-level enums
    imported_at               TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS annotation_fields (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id          TEXT NOT NULL,
    field_path       TEXT NOT NULL,           -- e.g. "II. LIABILITY/Tort-Specific Elements/Duty of Care"
    field_order      INTEGER NOT NULL DEFAULT 0,
    current_value    TEXT,
    status           TEXT NOT NULL DEFAULT 'UNREVIEWED',
    correction_reason TEXT,
    source_text_quote TEXT,
    source_text_offset INTEGER,
    annotator_id     TEXT NOT NULL DEFAULT 'default_user',
    version          INTEGER NOT NULL DEFAULT 1,
    updated_at       TEXT NOT NULL,
    FOREIGN KEY (case_id) REFERENCES cases(case_id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_annotation_fields_case_path
    ON annotation_fields(case_id, field_path);

CREATE TABLE IF NOT EXISTS top_level_corrections (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id       TEXT NOT NULL,
    field_name    TEXT NOT NULL,              -- decision_type / is_liability_suitable / ...
    original_value TEXT,                      -- JSON
    current_value  TEXT,                      -- JSON
    status        TEXT NOT NULL DEFAULT 'UNREVIEWED',
    reason        TEXT,
    annotator_id  TEXT NOT NULL DEFAULT 'default_user',
    version       INTEGER NOT NULL DEFAULT 1,
    updated_at    TEXT NOT NULL,
    FOREIGN KEY (case_id) REFERENCES cases(case_id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_top_level_case_field
    ON top_level_corrections(case_id, field_name);

CREATE TABLE IF NOT EXISTS case_classification (
    case_id      TEXT PRIMARY KEY,
    classification TEXT,                       -- 侵权 / 非侵权 / 案件不全 (nullable = unset)
    note         TEXT,
    annotator_id TEXT NOT NULL DEFAULT 'default_user',
    version      INTEGER NOT NULL DEFAULT 1,
    updated_at   TEXT NOT NULL,
    FOREIGN KEY (case_id) REFERENCES cases(case_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS audit_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id      TEXT,
    field_path   TEXT,
    action       TEXT NOT NULL,               -- CREATE / UPDATE / DELETE / STATUS_CHANGE
    old_value    TEXT,
    new_value    TEXT,
    annotator_id TEXT NOT NULL DEFAULT 'default_user',
    ts           TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_audit_case ON audit_log(case_id);
"""


def init_db(db_path: str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Create all tables/indexes if they do not yet exist and return the conn."""
    conn = get_connection(db_path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def _is_db_healthy(db_path: str = DEFAULT_DB_PATH) -> bool:
    """Quick smoke-test: open the file and run the query that fails on Streamlit Cloud."""
    if not os.path.exists(db_path):
        return False
    try:
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        result = conn.execute("PRAGMA integrity_check").fetchone()
        if result[0] != "ok":
            logger.warning("DB integrity_check failed: %s", result[0])
            conn.close()
            return False
        # run the exact query that was failing
        conn.execute(
            "SELECT status, COUNT(*) AS c FROM annotation_fields GROUP BY status"
        ).fetchall()
        conn.close()
        return True
    except Exception as exc:
        logger.warning("DB health check failed: %s", exc)
        try:
            conn.close()
        except Exception:
            pass
        return False


def ensure_db(db_path: str = DEFAULT_DB_PATH):
    """Verify the database is usable; rebuild from JSON + docx if it is corrupt.

    Call this once at application startup (e.g. in streamlit_app.py) BEFORE
    any service-layer code runs.  If the existing .db file passes a basic
    integrity / query smoke-test, this is a no-op.  Otherwise it deletes
    the corrupt file and re-imports from the checked-in source data so the
    app can self-heal on Streamlit Cloud without manual intervention.

    Rebuild priority:
      1. ``data/annotation_state_snapshot.json`` — a full export produced by
         ``svc.export_gold()`` that preserves every status / correction / quote.
      2. Fallback: ``cases_with_extracted_output.json`` + docx (loses annotations
         made after the initial import, but the app is at least usable).
    """
    if _is_db_healthy(db_path):
        logger.info("DB healthy at %s", db_path)
        return

    logger.warning("DB at %s is missing or corrupt – rebuilding from source data …",
                   db_path)

    # clear any cached connection for this path
    cache = getattr(_local, "conns", None)
    if cache and db_path in cache:
        try:
            cache[db_path].close()
        except Exception:
            pass
        del cache[db_path]

    # remove corrupt file
    if os.path.exists(db_path):
        os.remove(db_path)
        logger.info("Removed corrupt DB file")

    # lazy imports to avoid circular deps at module level
    import json
    import sys

    data_dir = os.path.join(_REPO_ROOT, "data")
    snapshot_path = os.path.join(data_dir, "annotation_state_snapshot.json")

    # ── Strategy 1: full snapshot (preserves all annotation state) ──────────
    if os.path.exists(snapshot_path):
        logger.info("Rebuilding from annotation_state_snapshot.json …")
        conn = init_db(db_path)
        _rebuild_from_snapshot(conn, snapshot_path)
        n = conn.execute("SELECT COUNT(*) FROM annotation_fields").fetchone()[0]
        logger.info("DB rebuilt from snapshot with %d annotation fields", n)
        return

    # ── Strategy 2: import pipeline from JSON + docx ───────────────────────
    scripts_dir = os.path.join(_REPO_ROOT, "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    from import_data import import_cases, match_docx_to_fields, JSON_PATH, DOCX_PATH

    conn = init_db(db_path)
    with open(JSON_PATH, encoding="utf-8") as f:
        cases = json.load(f)
    import_cases(conn, cases)
    conn.commit()
    logger.info("Imported %d cases from JSON", len(cases))

    if os.path.exists(DOCX_PATH):
        match_docx_to_fields(conn)
        conn.commit()
        logger.info("Applied docx annotations")
    else:
        logger.info("No docx file at %s – skipped annotation matching", DOCX_PATH)

    n = conn.execute("SELECT COUNT(*) FROM annotation_fields").fetchone()[0]
    logger.info("DB rebuilt (pipeline) with %d annotation fields", n)


def _rebuild_from_snapshot(conn, snapshot_path: str):
    """Populate an empty (schema-initialised) DB from an export_gold() snapshot.

    The snapshot JSON is a list of case objects, each containing fields,
    top_level_corrections, and classification — everything needed to fully
    restore the annotation state.
    """
    import json
    from datetime import datetime, timezone

    with open(snapshot_path, encoding="utf-8") as f:
        cases = json.load(f)

    now = datetime.now(timezone.utc).isoformat()

    # We also need the original case data (case_text, extracted_output, top_level_meta)
    data_dir = os.path.dirname(snapshot_path)
    json_path = os.path.join(data_dir, "cases_with_extracted_output.json")
    original_lookup = {}
    if os.path.exists(json_path):
        with open(json_path, encoding="utf-8") as f:
            for rec in json.load(f):
                original_lookup[rec["case_id"]] = rec

    for case in cases:
        cid = case["case_id"]
        orig = original_lookup.get(cid, {})
        top_level_fields = ["decision_type", "procedure_stage",
                            "is_liability_suitable", "is_specific_tort",
                            "tort_cause_of_action_list"]
        meta = {k: orig.get(k) for k in top_level_fields}

        conn.execute(
            "INSERT OR REPLACE INTO cases (case_id, case_name, case_text, "
            "source_file, original_extracted_output, top_level_meta, imported_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (cid, case.get("case_name", ""), orig.get("case_text", ""),
             case.get("source_file"), orig.get("extracted_output", ""),
             json.dumps(meta, ensure_ascii=False), now),
        )

        # annotation fields
        for i, f in enumerate(case.get("fields", []), 1):
            conn.execute(
                "INSERT OR IGNORE INTO annotation_fields (case_id, field_path, "
                "field_order, current_value, status, correction_reason, "
                "source_text_quote, source_text_offset, annotator_id, version, "
                "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'default_user', ?, ?)",
                (cid, f["field_path"], i, f.get("value"),
                 f.get("status", "UNREVIEWED"), f.get("reason"),
                 f.get("source_quote"), f.get("source_offset"),
                 f.get("version", 1), now),
            )

        # top-level corrections
        tl = case.get("top_level_corrections", {})
        for fname, info in tl.items():
            orig_val = info.get("original")
            cur_val = info.get("value")
            if isinstance(orig_val, str):
                pass  # already encoded
            else:
                orig_val = json.dumps(orig_val, ensure_ascii=False)
            if isinstance(cur_val, str):
                pass
            else:
                cur_val = json.dumps(cur_val, ensure_ascii=False)
            conn.execute(
                "INSERT OR IGNORE INTO top_level_corrections (case_id, field_name, "
                "original_value, current_value, status, reason, annotator_id, "
                "version, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'default_user', 1, ?)",
                (cid, fname, orig_val, cur_val,
                 info.get("status", "UNREVIEWED"), info.get("reason"), now),
            )

        # classification
        cls = case.get("classification")
        if cls:
            conn.execute(
                "INSERT OR IGNORE INTO case_classification (case_id, classification, "
                "note, annotator_id, version, updated_at) "
                "VALUES (?, ?, ?, 'default_user', 1, ?)",
                (cid, cls, case.get("classification_note"), now),
            )

    conn.commit()


if __name__ == "__main__":
    init_db()
    print(f"Initialized schema at {DEFAULT_DB_PATH}")
