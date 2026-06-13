"""Import pipeline for the case-annotation-review platform.

Run once after installing requirements:

    python scripts/import_data.py

Steps:
  1. Init the SQLite schema (storage.db).
  2. Load data/cases_with_extracted_output.json (107 cases) -> cases table.
  3. Flatten each `extracted_output` Markdown into annotation_fields
     (field_path like "II. LIABILITY/Tort-Specific Elements/A. Duty of Care"),
     status = UNREVIEWED.
  4. Seed top_level_corrections from the JSON enums (decision_type, etc.).
  5. Parse data/annotations_thunlp.docx, match CORRECT/INCORRECT marks to
     annotation_fields by (case_name, field_name) and set status; write the
     top line (侵权/非侵权/案件不全) into case_classification.
  6. Anything that cannot be matched is written to import_warnings.log.

The Markdown in the source data is NOT clean -- header levels vary wildly
(`# I.`, `## I.`, `### **I. LEGAL ELEMENT**`, `PART I:`, `[II. LIABILITY]`,
roman numerals sometimes plural). The parsers below are intentionally tolerant.
"""

import json
import os
import re
import sys
import zipfile
from datetime import datetime, timezone

# allow `python scripts/import_data.py` from repo root
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from storage import db  # noqa: E402

DATA_DIR = os.path.join(_REPO_ROOT, "data")
JSON_PATH = os.path.join(DATA_DIR, "cases_with_extracted_output.json")
DOCX_PATH = os.path.join(DATA_DIR, "annotations_0608.docx")
WARN_PATH = os.path.join(_REPO_ROOT, "import_warnings.log")

TOP_LEVEL_FIELDS = [
    "decision_type",
    "procedure_stage",
    "is_liability_suitable",
    "is_specific_tort",
    "tort_cause_of_action_list",
]

_warnings = []


def warn(msg):
    _warnings.append(msg)


def _now():
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# Markdown -> flat field tree
# --------------------------------------------------------------------------- #
ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4}

# strip leading markdown header markers, bold stars, brackets, trailing colons.
def _clean_heading(text):
    t = text.strip()
    t = t.strip("#").strip()
    t = t.strip("*").strip()
    t = t.strip("[]").strip()
    t = re.sub(r"\s+", " ", t)
    return t


def _section_key(line):
    """If `line` is a top-level section header (I/II/III LEGAL ELEMENT/LIABILITY/
    CONCLUSION) return a canonical key, else None.
    """
    t = _clean_heading(line)
    # remove a leading "PART " prefix
    t = re.sub(r"^PART\s+", "", t, flags=re.IGNORECASE)
    m = re.match(r"^(I{1,3}|IV)\s*[\.\):]?\s+(.*)", t)
    if not m:
        return None
    roman = m.group(1)
    rest = m.group(2).strip().rstrip(".").strip()
    rest_up = rest.upper()
    if "LEGAL ELEMENT" in rest_up:
        return "I. LEGAL ELEMENT"
    if "LIABILITY" in rest_up:
        return "II. LIABILITY"
    if "CONCLUSION" in rest_up:
        return "III. CONCLUSION"
    # generic fallback keyed by roman numeral
    return f"{roman}. {rest}" if rest else None


def _is_header_line(raw):
    """Return (level, text) if the line looks like a markdown/bold header."""
    s = raw.strip()
    if not s:
        return None
    # `#`-style
    m = re.match(r"^(#{1,6})\s*(.+)$", s)
    if m:
        return (len(m.group(1)), _clean_heading(m.group(2)))
    # bold-only line like **2. Issue Framing** or **A. Duty of Care**
    m = re.match(r"^\*\*(.+?)\*\*[:：]?\s*$", s)
    if m:
        return (3, _clean_heading(m.group(1)))
    return None


# subsection numbering like "1. Facts", "2. Issue Framing", "1. **Duty of Care**"
_SUBSEC_RE = re.compile(r"^(#{0,6}\s*)?\**\s*(\d+)\s*[\.\)]\s*\**\s*([A-Za-z][^\n]*?)\**\s*$")
# lettered element like "A. Duty of Care", "A) Act", "A. **Defamatory Meaning**"
_ELEMENT_RE = re.compile(r"^(#{0,6}\s*)?\**\s*([A-Z])\s*[\.\)]\s*\**\s*([A-Za-z][^\n]*?)\**[:：]?\s*$")


def parse_extracted_output(md):
    """Flatten a (messy) extracted_output Markdown into ordered field records.

    Returns a list of dicts: {field_path, label, value, order}.
    field_path uses canonical section keys joined by "/".
    A field's value is the text block following its heading up to the next heading.
    """
    if not md:
        return []
    lines = md.replace("\r\n", "\n").split("\n")

    records = []
    order = 0
    current_section = None           # e.g. "II. LIABILITY"
    current_sub = None               # e.g. "1. Tort-Specific Elements"
    current_element = None           # e.g. "A. Negligence" (active tort under LIABILITY)
    # buffer for the field currently accumulating body text
    cur_path = None
    cur_label = None
    cur_buf = []

    def flush():
        nonlocal cur_path, cur_label, cur_buf, order
        if cur_path is not None:
            order += 1
            records.append({
                "field_path": cur_path,
                "label": cur_label,
                "value": "\n".join(cur_buf).strip(),
                "order": order,
            })
        cur_path, cur_label, cur_buf = None, None, []

    for raw in lines:
        # 1) top-level section?
        sec = _section_key(raw)
        if sec is not None:
            flush()
            current_section = sec
            current_sub = None
            current_element = None
            continue

        stripped = raw.strip()
        if not stripped:
            if cur_path is not None:
                cur_buf.append("")
            continue

        # 2) lettered element (Duty of Care etc.) -- only meaningful under LIABILITY
        m_el = _ELEMENT_RE.match(stripped)
        # 3) numbered subsection
        m_sub = _SUBSEC_RE.match(stripped)

        if m_el and current_section == "II. LIABILITY":
            flush()
            label = _clean_heading(f"{m_el.group(2)}. {m_el.group(3)}")
            base = current_sub or "Tort-Specific Elements"
            cur_path = f"{current_section}/{base}/{label}"
            cur_label = label
            current_element = label
            continue

        if m_sub:
            label = _clean_heading(f"{m_sub.group(2)}. {m_sub.group(3)}")
            sect = current_section or "I. LEGAL ELEMENT"
            # "1. Tort-Specific Elements" acts as a sub-grouping header
            if "tort-specific" in label.lower():
                flush()
                current_sub = label
                current_element = None
                cur_path = f"{sect}/{label}"
                cur_label = label
                continue
            # numbered item nested under an active tort element (e.g.
            # "1. Duty of Care" under "A. Negligence") -> keep the tort prefix
            if sect == "II. LIABILITY" and current_element:
                flush()
                base = current_sub or "Tort-Specific Elements"
                cur_path = f"{sect}/{base}/{current_element}/{label}"
                cur_label = label
                continue
            flush()
            current_sub = None
            current_element = None
            cur_path = f"{sect}/{label}"
            cur_label = label
            continue

        # 4) plain `#`/bold header that is not a numbered/lettered field
        hdr = _is_header_line(raw)
        if hdr is not None:
            level, text = hdr
            # skip decorative banners / framework titles
            low = text.lower()
            if any(k in low for k in ("element extraction", "tort cot", "case analysis",
                                      "framework", "based on", "extracting case")):
                continue
            flush()
            sect = current_section or "I. LEGAL ELEMENT"
            cur_path = f"{sect}/{text}"
            cur_label = text
            continue

        # otherwise: body content for the current field
        if cur_path is not None:
            cur_buf.append(raw)
        # content before any field heading is ignored (banners etc.)

    flush()
    # de-dup identical field_paths (keep first, append later as -2)
    seen = {}
    for rec in records:
        fp = rec["field_path"]
        if fp in seen:
            seen[fp] += 1
            rec["field_path"] = f"{fp} ({seen[fp]})"
        else:
            seen[fp] = 1
    return records


# --------------------------------------------------------------------------- #
# docx parsing
# --------------------------------------------------------------------------- #
def _docx_paragraphs(path):
    """Return list of paragraph plain-text strings from a .docx without deps."""
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8", "ignore")
    # split into paragraphs, then strip tags, decode entities
    paras = re.split(r"</w:p>", xml)
    out = []
    for p in paras:
        # join runs: keep only text inside <w:t>..</w:t>
        texts = re.findall(r"<w:t[^>]*>(.*?)</w:t>", p, flags=re.DOTALL)
        text = "".join(texts)
        text = (text.replace("&amp;", "&").replace("&lt;", "<")
                .replace("&gt;", ">").replace("&quot;", '"').replace("&apos;", "'"))
        out.append(text)
    return out


_NAME_RE = re.compile(r"^案件\s*\d+\s*名称[:：]\s*(.+?)\s*$")
_CLASS_RE = re.compile(r"^(侵权案件|非侵权案件|案件不全)")
_STATUS_RE = re.compile(r"(CORRECT|INCORRECT)\s*$", re.IGNORECASE)


def parse_docx_blocks(path):
    """Parse docx into [{name, classification, note, fields:[(label,status)]}].

    A block starts at a `案件N名称：xxx` line and ends at the next one.
    `fields` are (label, status) pairs; label comes from the text before the
    CORRECT/INCORRECT token (or the previous non-status header line for bare
    CORRECT lines such as Defence / Remedy / Tort-Specific Elements).
    """
    paras = _docx_paragraphs(path)
    blocks = []
    cur = None
    pending_label = None  # header line awaiting a bare CORRECT/INCORRECT below it

    for raw in paras:
        line = raw.strip()
        if not line:
            continue

        mname = _NAME_RE.match(line)
        if mname:
            if cur:
                blocks.append(cur)
            cur = {"name": mname.group(1).strip(), "classification": None,
                   "note": None, "fields": []}
            pending_label = None
            continue

        if cur is None:
            continue

        mclass = _CLASS_RE.match(line)
        if mclass:
            raw_cls = mclass.group(1)
            cur["classification"] = {"侵权案件": "侵权", "非侵权案件": "非侵权",
                                     "案件不全": "案件不全"}[raw_cls]
            # capture any trailing note after the classification token
            note = line[len(raw_cls):].lstrip("，,：: ").strip()
            if note:
                cur["note"] = note
            continue

        mstatus = _STATUS_RE.search(line)
        if mstatus:
            status_word = mstatus.group(1).upper()
            # label = everything before the status token on this line
            label_part = line[:mstatus.start()].rstrip(" :：-").strip()
            if label_part:
                label = label_part
            elif pending_label:
                label = pending_label
            else:
                label = None
            if label:
                cur["fields"].append((label, status_word))
            pending_label = None
            continue

        # a non-status header line -> remember it for a following bare status line
        pending_label = line

    if cur:
        blocks.append(cur)
    return blocks


# --------------------------------------------------------------------------- #
# matching docx labels -> annotation_fields.field_path
# --------------------------------------------------------------------------- #
def _norm_label(s):
    """Normalise a field label for fuzzy matching."""
    s = s.lower()
    s = re.sub(r"^[ivx]+\s*[\.\):]\s*", "", s)        # roman prefix
    s = re.sub(r"^[a-z]\s*[\.\):]\s*", "", s)         # A. / B)
    s = re.sub(r"^\d+\s*[\.\):]\s*", "", s)           # 1. / 2)
    s = re.sub(r"[^a-z0-9]+", "", s)                  # drop spaces/punct
    return s


# canonical aliases: normalise common label synonyms onto one token
_ALIASES = {
    "judgementoutcome": "judgement",
    "judgmentoutcome": "judgement",
    "judgement": "judgement",
    "judgment": "judgement",
    "summaryofcourtsfindings": "summary",
    "summaryofcourtfindings": "summary",
    "issueframing": "issueframing",
    "applicablelaw": "applicablelaw",
    "actionabledamage": "actionabledamage",
    "recogniseddamage": "actionabledamage",
    "dutyofcare": "dutyofcare",
    "breachofduty": "breachofduty",
    "defence": "defence",
    "defences": "defence",
    "remedy": "remedy",
    "remedies": "remedy",
    "tortspecificelements": "tortspecific",
}


def _alias(token):
    return _ALIASES.get(token, token)


# section guess for docx labels with no Markdown match, so auto-created fields
# land in a sensible place in the tree.
_SECTION_GUESS = {
    "facts": "I. LEGAL ELEMENT",
    "issueframing": "I. LEGAL ELEMENT",
    "characterisation": "I. LEGAL ELEMENT",
    "applicablelaw": "I. LEGAL ELEMENT",
    "defence": "III. CONCLUSION",
    "remedy": "III. CONCLUSION",
    "judgement": "III. CONCLUSION",
    "summary": "III. CONCLUSION",
}


def _guess_section(label):
    key = _alias(_norm_label(label))
    for frag, sect in _SECTION_GUESS.items():
        if frag in key:
            return sect
    # default: liability tort-specific element
    return "II. LIABILITY/Tort-Specific Elements"


def match_docx_to_fields(conn):
    """Match parsed docx blocks to DB cases/fields and set statuses."""
    blocks = parse_docx_blocks(DOCX_PATH)

    # build name index of DB cases (normalised)
    db_cases = conn.execute("SELECT case_id, case_name FROM cases").fetchall()
    name_index = {}
    for c in db_cases:
        name_index.setdefault(_norm_case_name(c["case_name"]), []).append(c["case_id"])

    used_case_ids = set()
    for blk in blocks:
        norm = _norm_case_name(blk["name"])
        case_id = _best_case_match(norm, name_index, used_case_ids)
        if case_id is None:
            warn(f"[case-unmatched] docx block '{blk['name']}' -> no JSON case")
            continue
        used_case_ids.add(case_id)

        # classification
        if blk["classification"]:
            conn.execute(
                "INSERT INTO case_classification (case_id, classification, note, "
                "annotator_id, version, updated_at) VALUES (?, ?, ?, 'default_user', 1, ?) "
                "ON CONFLICT(case_id) DO UPDATE SET classification=excluded.classification, "
                "note=excluded.note, updated_at=excluded.updated_at",
                (case_id, blk["classification"], blk["note"], _now()),
            )

        # load this case's fields, index by alias-normalised tail label
        rows = conn.execute(
            "SELECT id, field_path FROM annotation_fields WHERE case_id=?", (case_id,)
        ).fetchall()
        field_index = {}
        for r in rows:
            tail = r["field_path"].split("/")[-1]
            key = _alias(_norm_label(tail))
            field_index.setdefault(key, []).append(r["id"])

        for label, status_word in blk["fields"]:
            key = _alias(_norm_label(label))
            target_ids = field_index.get(key)
            if not target_ids:
                # try a contains-match against any field token (length-guarded
                # to avoid spurious matches on very short tokens)
                target_ids = [fid for k, ids in field_index.items()
                              for fid in ids
                              if key and len(key) >= 4 and (key in k or k in key)]
            if not target_ids:
                # Preserve the human's CORRECT/INCORRECT even when the source
                # Markdown has no corresponding field: create the field so the
                # annotation is never lost (annotators may add fields anyway).
                new_path = f"{_guess_section(label)}/{label.strip()}"
                exists = conn.execute(
                    "SELECT id FROM annotation_fields WHERE case_id=? AND field_path=?",
                    (case_id, new_path),
                ).fetchone()
                if exists:
                    fid = exists["id"]
                    conn.execute(
                        "UPDATE annotation_fields SET status=?, updated_at=? WHERE id=?",
                        (status_word, _now(), fid),
                    )
                else:
                    order_row = conn.execute(
                        "SELECT COALESCE(MAX(field_order),0)+1 AS m "
                        "FROM annotation_fields WHERE case_id=?", (case_id,)
                    ).fetchone()
                    conn.execute(
                        "INSERT INTO annotation_fields (case_id, field_path, "
                        "field_order, current_value, status, annotator_id, version, "
                        "updated_at) VALUES (?, ?, ?, '', ?, 'default_user', 1, ?)",
                        (case_id, new_path, order_row["m"], status_word, _now()),
                    )
                    new_id = conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
                    field_index.setdefault(key, []).append(new_id)
                warn(f"[field-created] case '{blk['name']}' label '{label}' "
                     f"({status_word}) -> created '{new_path}' (no Markdown match)")
                continue
            for fid in target_ids:
                conn.execute(
                    "UPDATE annotation_fields SET status=?, updated_at=? WHERE id=?",
                    (status_word, _now(), fid),
                )

    # report JSON cases with no docx block
    for c in db_cases:
        if c["case_id"] not in used_case_ids:
            warn(f"[no-docx] JSON case '{c['case_name']}' has no docx annotation block")


def _norm_case_name(s):
    s = s.lower()
    # drop neutral citations / years / brackets and punctuation
    s = re.sub(r"\[?\d{4}\]?.*$", "", s)          # cut at first year onward
    s = re.sub(r"\b(ltd|anor|ors|the|and|v|vs)\b", " ", s)
    s = re.sub(r"[^a-z0-9]+", "", s)
    return s


def _best_case_match(norm, name_index, used):
    # exact normalised match first (prefer unused)
    cands = name_index.get(norm, [])
    for cid in cands:
        if cid not in used:
            return cid
    if cands:
        return cands[0]
    # prefix / containment fuzzy match
    best = None
    for k, ids in name_index.items():
        if not norm or not k:
            continue
        if norm.startswith(k) or k.startswith(norm) or norm in k or k in norm:
            for cid in ids:
                if cid not in used:
                    return cid
            best = ids[0]
    return best


# --------------------------------------------------------------------------- #
# main import
# --------------------------------------------------------------------------- #
def import_cases(conn, cases):
    for case in cases:
        meta = {k: case.get(k) for k in TOP_LEVEL_FIELDS}
        conn.execute(
            "INSERT OR REPLACE INTO cases (case_id, case_name, case_text, "
            "source_file, original_extracted_output, top_level_meta, imported_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (case["case_id"], case.get("case_name", ""), case.get("case_text", ""),
             case.get("source_file"), case.get("extracted_output", ""),
             json.dumps(meta, ensure_ascii=False), _now()),
        )

        # flatten markdown -> annotation_fields
        records = parse_extracted_output(case.get("extracted_output", ""))
        if not records:
            warn(f"[empty-extract] case '{case.get('case_name')}' produced 0 fields")
        for rec in records:
            conn.execute(
                "INSERT OR IGNORE INTO annotation_fields (case_id, field_path, "
                "field_order, current_value, status, annotator_id, version, updated_at) "
                "VALUES (?, ?, ?, ?, 'UNREVIEWED', 'default_user', 1, ?)",
                (case["case_id"], rec["field_path"], rec["order"],
                 rec["value"], _now()),
            )

        # seed top_level_corrections from JSON enums
        for fname in TOP_LEVEL_FIELDS:
            val = case.get(fname)
            enc = json.dumps(val, ensure_ascii=False)
            conn.execute(
                "INSERT OR IGNORE INTO top_level_corrections (case_id, field_name, "
                "original_value, current_value, status, annotator_id, version, updated_at) "
                "VALUES (?, ?, ?, ?, 'UNREVIEWED', 'default_user', 1, ?)",
                (case["case_id"], fname, enc, enc, _now()),
            )


def main():
    print("== Importing case annotation data ==")
    conn = db.init_db()

    with open(JSON_PATH, encoding="utf-8") as f:
        cases = json.load(f)
    print(f"Loaded {len(cases)} cases from JSON")

    import_cases(conn, cases)
    conn.commit()

    n_fields = conn.execute("SELECT COUNT(*) AS c FROM annotation_fields").fetchone()["c"]
    print(f"Flattened into {n_fields} annotation fields")

    if os.path.exists(DOCX_PATH):
        match_docx_to_fields(conn)
        conn.commit()
        marked = conn.execute(
            "SELECT COUNT(*) AS c FROM annotation_fields WHERE status != 'UNREVIEWED'"
        ).fetchone()["c"]
        cls = conn.execute("SELECT COUNT(*) AS c FROM case_classification").fetchone()["c"]
        print(f"Applied docx marks: {marked} fields with status, {cls} classifications")
    else:
        warn(f"[no-docx-file] {DOCX_PATH} not found")

    with open(WARN_PATH, "w", encoding="utf-8") as f:
        f.write(f"# import warnings ({len(_warnings)} entries)\n")
        for w in _warnings:
            f.write(w + "\n")
    print(f"Wrote {len(_warnings)} warnings to {WARN_PATH}")
    print("== Done ==")


if __name__ == "__main__":
    main()
