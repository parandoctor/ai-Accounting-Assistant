# AI 记账助手（AI Accounting Assistant）

广东金融学院第二十一组智能体项目。

面向个人记账场景的智能体：用户可以用一句话描述消费，或上传小票、支付截图；系统把内容转换为结构化账目，经确认后保存，并提供账目查询、分类统计与预算提醒。

后端接口与数据层已完成，Streamlit 前端已接入；文本抽取、自然语言查账、小票识别都走大模型，未配置模型密钥时自动退回规则解析，功能不中断。

## 技术栈

- Python 3.11+
- FastAPI + Pydantic v2
- 大模型：OpenAI 兼容接口，或 Dify 工作流（`AI_PROVIDER` 切换）
- PyMySQL + MySQL 8
- Uvicorn
- Streamlit（前端页面）

## 当前进度

版本：`v0.1.0`（2026-11）

- 已完成记账核心接口：一句话解析、保存账目、账目查询、分类统计。
- 已完成预算接口：按月设置总预算或类别预算，返回已用比例和超支状态。
- 数据层直连 MySQL，全部使用原生 SQL；应用启动时自动建库、建表并补齐缺失的列与索引。
- 接口统一使用 `snake_case`；金额以「分」为单位存储，时间统一使用 UTC。
- 错误响应统一为 `{code, message, detail}`，参数校验、404、405 使用同一结构。
- 已提供小票/支付截图上传接口：校验文件类型与大小后交给视觉模型抽取，未配置视觉模型时返回空草稿由用户补全。
- 大模型走 OpenAI 兼容接口，也可切到 Dify 工作流；配置项见 `backend/.env.example`，接入状态可查 `GET /ai/status`。
- Streamlit 前端位于 `frontend/`，单文件覆盖记账、查账、统计三个功能区；预算页待补充。
- OpenAPI 契约可由 `backend/export_openapi.py` 导出；服务启动后可访问 `/docs` 查看交互式接口文档。

## 项目结构

```text
ai-Accounting-Assistant/
├── backend/                        # 后端服务
│   ├── app/
│   │   ├── main.py                 # 应用入口：初始化数据库、中间件、路由
│   │   ├── db.py                   # MySQL 连接、建表与按请求注入连接
│   │   ├── store.py                # 数据访问层：账目、草稿、预算的读写
│   │   ├── api/
│   │   │   ├── transactions.py     # 解析、保存、查询、删除账目
│   │   │   ├── query.py            # 自然语言查账：解析条件、直接返回结果
│   │   │   ├── receipts.py         # 小票/截图上传与草稿
│   │   │   ├── stats.py            # 分类统计
│   │   │   └── budgets.py          # 预算设置、查询与超支状态
│   │   ├── schemas/
│   │   │   ├── common.py           # 时间范围查询参数
│   │   │   ├── transaction.py      # 记账请求/响应与枚举
│   │   │   ├── query.py            # 查账请求与响应
│   │   │   ├── stats.py            # 统计响应
│   │   │   ├── budget.py           # 预算请求/响应
│   │   │   └── meta.py             # 类别、健康检查与大模型状态响应
│   │   ├── ai/
│   │   │   ├── llm.py              # 大模型调用：OpenAI 兼容接口 / Dify 工作流
│   │   │   ├── parsing.py          # 模型输出字段清洗（金额、时间、类别）
│   │   │   ├── rules.py            # 规则兜底：关键词与正则
│   │   │   ├── text_extract.py     # 一句话记账的字段抽取
│   │   │   ├── query_extract.py    # 自然语言查账的条件解析
│   │   │   └── receipt_ocr.py      # 小票与支付截图识别
│   │   └── core/
│   │       └── errors.py           # 统一错误结构与异常处理
│   ├── export_openapi.py           # 导出 OpenAPI 契约
│   ├── schema.sql                  # 建表脚本，应用启动时读取执行
│   ├── sample.csv                  # 示例流水，可用 MySQL 客户端导入演示
│   ├── requirements.txt
│   ├── .env.example                # 环境变量示例
│   └── Dockerfile
├── frontend/                       # 前端页面
│   ├── app.py                      # 单文件页面：记账、查账、统计三个功能区（预算页待补充）
│   ├── pyproject.toml              # 依赖声明（streamlit、pandas、altair、requests）
│   └── uv.lock                     # 依赖锁定
├── docs/                           # 接口文档
├── pyproject.toml                  # 后端依赖声明
└── uv.lock                         # 后端依赖锁定
```

## 启动方式

后端（端口 8000）：

```powershell
cd backend
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
```

数据库连接参数写在 `backend/.env`（复制 `backend/.env.example` 后填入 MySQL 账号密码），
应用启动时会自动建库建表。

前端（端口 8501）：

```powershell
cd frontend
uv venv
uv pip install -r requirements.txt
.\.venv\Scripts\python.exe -m streamlit run app.py
```

注意 `frontend/` 只有 `requirements.txt`、没有 `pyproject.toml`，所以不能用 `uv sync`；
也不要写 `uv run streamlit`：`uv run` 会向上找到仓库根目录的 `pyproject.toml` 并复用根目录的 `.venv`，
那里没装 streamlit，会报 `Failed to spawn: streamlit`。

前端默认请求 `http://127.0.0.1:8000`，可用环境变量 `API_URL` 覆盖（例如后端换了端口时）。

## 待开发领域

| 领域 | 当前状态 | 计划 |
|---|---|---|
| 前端（Streamlit） | 单文件实现记账、查账、统计三个功能区；查账已接入真实接口 | 补充预算页，优化交互细节 |
| 账单删除 | 后端已提供 `DELETE /transactions/{id}`，前端暂未接入 | 前端改为调用删除接口并加二次确认 |
| 数据库 | 已接入 MySQL，启动时自动建库建表并补齐索引 | 补充迁移脚本，支持表结构调整与生产实例部署 |
| Docker 部署 | Dockerfile 已准备 | 完成镜像构建、容器编排、环境变量与数据卷联调 |
| AI 文本抽取 | 已接入大模型，输出 JSON 后统一校验；无密钥或调用失败退回规则 | 补抽取准确率评测集，按错误类型迭代提示词 |
| 小票/图片识别 | 视觉模型抽取，未配置 `AI_VISION_MODEL` 时返回空草稿 | 补真实小票样本，验证金额与日期识别率 |
| 自然语言查账 | 后端 `/query/parse`、`/query/ask`：问句转类别与时间区间并直接返回结果 | 前端接入该接口，替换占位页 |
| 预算管理 | 后端已实现：按月设置总预算/类别预算并给出超支状态 | 前端预算页待补充，后续支持修改历史与提醒推送 |
