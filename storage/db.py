"""SQLite storage layer for the case-annotation-review platform.

Phase 1 = single local user. The schema already carries the columns needed for
Phase 2 multi-user collaboration (annotator_id / version / updated_at / audit_log)
so that the upgrade path is a migration, not a rewrite.

This module ONLY knows about connections and schema. All business logic lives in
``service/annotation_service.py``. The UI must never import this module directly.
"""

import os
import sqlite3
import threading

# Default DB lives next to the repo root (one level up from storage/).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB_PATH = os.path.join(_REPO_ROOT, "data", "annotations.db")

# Status / classification enums kept as plain strings (SQLite has no native enum).
STATUS_VALUES = ("CORRECT", "INCORRECT", "NEEDS_REVIEW", "UNREVIEWED")
CLASSIFICATION_VALUES = ("侵权", "非侵权", "案件不全")

_local = threading.local()


def get_connection(db_path: str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Return a per-thread SQLite connection with WAL + sane pragmas.

    Streamlit reruns on threads, so we cache one connection per thread.
    ``check_same_thread=False`` is safe because each thread owns its own conn.
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
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.execute("PRAGMA busy_timeout=5000;")
        # Merge any leftover WAL pages from a previous session into the main db
        # file so that a stale WAL file cannot cause sqlite3.DatabaseError on
        # first read (observed after Spaul-fix script left a non-empty WAL).
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        except Exception:
            pass
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


if __name__ == "__main__":
    init_db()
    print(f"Initialized schema at {DEFAULT_DB_PATH}")
