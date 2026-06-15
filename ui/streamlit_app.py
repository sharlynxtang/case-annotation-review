"""Streamlit UI for the case-annotation-review platform (Phase 1, single user).

ARCHITECTURE RULE: this file talks ONLY to service.annotation_service.
It never opens a sqlite connection. All persistence goes through the service
layer so Phase 2 can swap the service for FastAPI calls without touching the UI.

Run:
    streamlit run ui/streamlit_app.py
"""

import os
import sys
import json

import streamlit as st

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from service import annotation_service as svc  # noqa: E402

ANNOTATOR_ID = "default_user"  # Phase 2: comes from auth/session
STATUS_OPTIONS = ["UNREVIEWED", "CORRECT", "INCORRECT", "NEEDS_REVIEW"]
CLASSIFICATION_OPTIONS = ["(未设置)", "侵权", "非侵权", "案件不全"]
STATUS_COLOR = {
    "CORRECT": "#1a7f37",
    "INCORRECT": "#cf222e",
    "NEEDS_REVIEW": "#9a6700",
    "UNREVIEWED": "#57606a",
}

st.set_page_config(page_title="案件标注修正平台", layout="wide")


# --------------------------------------------------------------------------- #
# session helpers
# --------------------------------------------------------------------------- #
def _state():
    if "current_case_id" not in st.session_state:
        st.session_state.current_case_id = None
    if "search" not in st.session_state:
        st.session_state.search = ""


def _select_case(case_id):
    st.session_state.current_case_id = case_id


def _highlight(text, query):
    """Return text with <mark> around query matches (case-insensitive)."""
    if not query:
        return text
    import re
    try:
        pattern = re.compile(re.escape(query), re.IGNORECASE)
    except re.error:
        return text
    return pattern.sub(lambda m: f"<mark>{m.group(0)}</mark>", text)


def _safe_filename(name, fallback, prefix="corrected_", ext=".json", max_len=120):
    """Build a download-safe filename from a case name.

    - Replace filesystem-illegal chars (/ \\ : * ? " < > |) and control chars
      with a space, collapse runs of whitespace, strip leading/trailing junk.
    - Truncate the name portion to ``max_len`` chars.
    - Fall back to ``fallback`` (e.g. case_id) when the name is empty/None.
    """
    import re
    raw = (name or "").strip()
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", raw)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ._-")
    if not cleaned:
        cleaned = str(fallback or "case")
    cleaned = cleaned[:max_len].strip()
    return f"{prefix}{cleaned}{ext}"


# --------------------------------------------------------------------------- #
# top bar: global progress + filters
# --------------------------------------------------------------------------- #
def render_topbar():
    prog = svc.get_progress()
    st.title("案件标注人工修正平台")
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("案件总数", prog["total_cases"])
    c2.metric("已审字段", f"{prog['reviewed']}/{prog['total_fields']}")
    c3.metric("CORRECT", prog["correct"])
    c4.metric("INCORRECT", prog["incorrect"])
    c5.metric("NEEDS_REVIEW", prog["needs_review"])
    st.progress(prog["progress"] / 100.0, text=f"总体进度 {prog['progress']}%")

    f1, f2, f3 = st.columns([2, 2, 3])
    status_filter = f1.selectbox("按字段状态过滤",
                                 ["(全部)"] + STATUS_OPTIONS, key="filter_status")
    class_filter = f2.selectbox("按案件分类过滤",
                                ["(全部)", "侵权", "非侵权", "案件不全"],
                                key="filter_class")
    search = f3.text_input("搜索案件名", key="search_box")
    return (
        None if status_filter == "(全部)" else status_filter,
        None if class_filter == "(全部)" else class_filter,
        search or None,
    )


# --------------------------------------------------------------------------- #
# left column: case list
# --------------------------------------------------------------------------- #
def render_case_list(filter_status, filter_class, search):
    st.subheader("案件列表")
    cases = svc.list_cases(filter_status=filter_status,
                           filter_classification=filter_class, search=search)
    st.caption(f"{len(cases)} 个案件")

    if st.session_state.current_case_id is None and cases:
        st.session_state.current_case_id = cases[0]["case_id"]

    for c in cases:
        cls = c["classification"] or "—"
        bar = _progress_block(c["progress"])
        label = f"{bar} {c['case_name'][:42]}"
        is_current = c["case_id"] == st.session_state.current_case_id
        if st.button(label, key=f"case_{c['case_id']}",
                     use_container_width=True,
                     type="primary" if is_current else "secondary"):
            _select_case(c["case_id"])
            st.rerun()
        st.caption(f"　{cls} · {c['progress']}% · {c['total_fields']} 字段")
    return cases


def _progress_block(pct):
    if pct >= 100:
        return "🟩"
    if pct >= 50:
        return "🟨"
    if pct > 0:
        return "🟧"
    return "⬜"


# --------------------------------------------------------------------------- #
# case_text panel (with search highlight)
# --------------------------------------------------------------------------- #
def render_case_text(case):
    st.subheader("判决书全文 (case_text)")
    q = st.text_input("在全文中搜索高亮", key="text_search")
    text = case.get("case_text") or ""
    st.caption(f"全文 {len(text)} 字符。选中文字复制后，可粘贴到右侧字段的「引用原文」框作为依据。")
    rendered = _highlight(text, q).replace("\n", "<br>")
    st.markdown(
        f"<div style='height:70vh;overflow:auto;border:1px solid #ddd;"
        f"padding:12px;font-size:13px;line-height:1.5;white-space:normal'>"
        f"{rendered}</div>",
        unsafe_allow_html=True,
    )


# --------------------------------------------------------------------------- #
# top-level enum editor
# --------------------------------------------------------------------------- #
def render_top_level(detail):
    case_id = detail["case"]["case_id"]
    with st.expander("顶层枚举 (decision_type / liability / tort list ...)", expanded=False):
        for tl in detail["top_level"]:
            fname = tl["field_name"]
            val = tl["current_value"]
            if fname == "tort_cause_of_action_list":
                current = val if isinstance(val, list) else []
                options = sorted(set(current) | {
                    "negligence", "defamation", "battery", "assault", "nuisance",
                    "trespass", "false imprisonment", "product liability",
                    "harassment", "privacy", "strict liability",
                })
                st.multiselect(fname, options, default=current,
                               key=f"tl_{case_id}_{fname}")
            else:
                st.text_input(fname, value="" if val is None else str(val),
                              key=f"tl_{case_id}_{fname}")


# --------------------------------------------------------------------------- #
# classification editor
# --------------------------------------------------------------------------- #
def render_classification(detail):
    case_id = detail["case"]["case_id"]
    cls = (detail["classification"] or {})
    current = cls.get("classification") or "(未设置)"
    note = cls.get("note") or ""
    with st.expander(f"案件分类: {current}", expanded=False):
        col1, col2 = st.columns([2, 3])
        col1.selectbox("分类", CLASSIFICATION_OPTIONS,
                       index=CLASSIFICATION_OPTIONS.index(current)
                       if current in CLASSIFICATION_OPTIONS else 0,
                       key=f"cls_{case_id}")
        col2.text_input("备注", value=note, key=f"clsnote_{case_id}")


# --------------------------------------------------------------------------- #
# field cards (grouped by section)
# --------------------------------------------------------------------------- #
def render_fields(detail):
    case_id = detail["case"]["case_id"]
    fields = detail["fields"]
    st.subheader("抽取字段 (extracted_output)")
    st.caption("直接在下方正文上原地编辑；状态/修正理由/引用原文收在每个字段的「更多」里。"
               "改完后点页面顶部或底部的「💾 保存本案」一次性落盘。")

    # group by first path segment (section)
    groups = {}
    for f in fields:
        section = f["field_path"].split("/")[0]
        groups.setdefault(section, []).append(f)

    for section, items in groups.items():
        with st.expander(section, expanded=True):
            for f in items:
                _render_field_block(case_id, f)

    _render_add_field(case_id)


def _field_height(text):
    """Auto-size a text_area: short fields stay compact, long ones get room."""
    n = len(text or "")
    if n < 200:
        return 80
    if n < 800:
        return 160
    return 260


def _render_field_block(case_id, f):
    """One field = an in-place editable text_area; metadata folded under 「更多」.

    No per-field save button: edits live in session_state widgets and are
    committed together by the whole-case Save bar (see render_save_bar).
    """
    fp = f["field_path"]
    # label = sub-path without the top-level section prefix
    label = fp.split("/", 1)[-1] if "/" in fp else fp
    color = STATUS_COLOR.get(f["status"], "#57606a")
    st.markdown(
        f"<div style='border-left:4px solid {color};padding-left:8px;margin-top:8px'>"
        f"<b>{label}</b> "
        f"<span style='color:{color};font-size:12px'>[{f['status']}]</span></div>",
        unsafe_allow_html=True,
    )
    st.text_area("字段内容 (Markdown)", value=f["current_value"] or "",
                 key=f"val_{f['id']}", height=_field_height(f["current_value"]),
                 label_visibility="collapsed")
    with st.expander("更多：状态 / 修正理由 / 引用原文 / 删除", expanded=False):
        c1, c2 = st.columns([1, 2])
        c1.selectbox("状态", STATUS_OPTIONS,
                     index=STATUS_OPTIONS.index(f["status"])
                     if f["status"] in STATUS_OPTIONS else 0,
                     key=f"st_{f['id']}")
        c2.text_input("修正理由", value=f["correction_reason"] or "",
                      key=f"rs_{f['id']}")
        st.text_input("引用原文 (source_quote)", value=f["source_text_quote"] or "",
                      key=f"q_{f['id']}")
        if st.button("删除字段", key=f"del_{f['id']}"):
            svc.delete_field(case_id, fp, annotator_id=ANNOTATOR_ID)
            st.warning("字段已删除")
            st.rerun()


def _render_add_field(case_id):
    with st.expander("➕ 新增字段", expanded=False):
        path = st.text_input("field_path (例如 III. CONCLUSION/1. Defences)",
                             key=f"addpath_{case_id}")
        value = st.text_area("初始内容", key=f"addval_{case_id}", height=80)
        if st.button("创建字段", key=f"addbtn_{case_id}"):
            if path.strip():
                try:
                    svc.add_field(case_id, path.strip(), value=value,
                                  annotator_id=ANNOTATOR_ID)
                    st.success("已新增")
                    st.rerun()
                except ValueError as e:
                    st.error(str(e))
            else:
                st.error("field_path 不能为空")


# --------------------------------------------------------------------------- #
# whole-case Save bar (one button commits all edits for the case)
# --------------------------------------------------------------------------- #
def _ss(key, default=None):
    """Read a widget value from session_state if it exists, else default."""
    return st.session_state.get(key, default)


def _commit_case(detail):
    """Diff every widget against the DB snapshot and persist only the changes.

    Covers extracted_output fields + top-level enums + classification.
    Returns (n_changes, errors[]).
    """
    case_id = detail["case"]["case_id"]
    changes = 0
    errors = []

    # 1) annotation fields ---------------------------------------------------
    for f in detail["fields"]:
        new_value = _ss(f"val_{f['id']}", f["current_value"] or "")
        new_status = _ss(f"st_{f['id']}", f["status"])
        new_reason = _ss(f"rs_{f['id']}", f["correction_reason"] or "")
        new_quote = _ss(f"q_{f['id']}", f["source_text_quote"] or "")

        old_value = f["current_value"] or ""
        old_status = f["status"]
        old_reason = f["correction_reason"] or ""
        old_quote = f["source_text_quote"] or ""

        if (new_value == old_value and new_status == old_status
                and new_reason == old_reason and new_quote == old_quote):
            continue
        try:
            svc.update_field(
                case_id, f["field_path"],
                new_value=new_value if new_value != old_value else None,
                status=new_status if new_status != old_status else None,
                reason=new_reason if new_reason != old_reason else None,
                source_quote=new_quote if new_quote != old_quote else None,
                annotator_id=ANNOTATOR_ID,
                expected_version=f["version"],
            )
            changes += 1
        except svc.VersionConflict:
            errors.append(f"字段「{f['field_path']}」版本冲突，请刷新后重试。")
        except Exception as e:  # noqa: BLE001
            errors.append(f"字段「{f['field_path']}」保存失败：{e}")

    # 2) top-level enums -----------------------------------------------------
    for tl in detail["top_level"]:
        fname = tl["field_name"]
        old_val = tl["current_value"]
        key = f"tl_{case_id}_{fname}"
        if key not in st.session_state:
            continue
        new_val = st.session_state[key]
        if fname != "tort_cause_of_action_list":
            # text_input gives a string; original may be None / non-string
            old_cmp = "" if old_val is None else str(old_val)
            if new_val == old_cmp:
                continue
        else:
            old_cmp = old_val if isinstance(old_val, list) else []
            if list(new_val) == list(old_cmp):
                continue
        try:
            svc.update_top_level(case_id, fname, new_val, annotator_id=ANNOTATOR_ID)
            changes += 1
        except Exception as e:  # noqa: BLE001
            errors.append(f"枚举「{fname}」保存失败：{e}")

    # 3) classification ------------------------------------------------------
    cls = detail["classification"] or {}
    old_class = cls.get("classification") or "(未设置)"
    old_note = cls.get("note") or ""
    new_class = _ss(f"cls_{case_id}", old_class)
    new_note = _ss(f"clsnote_{case_id}", old_note)
    if new_class != old_class or new_note != old_note:
        try:
            svc.update_classification(
                case_id, None if new_class == "(未设置)" else new_class,
                note=new_note, annotator_id=ANNOTATOR_ID)
            changes += 1
        except Exception as e:  # noqa: BLE001
            errors.append(f"案件分类保存失败：{e}")

    return changes, errors


def render_save_bar(detail, where="top"):
    """Render one whole-case Save button. `where` keeps widget keys unique."""
    case_id = detail["case"]["case_id"]
    if st.button("💾 保存本案", type="primary", key=f"save_case_{where}_{case_id}",
                 use_container_width=True):
        changes, errors = _commit_case(detail)
        for err in errors:
            st.error(err)
        if changes and not errors:
            st.success(f"已保存 {changes} 处修改")
        elif changes and errors:
            st.warning(f"已保存 {changes} 处修改，但有 {len(errors)} 处失败（见上）")
        elif not changes and not errors:
            st.info("无改动")
        if not errors:
            st.rerun()


# --------------------------------------------------------------------------- #
# bottom nav + export
# --------------------------------------------------------------------------- #
def render_bottom_nav(all_cases, case_id):
    ids = [c["case_id"] for c in all_cases]
    idx = ids.index(case_id) if case_id in ids else 0
    c1, c2, c3, c4 = st.columns([1, 1, 2, 2])
    if c1.button("⬅ 上一条") and idx > 0:
        _select_case(ids[idx - 1]); st.rerun()
    if c2.button("下一条 ➡") and idx < len(ids) - 1:
        _select_case(ids[idx + 1]); st.rerun()
    jump = c3.selectbox("跳转到", [c["case_name"] for c in all_cases],
                        index=idx, key="jump")
    if c3.button("跳转"):
        _select_case(all_cases[[c["case_name"] for c in all_cases].index(jump)]["case_id"])
        st.rerun()

    with c4:
        # ── 原始格式导出（extracted_output 为 Markdown 字符串）──
        orig_one = svc.export_original_format(case_id)
        _case_name = orig_one.get("case_name") if isinstance(orig_one, dict) else None
        st.download_button("导出本案（原始格式）",
                           data=json.dumps(orig_one, ensure_ascii=False, indent=2),
                           file_name=_safe_filename(_case_name, case_id),
                           mime="application/json",
                           help="与 cases_with_extracted_output.json 同格式，extracted_output 为重建 Markdown")
        orig_all = svc.export_original_format()
        st.download_button("导出全部（原始格式）",
                           data=json.dumps(orig_all, ensure_ascii=False, indent=2),
                           file_name="corrected_all.json",
                           mime="application/json",
                           help="所有案件，原始格式，修正后内容")
        # ── 标注平台格式导出（含 status/reason/version 等元数据）──
        with st.expander("高级导出（含标注元数据）"):
            gold_one = svc.export_gold(case_id)
            st.download_button("导出本案金标 JSON（含元数据）",
                               data=json.dumps(gold_one, ensure_ascii=False, indent=2),
                               file_name=f"gold_{case_id}.json", mime="application/json")
            gold_all = svc.export_gold()
            st.download_button("导出全部金标 JSON（含元数据）",
                               data=json.dumps(gold_all, ensure_ascii=False, indent=2),
                               file_name="gold_all.json", mime="application/json")


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main():
    _state()
    filter_status, filter_class, search = render_topbar()

    left, right = st.columns([1, 3])
    with left:
        all_cases = render_case_list(filter_status, filter_class, search)

    case_id = st.session_state.current_case_id
    if not case_id:
        right.info("请从左侧选择一个案件。")
        return

    detail = svc.get_case_detail(case_id)
    if detail is None:
        right.error("案件不存在。")
        return

    with right:
        st.header(detail["case"]["case_name"])
        render_save_bar(detail, where="top")
        render_classification(detail)
        render_top_level(detail)
        col_text, col_fields = st.columns(2)
        with col_text:
            render_case_text(detail["case"])
        with col_fields:
            render_fields(detail)
        st.divider()
        render_save_bar(detail, where="bottom")
        # need full list (unfiltered) for nav to always work
        nav_cases = svc.list_cases()
        render_bottom_nav(nav_cases, case_id)


main()
