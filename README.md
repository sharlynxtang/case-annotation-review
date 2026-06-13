# 案件标注人工修正平台 — Phase 1（单人本地版）

法律案件抽取结果（`extracted_output`）的人工修正与金标导出工具。单人本地运行，
三层解耦架构（UI → service → storage），已为 Phase 2 多人协作预留接口。

---

## 目录结构

```
case-annotation-review/
├── data/
│   ├── cases_with_extracted_output.json   # 107 条案件原始数据（输入）
│   ├── annotations_thunlp.docx            # 已有 CORRECT/INCORRECT 基线（输入）
│   └── annotations.db                     # SQLite（import 后生成）
├── storage/
│   └── db.py                  # SQLite schema + 连接管理（WAL），唯一接触 DB 的底层
├── service/
│   └── annotation_service.py  # 纯 Python 业务函数，未来直接被 FastAPI 复用
├── scripts/
│   └── import_data.py         # JSON + Markdown + docx 解析灌库
├── ui/
│   └── streamlit_app.py       # 单文件 Streamlit，只调 service 层
├── requirements.txt
├── README.md
└── import_warnings.log        # import 后生成，列出未匹配项
```

**架构铁律**：UI 只调 `service`，`service` 只调 `storage`。
UI 文件里禁止出现 `sqlite3.connect`。

---

## 阶段1 启动命令

```bash
cd /Users/a1234/.verdent/verdent-projects/case-annotation-review

# 1. 安装依赖（建议先建虚拟环境）
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. 灌库（读 JSON + docx，生成 data/annotations.db）
python scripts/import_data.py

# 3. 启动 UI
streamlit run ui/streamlit_app.py
```

打开浏览器后访问 `http://localhost:8501`。

> 数据持久化在 `data/annotations.db`。关闭 / 重开 Streamlit，已编辑的字段值、状态、
> 理由、引用都会保留。要彻底重置，删除 `data/annotations.db*` 后重跑 `import_data.py`。

---

## 功能说明

- **顶部**：全局进度条 + 按状态 / 分类 / 关键词过滤。
- **左栏**：案件列表，进度色块（⬜ 0% / 🟧 / 🟨 / 🟩 100%），点击切换。
- **主区右侧**：
  - 案件分类切换（侵权 / 非侵权 / 案件不全 + 备注）。
  - 顶层枚举编辑（`decision_type` / `is_liability_suitable` /
    `tort_cause_of_action_list` 多选等）。
  - 左：`case_text` 全文，可搜索高亮；选中文字复制后粘贴到右侧「引用原文」框。
  - 右：按 section 折叠的字段卡片，可编辑 Markdown、切换
    CORRECT / INCORRECT / NEEDS_REVIEW / UNREVIEWED、写修正理由、贴引用、增 / 删字段。
- **底部**：上一条 / 下一条 / 跳转 / 导出金标 JSON（单案 + 全部）。

所有写操作都会写入 `audit_log` 表（谁、何时、什么动作、旧值 → 新值）。

---

## 数据模型与解析说明

### Markdown 扁平化

`extracted_output` 是字符串形式的 Markdown，结构在 107 条里**极不统一**
（`# I.` / `## I.` / `### **I. LEGAL ELEMENT**` / `PART I:` / `[II. LIABILITY]`，
小节用 `## 1.` / `### 1.` / `#### 1.`，元素用 `A. Duty of Care` 等）。

`scripts/import_data.py` 的 `parse_extracted_output` 做容错解析，扁平成
`field_path`，例如：

```
I. LEGAL ELEMENT/1. Facts
I. LEGAL ELEMENT/2. Issue Framing
II. LIABILITY/Tort-Specific Elements/A. Duty of Care
III. CONCLUSION/1. Defences
```

初始 `status = UNREVIEWED`。

### docx 标注匹配

`parse_docx_blocks` 用 `zipfile` 直接读 `word/document.xml`（不依赖 python-docx），
按 `案件N名称：xxx` 分块；顶行（侵权 / 非侵权 / 案件不全）写入 `case_classification`，
每行 `字段名: CORRECT|INCORRECT` 按**案件名模糊匹配 + 字段名别名归一化**写回
`annotation_fields.status`。匹配不上的（案件名 / 字段名 / 无 docx 块）全部写入
`import_warnings.log`。

> 已知数据特点：docx 有 102 个标注块 vs JSON 107 条案件；存在重名块
> （如 `FLR v Chandran` 出现两次）、个别块缺顶行分类、字段标签大量变体
> （全角 `：`、`A.`/`B.` 前缀、`::`、`2.Defence` 等）。解析器对这些做了归一化，
> 残余未匹配项会在 `import_warnings.log` 中逐条列出，便于人工核对。

### SQLite 表

`cases` / `annotation_fields` / `top_level_corrections` / `case_classification` /
`audit_log`。`annotation_fields` 上有唯一索引 `(case_id, field_path)`。
每条标注带 `annotator_id` / `version` / `updated_at`（Phase 2 用）。

---

## 阶段2 升级路径

Phase 1 的所有"多人"列已经在 schema 和函数签名里就位，升级是**迁移**而非重写。

### 1. SQLite → Postgres（pgloader）

```bash
# 安装 pgloader（macOS）
brew install pgloader

# 在 Postgres 建空库
createdb annotations

# 一条命令迁移（schema + 数据）
pgloader ./data/annotations.db postgresql://user:pass@localhost/annotations
```

迁移后把 `storage/db.py` 的连接换成 `psycopg`（其余 service 层 SQL 基本通用，
注意把 `INSERT OR REPLACE` 改成 `INSERT ... ON CONFLICT ... DO UPDATE`，
`AUTOINCREMENT` 改成 `SERIAL/IDENTITY`）。乐观锁 `WHERE version = ?` 已就绪。

### 2. service 层包成 FastAPI（函数零改动）

`service/annotation_service.py` 是纯函数，直接套 `@app` 装饰器即可：

```python
from fastapi import FastAPI, Depends, HTTPException
from pydantic import BaseModel
from service import annotation_service as svc

app = FastAPI()

class UpdateFieldReq(BaseModel):
    new_value: str | None = None
    status: str | None = None
    reason: str | None = None
    source_quote: str | None = None
    expected_version: int | None = None

@app.get("/cases")
def list_cases(status: str | None = None, classification: str | None = None,
               search: str | None = None):
    return svc.list_cases(status, classification, search)

@app.get("/cases/{case_id}")
def get_case(case_id: str):
    detail = svc.get_case_detail(case_id)
    if detail is None:
        raise HTTPException(404)
    return detail

@app.post("/cases/{case_id}/fields/{field_path:path}")
def update_field(case_id: str, field_path: str, req: UpdateFieldReq,
                 user=Depends(current_user)):           # 见下方第 3 点
    try:
        version = svc.update_field(
            case_id, field_path, new_value=req.new_value, status=req.status,
            reason=req.reason, source_quote=req.source_quote,
            annotator_id=user.id, expected_version=req.expected_version)
        return {"version": version}
    except svc.VersionConflict as e:
        raise HTTPException(409, str(e))                # 版本冲突 → 409

@app.get("/progress")
def progress():
    return svc.get_progress()

@app.get("/export")
def export(case_id: str | None = None):
    return svc.export_gold(case_id)
```

### 3. 加用户系统的 3 个改动点

1. **`annotator_id` 来源**：Phase 1 写死 `"default_user"`。Phase 2 改为从登录态取，
   FastAPI 用 `Depends(current_user)` 注入真实用户 id 传给 service 的
   `annotator_id` 参数（签名已支持）。
2. **登录中间件**：加一个鉴权依赖（JWT / session），未登录返回 401；
   `current_user()` 解析 token → 返回用户对象。
3. **版本冲突 UI**：service 已抛 `VersionConflict`，FastAPI 映射为 `409`。
   前端在保存字段时带上当前 `version`，收到 409 时弹"该字段已被他人修改，
   请刷新后重试"，并重新拉取最新值。Phase 1 单人时不传 `expected_version`
   即不校验，行为不变。

### 4. React/Vue 前端对接（直接 fetch service 层 JSON）

service 函数返回的就是 JSON-friendly dict / list，前端直接消费：

```js
// 列表
const cases = await fetch('/cases?status=INCORRECT').then(r => r.json());

// 详情
const detail = await fetch(`/cases/${caseId}`).then(r => r.json());

// 保存字段（带乐观锁版本）
const res = await fetch(
  `/cases/${caseId}/fields/${encodeURIComponent(fieldPath)}`,
  { method: 'POST',
    headers: { 'Content-Type': 'application/json',
               'Authorization': `Bearer ${token}` },
    body: JSON.stringify({ new_value, status, reason, expected_version }) });
if (res.status === 409) showConflictDialog();   // 版本冲突
const { version } = await res.json();

// 导出金标
const gold = await fetch('/export').then(r => r.json());
```

---

## 验收对照

| 验收项 | 对应实现 |
|---|---|
| `python scripts/import_data.py` 一次跑通，107 案件 + docx 入库，warnings.log 列出未匹配 | `scripts/import_data.py` → `import_warnings.log` |
| `streamlit run` 能开图、加载第 1 条、编辑保存、audit_log 有记录 | `ui/streamlit_app.py` + `svc.update_field` 写 `audit_log` |
| 关闭重开，编辑过的值/状态保留 | SQLite 持久化于 `data/annotations.db` |
| 切到第 2 条再切回，状态不丢 | 每次切换从 DB 重新读 `get_case_detail` |
| 导出金标 JSON 结构合理 | `svc.export_gold`（case_id / 字段树 / 状态 / 理由 / 引用） |

---

## 已知限制

- docx 与 JSON 案件数不等（102 块 vs 107 案），且存在重名块 / 个别缺顶行分类；
  模糊匹配尽力而为，残余项见 `import_warnings.log`，需人工核对。当前实测：
  107 案 / 2105 字段入库，807 字段被 docx 标记（740 CORRECT + 67 INCORRECT），
  92 条分类（71 侵权 / 11 案件不全 / 10 非侵权），130 条 warning。
- **docx 标签无对应 Markdown 字段时会自动创建该字段**（warning 标 `[field-created]`），
  以免人工的 CORRECT/INCORRECT 丢失（如 `Summary of Court's Findings` 在
  `extracted_output` 里没有对应节点）。这些字段初始内容为空，仅带状态。
- 2 条案件 `extracted_output` 本身为空 / LLM 拒答（`Stark v Lyddon` 空串；
  `Loughran` 返回 "Apologies, ... not been provided"），故 0 字段，属源数据问题。
- Streamlit 的"框选复制原文"依赖浏览器原生选区 + 手动粘贴到引用框
  （Streamlit 无法直接捕获 DOM 选区）；高亮搜索已内置。
- 单人版未强制乐观锁校验（UI 会传 `expected_version`，但单人无并发冲突）。
