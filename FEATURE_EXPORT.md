# FEATURE_EXPORT.md — 数据导出功能设计文档

> 作业：为"数据库查询工具"添加数据导出功能
> 项目：db-query-export（FastAPI 后端 + React/Refine 前端，支持 PostgreSQL 与 MySQL）

## 1. 功能概述

在原有"智能数据库查询工具"基础上，新增**数据导出功能模块**，用户可以一键将 SQL 查询结果导出为 **CSV** 或 **JSON** 两种格式的文件。

功能点：

- **双格式导出**：CSV（带 UTF-8 BOM，Excel 打开中文不乱码）与 JSON（含 sql、rowCount、columns、rows 元信息）
- **一键自动化**：单个 API 端点 / 单条命令完成"执行查询 + 格式化 + 生成文件"全流程
- **主动交互**：查询完成后，界面主动提示"需要将这次查询结果导出为 CSV 或 JSON 文件吗？"，点击即可导出

## 2. 设计思路

### 2.1 代码库理解与切入点

通过 AI 工具快速梳理了现有代码结构，确定扩展切入点：

```
backend/
├── app/
│   ├── api/v1/queries.py         # 查询相关 API（POST /{name}/query 等）← 在此新增导出端点
│   ├── services/
│   │   ├── query_wrapper.py      # 现有查询执行入口（复用，不重复造轮子）
│   │   └── export_service.py     # 【新增】导出格式化服务
│   └── models/schemas.py         # 【新增】ExportInput 请求模型
└── scripts/
    └── export_query.py           # 【新增】一键导出命令行工具

frontend/
└── src/pages/
    ├── Home.tsx                  # 【修改】主查询界面：导出按钮 + 主动询问提示条
    └── queries/execute.tsx       # 【修改】查询执行页：同步支持导出
```

选择 `api/v1/queries.py` 作为切入点的原因：导出本质上是"查询"的延伸（先执行查询，再格式化输出），与查询端点同属一个资源路由 `/{name}/query/*`，语义清晰且可直接复用现有的查询执行链路（含 SQL 校验、查询历史记录）。

### 2.2 AI Agent 任务分解

按照作业要求，将"导出数据"这个复杂任务分解为三个子任务，由 Agent 协调处理：

| 子任务 | 实现位置 | 说明 |
|--------|----------|------|
| 1. 获取查询结果 | `execute_query_with_service()` | 复用现有查询服务，自动适配 PostgreSQL/MySQL，自动记录查询历史 |
| 2. 格式化数据 | `export_service.render()` | CSV 用 `csv.writer`（自动处理逗号/引号转义），JSON 用 `json.dumps`（自定义 serializer 处理 date/Decimal 等非原生类型） |
| 3. 创建文件 | HTTP `Response` + `Content-Disposition` | 后端返回文件流，浏览器/CLI 保存为文件 |

三个子任务在单个 API 调用中串联完成，对调用方而言就是"一键导出"。

### 2.3 关键技术决策

1. **导出在后端完成，而非前端拼接**
   - 前端拼接 CSV 无法保证编码正确（Excel 打开无 BOM 的 UTF-8 会中文乱码）
   - 后端生成的 CSV 使用 `utf-8-sig` 编码写入 BOM（`EF BB BF`），Excel 可正确识别
   - 后端导出与命令行工具、API 调用共享同一套格式化逻辑，行为一致

2. **端点设计为"查询+导出"一步完成**
   - `POST /api/v1/dbs/{name}/query/export` 接收 `{ sql, format }`
   - 天然满足作业"执行查询和导出结果一键完成"的要求
   - 文件名自动带时间戳：`interview_db_export_20260819_093520.csv`，并通过 RFC 5987 `filename*` 兼容非 ASCII 文件名

3. **中文编码全链路处理**
   - MySQL 连接使用 `utf8mb4`
   - CSV 导出使用 `utf-8-sig`（带 BOM）
   - JSON 导出使用 `ensure_ascii=False`
   - 测试数据库导入脚本（`scripts/import_db.ps1`）自动检测源 SQL 文件的 UTF-8/GBK 编码并统一转为 UTF-8 导入

## 3. API 设计

### POST /api/v1/dbs/{name}/query/export

请求：

```json
{
  "sql": "SELECT id, first_name, last_name FROM candidates LIMIT 10",
  "format": "csv"
}
```

响应（文件下载）：

```
HTTP/1.1 200 OK
Content-Type: text/csv; charset=utf-8
Content-Disposition: attachment; filename=interview_db_export_20260819_093520.csv; filename*=UTF-8''...
```

错误响应：

- `400` 不支持的导出格式 / SQL 校验失败
- `404` 数据库连接不存在
- `500` 查询执行失败

## 4. 用户交互设计

作业要求："AI 助手可以主动询问'需要将这次查询结果导出为 CSV 或 JSON 文件吗？'"

实现方式：查询成功返回结果后，结果表格上方自动出现蓝色提示条（Ant Design `Alert`）：

> 💡 需要将这次查询结果导出为 CSV 或 JSON 文件吗？　[导出 CSV] [导出 JSON]

同时结果卡片右上角保留 `EXPORT CSV` / `EXPORT JSON` 按钮。点击任一按钮：

1. 调用后端 `POST /query/export`（responseType: blob）
2. 浏览器自动触发文件下载
3. 显示成功消息："已导出 N 行数据为 CSV 文件"

超过 10000 行时弹出二次确认对话框，防止大数据量导出占用过多内存。

## 5. 自动化流程（一键导出命令行工具）

`backend/scripts/export_query.py` 提供命令行一键导出，一条命令完成"连接 → 查询 → 导出"：

```bash
cd backend
uv run python scripts/export_query.py \
    --db interview_db \
    --sql "SELECT id, first_name, last_name, current_company FROM candidates LIMIT 10" \
    --format csv \
    --out candidates.csv
```

输出：

```
[1/3] Executing query on 'interview_db' ...
      SELECT id, first_name, last_name, current_company FROM candidates LIMIT 10
[2/3] Result formatted as CSV
[3/3] File created: C:\...\candidates.csv (146 bytes)
```

参数说明：

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--db` | 已注册的数据库连接名（必填） | - |
| `--sql` | 要执行的 SQL（必填） | - |
| `--format` | 导出格式 csv/json | csv |
| `--out` | 输出文件路径 | `<db>_export_<时间戳>.<格式>` |
| `--api-base` | 后端地址 | http://localhost:8000 |

## 6. 工具链整合思考（Cursor + Claude Code）

- **Cursor 的优势（快速迭代与代码生成）**：本次在 Cursor 中快速理解了现有代码结构（查询链路、schema 约定、camelCase API 风格），并在既有模式下生成新端点与前端组件，保证新代码与项目风格一致。
- **Claude Code 的优势（多步骤自动化）**：将"启动 MySQL → 导入测试数据 → 启动服务 → 执行查询 → 导出文件"这类多步骤流程交给 Agent 自动完成，例如本次数据库环境修复（发现并修复了 my.ini 中已废弃的 `default_authentication_plugin` 配置导致服务无法启动的问题）和编码检测导入脚本，都是 Agent 自主诊断、分解、修复的典型案例。
- **结合方式**：Cursor 负责"写对代码"，Claude Code 负责"跑通流程"，两者互补。

## 7. 测试验证

| 测试项 | 结果 |
|--------|------|
| CSV 导出接口（POST /query/export, format=csv） | 通过，文件带 UTF-8 BOM（EF BB BF），中文正常 |
| JSON 导出接口（format=json） | 通过，含 sql/rowCount/columns/rows 元信息，中文不转义 |
| 不支持的格式（如 format=xlsx） | 返回 400 与明确错误信息 |
| 命令行一键导出（CSV/JSON） | 通过，文件正确生成 |
| 前端交互：查询后出现导出提示条 | 通过 |
| 前端交互：点击导出按钮触发下载并提示成功 | 通过（"已导出 10 行数据为 CSV 文件"） |
| 中文数据端到端（MySQL utf8mb4 → API → 文件） | 通过（张明/阿里巴巴 等数据完整） |

测试数据：`interview_db` 面试管理库（12 张表，50 名候选人），由 `backend/scripts/import_db.ps1` 一键导入。

## 8. 运行方式

```bash
# 1. 启动 MySQL（确保 interview_db 已导入：backend/scripts/import_db.ps1）
# 2. 启动后端
cd backend && uv run uvicorn app.main:app --reload --port 8000
# 3. 启动前端
cd frontend && npm run dev
# 4. 浏览器打开前端地址，选择 interview_db，执行查询，点击导出
```

## 9. 后续可扩展方向

- 支持 Excel（.xlsx）格式（需引入 openpyxl）
- 流式导出超大数据集（StreamingResponse 分批写入）
- 导出历史记录与文件管理
- 自然语言直接导出："把候选人的联系方式导出成 CSV"（NL2SQL + 导出端点串联）
