import argparse
import json
import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from scripts.import_data import (  # noqa: E402
    TOP_LEVEL_FIELDS,
    _alias,
    _best_case_match,
    _guess_section,
    _norm_case_name,
    _norm_label,
    parse_extracted_output,
)
from storage import db  # noqa: E402

ANNOTATOR_ID = "综合审查_0613"
CLASSIFICATION_MAP = {"侵权案件": "侵权", "非侵权案件": "非侵权", "案件不全": "案件不全"}
ALL_CORRECT_HINTS = (
    "全部CORRECT",
    "完全正确，无需修改",
    "按AI判定CORRECT",
    "一致，无需修改",
    "按AI判定为非侵权",
    "按AI判定CORRECT。",
    "无需修改。",
)


@dataclass
class FieldDecision:
    label: str
    status: str
    detail: str | None = None


@dataclass
class CaseReview:
    number: int
    case_name: str
    classification: str
    field_decisions: list[FieldDecision]
    whole_case_status: str | None
    generic_notes: list[str]
    field_notes: dict[str, str]
    raw_body: str


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def clean_text(text: str) -> str:
    text = text.replace("\u00a0", " ").strip()
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def split_case_sections(markdown_text: str) -> list[CaseReview]:
    parts = re.split(r"(?m)^###\s+案件(\d+)名称：(.+)$", markdown_text)
    reviews: list[CaseReview] = []
    for index in range(1, len(parts), 3):
        number = int(parts[index])
        case_name = parts[index + 1].strip()
        body = parts[index + 2]
        classification_match = re.search(r"(?m)^<(侵权案件|非侵权案件|案件不全)>", body)
        if not classification_match:
            raise ValueError(f"未找到案件分类: {case_name}")
        classification = CLASSIFICATION_MAP[classification_match.group(1)]
        field_decisions = parse_field_table(body)
        field_notes, generic_notes = parse_blockquotes(body)
        whole_case_status = "INCORRECT" if "整篇INCORRECT" in body else None
        reviews.append(
            CaseReview(
                number=number,
                case_name=case_name,
                classification=classification,
                field_decisions=field_decisions,
                whole_case_status=whole_case_status,
                generic_notes=generic_notes,
                field_notes=field_notes,
                raw_body=body,
            )
        )
    return reviews


def parse_field_table(body: str) -> list[FieldDecision]:
    decisions: list[FieldDecision] = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cols = [clean_text(col) for col in stripped.strip("|").split("|")]
        if len(cols) < 2:
            continue
        first = cols[0]
        if first in {"字段", "来源", "指标", "错误类型"} or set(first) == {"-"}:
            continue
        verdict = cols[1]
        if "INCORRECT" in verdict:
            status = "INCORRECT"
        elif "待核实" in verdict:
            status = "NEEDS_REVIEW"
        elif "CORRECT" in verdict:
            status = "CORRECT"
        else:
            continue
        detail = None
        if len(cols) >= 5 and cols[4]:
            detail = cols[4]
        elif len(cols) >= 3 and cols[2]:
            detail = cols[2]
        decisions.append(FieldDecision(label=first, status=status, detail=detail))
    return decisions


def parse_blockquotes(body: str) -> tuple[dict[str, str], list[str]]:
    field_notes: dict[str, str] = {}
    generic_notes: list[str] = []
    lines = body.splitlines()
    index = 0
    while index < len(lines):
        if not lines[index].lstrip().startswith(">"):
            index += 1
            continue
        block_lines = []
        while index < len(lines) and lines[index].lstrip().startswith(">"):
            line = lines[index].lstrip()[1:].strip()
            if line.startswith("-"):
                line = line[1:].strip()
            block_lines.append(clean_text(line))
            index += 1
        block_text = "\n".join(line for line in block_lines if line).strip()
        if not block_text:
            continue
        first_line, *rest = block_text.split("\n")
        match = re.match(r"^(.+?)(?: 修改建议| 备注)：\s*(.*)$", first_line)
        if match:
            label = clean_label(match.group(1))
            content_lines = [match.group(2).strip()] if match.group(2).strip() else []
            content_lines.extend(rest)
            field_notes[label] = clean_text("\n".join(content_lines))
        else:
            generic_notes.append(clean_text(block_text))
    return field_notes, generic_notes


def clean_label(label: str) -> str:
    label = clean_text(label)
    label = label.replace("Characterisation", "Issue Framing")
    return label


def case_default_status(review: CaseReview) -> str:
    if review.whole_case_status:
        return review.whole_case_status
    if review.field_decisions:
        return "CORRECT"
    if any(hint in review.raw_body for hint in ALL_CORRECT_HINTS):
        return "CORRECT"
    return "CORRECT"


def next_import_version(conn) -> int:
    max_version = 0
    for table in ("annotation_fields", "top_level_corrections", "case_classification"):
        row = conn.execute(f"SELECT MAX(version) AS max_version FROM {table}").fetchone()
        value = row["max_version"] if row else None
        if value and value > max_version:
            max_version = value
    return max_version + 1


def build_case_index(cases: list[dict]) -> dict[str, dict]:
    exact: dict[str, list[dict]] = defaultdict(list)
    name_index: dict[str, list[str]] = defaultdict(list)
    for case in cases:
        exact[case["case_name"]].append(case)
        name_index[_norm_case_name(case["case_name"])].append(case["case_id"])
    case_by_id = {case["case_id"]: case for case in cases}
    return {"exact": exact, "name_index": name_index, "case_by_id": case_by_id}


def match_case(review: CaseReview, case_lookup: dict[str, dict], used_case_ids: set[str]) -> dict:
    if review.case_name in case_lookup["exact"]:
        candidates = case_lookup["exact"][review.case_name]
        for case in candidates:
            if case["case_id"] not in used_case_ids:
                used_case_ids.add(case["case_id"])
                return case
        case = candidates[0]
        used_case_ids.add(case["case_id"])
        return case
    norm = _norm_case_name(review.case_name)
    case_id = _best_case_match(norm, case_lookup["name_index"], used_case_ids)
    if case_id is None:
        raise KeyError(f"案件未匹配到 JSON: {review.case_name}")
    used_case_ids.add(case_id)
    return case_lookup["case_by_id"][case_id]


def build_field_records(case: dict, import_version: int, default_status: str) -> list[dict]:
    records = parse_extracted_output(case.get("extracted_output", ""))
    built = []
    for rec in records:
        built.append(
            {
                "case_id": case["case_id"],
                "field_path": rec["field_path"],
                "field_order": rec["order"],
                "current_value": rec["value"],
                "status": default_status,
                "correction_reason": None,
                "source_text_quote": None,
                "source_text_offset": None,
                "annotator_id": ANNOTATOR_ID,
                "version": import_version,
                "updated_at": now(),
            }
        )
    return built


def resolve_field_targets(field_records: list[dict], label: str) -> list[int]:
    label = clean_label(label)
    if "全部Liability字段" in label:
        return [index for index, row in enumerate(field_records) if row["field_path"].startswith("II. LIABILITY")]
    if label in {"全部字段", "其余全部字段"}:
        return list(range(len(field_records)))
    key = _alias(_norm_label(label))
    target_indexes: list[int] = []
    for index, row in enumerate(field_records):
        parts = row["field_path"].split("/")
        tail = _alias(_norm_label(parts[-1]))
        aliases = {_alias(_norm_label(part)) for part in parts}
        if key == tail or key in aliases:
            target_indexes.append(index)
    if target_indexes:
        return dedupe(target_indexes)
    for index, row in enumerate(field_records):
        normalized_path = _alias(_norm_label(row["field_path"]))
        if key and len(key) >= 4 and (key in normalized_path or normalized_path in key):
            target_indexes.append(index)
    if target_indexes:
        return dedupe(target_indexes)
    compact_label = key.replace("misc", "")
    if compact_label != key and compact_label:
        for index, row in enumerate(field_records):
            normalized_path = _alias(_norm_label(row["field_path"]))
            if compact_label in normalized_path:
                target_indexes.append(index)
    return dedupe(target_indexes)


def dedupe(values: list[int]) -> list[int]:
    seen = set()
    ordered = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered


def create_missing_field(field_records: list[dict], case_id: str, label: str, status: str, import_version: int, reason: str | None) -> int:
    next_order = max((row["field_order"] for row in field_records), default=0) + 1
    field_records.append(
        {
            "case_id": case_id,
            "field_path": f"{_guess_section(label)}/{label}",
            "field_order": next_order,
            "current_value": "",
            "status": status,
            "correction_reason": reason,
            "source_text_quote": None,
            "source_text_offset": None,
            "annotator_id": ANNOTATOR_ID,
            "version": import_version,
            "updated_at": now(),
        }
    )
    return len(field_records) - 1


def build_reason(review: CaseReview, decision: FieldDecision) -> str | None:
    notes = []
    explicit = review.field_notes.get(clean_label(decision.label))
    if explicit:
        notes.append(explicit)
    else:
        decision_key = _alias(_norm_label(decision.label))
        for note_label, note_text in review.field_notes.items():
            note_key = _alias(_norm_label(note_label))
            if decision_key == note_key or (decision_key and len(decision_key) >= 4 and (decision_key in note_key or note_key in decision_key)):
                notes.append(note_text)
                break
    if decision.detail and decision.detail not in {"说明", "一致", "CORRECT", "INCORRECT"}:
        notes.append(decision.detail)
    if not notes and decision.status in {"INCORRECT", "NEEDS_REVIEW"} and review.generic_notes:
        notes.extend(review.generic_notes[:1])
    if not notes and review.whole_case_status == "INCORRECT":
        notes.extend(review.generic_notes[:1])
    if not notes:
        return None
    return "\n".join(dict.fromkeys(notes))


def classification_note(review: CaseReview) -> str | None:
    if not review.generic_notes:
        return None
    return "\n".join(review.generic_notes)


def reset_tables(conn) -> None:
    conn.execute("DELETE FROM audit_log")
    conn.execute("DELETE FROM case_classification")
    conn.execute("DELETE FROM top_level_corrections")
    conn.execute("DELETE FROM annotation_fields")
    conn.execute("DELETE FROM cases")
    conn.commit()


def insert_case(conn, case: dict) -> None:
    meta = {key: case.get(key) for key in TOP_LEVEL_FIELDS}
    conn.execute(
        "INSERT INTO cases (case_id, case_name, case_text, source_file, original_extracted_output, top_level_meta, imported_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            case["case_id"],
            case.get("case_name", ""),
            case.get("case_text", ""),
            case.get("source_file"),
            case.get("extracted_output", ""),
            json.dumps(meta, ensure_ascii=False),
            now(),
        ),
    )


def insert_top_level(conn, case: dict, import_version: int) -> None:
    for field_name in TOP_LEVEL_FIELDS:
        value = json.dumps(case.get(field_name), ensure_ascii=False)
        conn.execute(
            "INSERT INTO top_level_corrections (case_id, field_name, original_value, current_value, status, reason, annotator_id, version, updated_at) "
            "VALUES (?, ?, ?, ?, 'UNREVIEWED', NULL, ?, ?, ?)",
            (case["case_id"], field_name, value, value, ANNOTATOR_ID, import_version, now()),
        )


def insert_classification(conn, case_id: str, review: CaseReview, import_version: int) -> None:
    conn.execute(
        "INSERT INTO case_classification (case_id, classification, note, annotator_id, version, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (case_id, review.classification, classification_note(review), ANNOTATOR_ID, import_version, now()),
    )


def insert_fields(conn, field_records: list[dict]) -> None:
    conn.executemany(
        "INSERT INTO annotation_fields (case_id, field_path, field_order, current_value, status, correction_reason, source_text_quote, source_text_offset, annotator_id, version, updated_at) "
        "VALUES (:case_id, :field_path, :field_order, :current_value, :status, :correction_reason, :source_text_quote, :source_text_offset, :annotator_id, :version, :updated_at)",
        field_records,
    )


def apply_decisions(review: CaseReview, case: dict, field_records: list[dict], import_version: int) -> tuple[int, int]:
    explicit_incorrect = 0
    explicit_review = 0
    for decision in review.field_decisions:
        targets = resolve_field_targets(field_records, decision.label)
        reason = build_reason(review, decision)
        if not targets:
            targets = [create_missing_field(field_records, case["case_id"], decision.label, decision.status, import_version, reason)]
        for target in targets:
            field_records[target]["status"] = decision.status
            if reason:
                field_records[target]["correction_reason"] = reason
        if decision.status == "INCORRECT":
            explicit_incorrect += len(targets)
        if decision.status == "NEEDS_REVIEW":
            explicit_review += len(targets)
    if review.whole_case_status == "INCORRECT":
        whole_reason = classification_note(review)
        for row in field_records:
            row["status"] = "INCORRECT"
            if whole_reason:
                row["correction_reason"] = whole_reason
    return explicit_incorrect, explicit_review


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json-path", required=True)
    parser.add_argument("--review-path", required=True)
    parser.add_argument("--db-path", default=db.DEFAULT_DB_PATH)
    parser.add_argument("--import-version", type=int, default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    review_text = Path(args.review_path).read_text(encoding="utf-8")
    reviews = split_case_sections(review_text)
    with open(args.json_path, encoding="utf-8") as handle:
        cases = json.load(handle)
    if len(cases) != len(reviews):
        raise ValueError(f"JSON 案件数 {len(cases)} 与 Markdown 案件数 {len(reviews)} 不一致")
    conn = db.init_db(args.db_path)
    import_version = args.import_version if args.import_version is not None else next_import_version(conn)
    reset_tables(conn)
    case_lookup = build_case_index(cases)
    used_case_ids: set[str] = set()
    total_incorrect = 0
    total_needs_review = 0
    created_fields = 0
    for review in reviews:
        case = match_case(review, case_lookup, used_case_ids)
        insert_case(conn, case)
        default_status = case_default_status(review)
        field_records = build_field_records(case, import_version, default_status)
        explicit_incorrect, explicit_review = apply_decisions(review, case, field_records, import_version)
        total_incorrect += explicit_incorrect
        total_needs_review += explicit_review
        created_fields += max(0, len(field_records) - len(parse_extracted_output(case.get("extracted_output", ""))))
        insert_fields(conn, field_records)
        insert_top_level(conn, case, import_version)
        insert_classification(conn, case["case_id"], review, import_version)
    summary = {
        "import_version": import_version,
        "case_count": len(reviews),
        "json_path": args.json_path,
        "review_path": args.review_path,
        "explicit_incorrect_targets": total_incorrect,
        "explicit_needs_review_targets": total_needs_review,
        "created_fields": created_fields,
    }
    conn.execute(
        "INSERT INTO audit_log (case_id, field_path, action, old_value, new_value, annotator_id, ts) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (None, "bulk_import/0612_markdown_review", "UPDATE", None, json.dumps(summary, ensure_ascii=False), ANNOTATOR_ID, now()),
    )
    conn.commit()
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()