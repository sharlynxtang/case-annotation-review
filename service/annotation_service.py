"""Annotation service layer -- pure Python functions over the SQLite storage.

ARCHITECTURE RULE: this module is the ONLY place that touches the DB for business
logic. The Streamlit UI calls these functions and never opens a sqlite connection
itself. In Phase 2 these exact functions are wrapped by FastAPI endpoints
(see README "阶段2 升级路径").

Every write goes through ``_audit`` so audit_log always reflects reality.

Optimistic locking: write functions accept ``expected_version``. In Phase 1
(single user) it is not enforced unless the caller passes a non-None value, but
the plumbing is here so Phase 2 can flip it on without signature changes.
"""

import json
from datetime import datetime, timezone

from storage import db

DEFAULT_USER = "default_user"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _conn(db_path: str = db.DEFAULT_DB_PATH):
    return db.get_connection(db_path)


def _audit(conn, case_id, field_path, action, old_value, new_value, annotator_id):
    conn.execute(
        "INSERT INTO audit_log (case_id, field_path, action, old_value, new_value, "
        "annotator_id, ts) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (case_id, field_path, action,
         _as_text(old_value), _as_text(new_value), annotator_id, _now()),
    )


def _as_text(v):
    if v is None or isinstance(v, str):
        return v
    return json.dumps(v, ensure_ascii=False)


class VersionConflict(Exception):
    """Raised when expected_version does not match the stored version."""


def _check_version(row_version, expected_version):
    if expected_version is not None and row_version != expected_version:
        raise VersionConflict(
            f"expected version {expected_version} but found {row_version}"
        )


# --------------------------------------------------------------------------- #
# read APIs
# --------------------------------------------------------------------------- #
def list_cases(filter_status=None, filter_classification=None, search=None,
               db_path: str = db.DEFAULT_DB_PATH):
    """Return [{case_id, case_name, progress, classification, counts}].

    progress = pct of fields that are reviewed (status != UNREVIEWED).
    Filters:
      - filter_status: keep cases that have at least one field with this status.
      - filter_classification: exact classification match.
      - search: case-insensitive substring of case_name.
    """
    conn = _conn(db_path)
    cases = conn.execute(
        "SELECT case_id, case_name FROM cases ORDER BY case_name"
    ).fetchall()

    results = []
    for c in cases:
        cid = c["case_id"]
        rows = conn.execute(
            "SELECT status FROM annotation_fields WHERE case_id=?", (cid,)
        ).fetchall()
        total = len(rows)
        counts = {s: 0 for s in db.STATUS_VALUES}
        for r in rows:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        reviewed = total - counts.get("UNREVIEWED", 0)
        progress = round(100 * reviewed / total) if total else 0

        cls_row = conn.execute(
            "SELECT classification FROM case_classification WHERE case_id=?", (cid,)
        ).fetchone()
        classification = cls_row["classification"] if cls_row else None

        if search and search.strip().lower() not in c["case_name"].lower():
            continue
        if filter_classification and classification != filter_classification:
            continue
        if filter_status and counts.get(filter_status, 0) == 0:
            continue

        results.append({
            "case_id": cid,
            "case_name": c["case_name"],
            "progress": progress,
            "classification": classification,
            "counts": counts,
            "total_fields": total,
        })
    return results


def get_case_detail(case_id, db_path: str = db.DEFAULT_DB_PATH):
    """Return {case, fields[], top_level[], classification} for one case."""
    conn = _conn(db_path)
    case = conn.execute(
        "SELECT * FROM cases WHERE case_id=?", (case_id,)
    ).fetchone()
    if case is None:
        return None

    fields = conn.execute(
        "SELECT * FROM annotation_fields WHERE case_id=? ORDER BY field_order, id",
        (case_id,),
    ).fetchall()
    top_level = conn.execute(
        "SELECT * FROM top_level_corrections WHERE case_id=? ORDER BY field_name",
        (case_id,),
    ).fetchall()
    cls = conn.execute(
        "SELECT * FROM case_classification WHERE case_id=?", (case_id,)
    ).fetchone()

    return {
        "case": dict(case),
        "fields": [dict(r) for r in fields],
        "top_level": [_decode_top_level(dict(r)) for r in top_level],
        "classification": dict(cls) if cls else None,
    }


def _decode_top_level(row):
    for k in ("original_value", "current_value"):
        if row.get(k) is not None:
            try:
                row[k] = json.loads(row[k])
            except (ValueError, TypeError):
                pass
    return row


# --------------------------------------------------------------------------- #
# annotation_fields write APIs
# --------------------------------------------------------------------------- #
def update_field(case_id, field_path, new_value=None, status=None, reason=None,
                 source_quote=None, source_offset=None,
                 annotator_id: str = DEFAULT_USER, expected_version=None,
                 db_path: str = db.DEFAULT_DB_PATH):
    """Update value/status/reason/source of one field. Returns new version.

    Only provided (non-None) arguments overwrite existing columns, except status
    which, if provided, also emits a STATUS_CHANGE audit entry.
    """
    conn = _conn(db_path)
    row = conn.execute(
        "SELECT * FROM annotation_fields WHERE case_id=? AND field_path=?",
        (case_id, field_path),
    ).fetchone()
    if row is None:
        raise KeyError(f"field not found: {case_id} / {field_path}")
    _check_version(row["version"], expected_version)

    old_value = row["current_value"]
    old_status = row["status"]

    new_value_final = old_value if new_value is None else new_value
    new_status_final = old_status if status is None else status
    if status is not None and status not in db.STATUS_VALUES:
        raise ValueError(f"invalid status: {status}")

    new_version = row["version"] + 1
    conn.execute(
        "UPDATE annotation_fields SET current_value=?, status=?, correction_reason=?, "
        "source_text_quote=?, source_text_offset=?, annotator_id=?, version=?, "
        "updated_at=? WHERE id=? AND version=?",
        (new_value_final, new_status_final,
         row["correction_reason"] if reason is None else reason,
         row["source_text_quote"] if source_quote is None else source_quote,
         row["source_text_offset"] if source_offset is None else source_offset,
         annotator_id, new_version, _now(), row["id"], row["version"]),
    )

    if status is not None and status != old_status:
        _audit(conn, case_id, field_path, "STATUS_CHANGE", old_status, status,
               annotator_id)
    if new_value is not None and new_value != old_value:
        _audit(conn, case_id, field_path, "UPDATE", old_value, new_value,
               annotator_id)
    if status is None and new_value is None:
        # metadata-only change (reason / quote)
        _audit(conn, case_id, field_path, "UPDATE", old_value, new_value_final,
               annotator_id)
    conn.commit()
    return new_version


def add_field(case_id, field_path, value=None, status="UNREVIEWED", reason=None,
              source_quote=None, source_offset=None, field_order=None,
              annotator_id: str = DEFAULT_USER,
              db_path: str = db.DEFAULT_DB_PATH):
    """Add a brand-new annotation field to a case. Returns the new row id."""
    conn = _conn(db_path)
    exists = conn.execute(
        "SELECT 1 FROM annotation_fields WHERE case_id=? AND field_path=?",
        (case_id, field_path),
    ).fetchone()
    if exists:
        raise ValueError(f"field already exists: {field_path}")
    if status not in db.STATUS_VALUES:
        raise ValueError(f"invalid status: {status}")

    if field_order is None:
        row = conn.execute(
            "SELECT COALESCE(MAX(field_order), 0) AS m FROM annotation_fields "
            "WHERE case_id=?", (case_id,),
        ).fetchone()
        field_order = (row["m"] or 0) + 1

    cur = conn.execute(
        "INSERT INTO annotation_fields (case_id, field_path, field_order, "
        "current_value, status, correction_reason, source_text_quote, "
        "source_text_offset, annotator_id, version, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
        (case_id, field_path, field_order, value, status, reason,
         source_quote, source_offset, annotator_id, _now()),
    )
    _audit(conn, case_id, field_path, "CREATE", None, value, annotator_id)
    conn.commit()
    return cur.lastrowid


def delete_field(case_id, field_path, annotator_id: str = DEFAULT_USER,
                 expected_version=None, db_path: str = db.DEFAULT_DB_PATH):
    """Delete an annotation field. Returns True if a row was removed."""
    conn = _conn(db_path)
    row = conn.execute(
        "SELECT * FROM annotation_fields WHERE case_id=? AND field_path=?",
        (case_id, field_path),
    ).fetchone()
    if row is None:
        return False
    _check_version(row["version"], expected_version)
    conn.execute("DELETE FROM annotation_fields WHERE id=?", (row["id"],))
    _audit(conn, case_id, field_path, "DELETE", row["current_value"], None,
           annotator_id)
    conn.commit()
    return True


# --------------------------------------------------------------------------- #
# top-level enum corrections
# --------------------------------------------------------------------------- #
def update_top_level(case_id, field_name, new_value, status=None, reason=None,
                     annotator_id: str = DEFAULT_USER, expected_version=None,
                     db_path: str = db.DEFAULT_DB_PATH):
    """Upsert a correction to a top-level enum (decision_type, tort list, ...).

    new_value may be a scalar (str) or a list (for tort_cause_of_action_list).
    Returns the new version.
    """
    conn = _conn(db_path)
    row = conn.execute(
        "SELECT * FROM top_level_corrections WHERE case_id=? AND field_name=?",
        (case_id, field_name),
    ).fetchone()
    encoded = json.dumps(new_value, ensure_ascii=False)

    if row is None:
        conn.execute(
            "INSERT INTO top_level_corrections (case_id, field_name, original_value, "
            "current_value, status, reason, annotator_id, version, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)",
            (case_id, field_name, encoded, encoded,
             status or "UNREVIEWED", reason, annotator_id, _now()),
        )
        _audit(conn, case_id, f"top_level/{field_name}", "CREATE", None,
               encoded, annotator_id)
        conn.commit()
        return 1

    _check_version(row["version"], expected_version)
    new_version = row["version"] + 1
    conn.execute(
        "UPDATE top_level_corrections SET current_value=?, status=?, reason=?, "
        "annotator_id=?, version=?, updated_at=? WHERE id=? AND version=?",
        (encoded, status if status is not None else row["status"],
         reason if reason is not None else row["reason"],
         annotator_id, new_version, _now(), row["id"], row["version"]),
    )
    _audit(conn, case_id, f"top_level/{field_name}", "UPDATE",
           row["current_value"], encoded, annotator_id)
    conn.commit()
    return new_version


# --------------------------------------------------------------------------- #
# case classification (docx top line)
# --------------------------------------------------------------------------- #
def update_classification(case_id, classification, note=None,
                          annotator_id: str = DEFAULT_USER, expected_version=None,
                          db_path: str = db.DEFAULT_DB_PATH):
    """Set the case-level classification (侵权 / 非侵权 / 案件不全)."""
    if classification is not None and classification not in db.CLASSIFICATION_VALUES:
        raise ValueError(f"invalid classification: {classification}")
    conn = _conn(db_path)
    row = conn.execute(
        "SELECT * FROM case_classification WHERE case_id=?", (case_id,)
    ).fetchone()

    if row is None:
        conn.execute(
            "INSERT INTO case_classification (case_id, classification, note, "
            "annotator_id, version, updated_at) VALUES (?, ?, ?, ?, 1, ?)",
            (case_id, classification, note, annotator_id, _now()),
        )
        _audit(conn, case_id, "classification", "CREATE", None,
               classification, annotator_id)
        conn.commit()
        return 1

    _check_version(row["version"], expected_version)
    new_version = row["version"] + 1
    conn.execute(
        "UPDATE case_classification SET classification=?, note=?, annotator_id=?, "
        "version=?, updated_at=? WHERE case_id=? AND version=?",
        (classification, note if note is not None else row["note"],
         annotator_id, new_version, _now(), case_id, row["version"]),
    )
    _audit(conn, case_id, "classification", "STATUS_CHANGE",
           row["classification"], classification, annotator_id)
    conn.commit()
    return new_version


# --------------------------------------------------------------------------- #
# progress / export
# --------------------------------------------------------------------------- #
def get_progress(db_path: str = db.DEFAULT_DB_PATH):
    """Return aggregate counts across all fields of all cases."""
    conn = _conn(db_path)
    total_cases = conn.execute("SELECT COUNT(*) AS c FROM cases").fetchone()["c"]
    rows = conn.execute(
        "SELECT status, COUNT(*) AS c FROM annotation_fields GROUP BY status"
    ).fetchall()
    counts = {s: 0 for s in db.STATUS_VALUES}
    for r in rows:
        counts[r["status"]] = r["c"]
    total_fields = sum(counts.values())
    reviewed = total_fields - counts.get("UNREVIEWED", 0)
    return {
        "total_cases": total_cases,
        "total_fields": total_fields,
        "reviewed": reviewed,
        "unreviewed": counts.get("UNREVIEWED", 0),
        "correct": counts.get("CORRECT", 0),
        "incorrect": counts.get("INCORRECT", 0),
        "needs_review": counts.get("NEEDS_REVIEW", 0),
        "progress": round(100 * reviewed / total_fields) if total_fields else 0,
    }


def export_gold(case_id=None, db_path: str = db.DEFAULT_DB_PATH):
    """Export corrected gold standard as a JSON-serialisable structure.

    If case_id is None, export all cases. Each case keeps case_id / meta /
    classification / the full field tree with status / reason / source quote.
    """
    conn = _conn(db_path)
    if case_id is None:
        ids = [r["case_id"] for r in
               conn.execute("SELECT case_id FROM cases ORDER BY case_name").fetchall()]
    else:
        ids = [case_id]

    out = []
    for cid in ids:
        detail = get_case_detail(cid, db_path=db_path)
        if detail is None:
            continue
        case = detail["case"]
        try:
            meta = json.loads(case.get("top_level_meta") or "{}")
        except (ValueError, TypeError):
            meta = {}

        # apply top-level corrections over the original meta snapshot
        top_level = {}
        for tl in detail["top_level"]:
            top_level[tl["field_name"]] = {
                "value": tl["current_value"],
                "original": tl["original_value"],
                "status": tl["status"],
                "reason": tl["reason"],
            }

        fields = []
        for f in detail["fields"]:
            fields.append({
                "field_path": f["field_path"],
                "value": f["current_value"],
                "status": f["status"],
                "reason": f["correction_reason"],
                "source_quote": f["source_text_quote"],
                "source_offset": f["source_text_offset"],
                "version": f["version"],
            })

        out.append({
            "case_id": cid,
            "case_name": case["case_name"],
            "source_file": case.get("source_file"),
            "original_top_level_meta": meta,
            "top_level_corrections": top_level,
            "classification": (detail["classification"] or {}).get("classification"),
            "classification_note": (detail["classification"] or {}).get("note"),
            "fields": fields,
        })

    return out[0] if case_id is not None and out else out


def _rebuild_markdown(fields):
    """Reconstruct an extracted_output Markdown string from corrected annotation fields.

    Uses the field_path hierarchy to emit headers at the appropriate level and
    the current_value as the body text below each header.
    """
    lines = []
    prev_section = None
    prev_sub = None

    for f in fields:
        path = f["field_path"]
        value = (f["current_value"] or "").strip()
        parts = path.split("/")

        section = parts[0] if len(parts) >= 1 else ""
        sub = parts[1] if len(parts) >= 2 else None
        leaf = parts[-1] if len(parts) >= 2 else None

        # emit top-level section header once
        if section != prev_section:
            if lines:
                lines.append("")
            lines.append(f"# **{section}**")
            lines.append("")
            prev_section = section
            prev_sub = None

        depth = len(parts)
        if depth == 1:
            # section-level field (no sub-path) — body goes directly under section header
            if value:
                lines.append(value)
                lines.append("")
        elif depth == 2:
            # second-level: ## heading
            if sub != prev_sub:
                lines.append(f"## **{sub}**")
                prev_sub = sub
            if value:
                lines.append(value)
                lines.append("")
        elif depth == 3:
            # third-level: ### heading
            lines.append(f"### **{leaf}**")
            if value:
                lines.append(value)
                lines.append("")
        else:
            # deeper: #### heading
            lines.append(f"#### **{leaf}**")
            if value:
                lines.append(value)
                lines.append("")

    # strip trailing blank lines
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def export_original_format(case_id=None, db_path: str = db.DEFAULT_DB_PATH):
    """Export in the same format as the source cases_with_extracted_output.json.

    The corrected annotation field values are reassembled into a single
    ``extracted_output`` Markdown string so the output is a drop-in replacement
    for the original JSON file.

    Top-level enum fields (decision_type etc.) reflect the current corrections.
    """
    conn = _conn(db_path)
    if case_id is None:
        ids = [r["case_id"] for r in
               conn.execute("SELECT case_id FROM cases ORDER BY case_name").fetchall()]
    else:
        ids = [case_id]

    out = []
    for cid in ids:
        detail = get_case_detail(cid, db_path=db_path)
        if detail is None:
            continue
        case = detail["case"]
        try:
            meta = json.loads(case.get("top_level_meta") or "{}")
        except (ValueError, TypeError):
            meta = {}

        # apply top-level corrections
        corrected_meta = dict(meta)
        for tl in detail["top_level"]:
            corrected_meta[tl["field_name"]] = tl["current_value"]

        # rebuild extracted_output Markdown from corrected fields
        extracted_output = _rebuild_markdown(detail["fields"])

        record = {
            "case_id": cid,
            "case_name": case["case_name"],
            "source_file": case.get("source_file"),
            "case_text": case.get("case_text") or "",
            "extracted_output": extracted_output,
        }
        # merge corrected top-level enum fields at top level (original format)
        for k in ("decision_type", "procedure_stage", "is_liability_suitable",
                  "is_specific_tort", "tort_cause_of_action_list"):
            record[k] = corrected_meta.get(k)

        out.append(record)

    return out[0] if case_id is not None and out else out


def get_audit_log(case_id=None, limit=200, db_path: str = db.DEFAULT_DB_PATH):
    """Return recent audit entries (newest first), optionally per case."""
    conn = _conn(db_path)
    if case_id:
        rows = conn.execute(
            "SELECT * FROM audit_log WHERE case_id=? ORDER BY id DESC LIMIT ?",
            (case_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]
