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
- 智能助手支持多轮对话：Dify 对话应用或兼容接口（历史由后端保存并随请求带上），都不可用时退回本地单轮解析。
- Streamlit 前端位于 `frontend/`，覆盖记账、查账、统计、预算与智能助手五个功能区。
- 服务启动后可访问 `/docs` 查看交互式接口文档，`GET /openapi.json` 可取完整契约。

## 项目结构

```text
ai-Accounting-Assistant/
├── backend/                        # 后端服务，业务代码 8 个文件
│   ├── app/
│   │   ├── main.py                 # 入口：建库建表、CORS、挂路由、/health 等基础接口
│   │   ├── api.py                  # HTTP 层：请求/响应模型与六个路由
│   │   ├── error.py                # 统一错误结构 {code, message, detail} 与异常处理器
│   │   ├── db.py                   # 存储层：领域枚举、MySQL 连接、建表与全部 SQL
│   │   ├── ai_client.py            # 模型调用：传输重试 + OpenAI 兼容接口 + 字段清洗
│   │   ├── dify.py                 # Dify 接入：任务工作流、文件上传、对话应用
│   │   ├── ai_tasks.py             # 抽取任务：一句话记账、查账解析、小票识别、规则兜底
│   │   └── ai_chat.py              # 多轮对话：Dify / 兼容接口 / 本地兜底三路降级
│   ├── schema.sql                  # 建表脚本，应用启动时读取执行
│   ├── sample.csv                  # 示例流水，可用 MySQL 客户端导入演示
│   ├── .env.example                # 环境变量示例
│   └── Dockerfile
├── frontend/                       # 前端页面
│   ├── app.py                      # 单文件页面：记账、查账、统计、预算、智能助手
│   └── .streamlit/config.toml      # 主题色、上传大小与图表配色
└── docs/                           # 接口文档
```

## 依赖

| 端 | 依赖 |
|---|---|
| 后端 | `fastapi`、`uvicorn[standard]`、`pymysql`、`cryptography`、`python-dotenv`、`httpx`、`python-multipart` |
| 前端 | `streamlit`、`pandas`、`altair`、`requests` |

运行环境要求 Python 3.11+。

## 启动方式

后端（端口 8000）：

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\pip.exe install fastapi "uvicorn[standard]" pymysql cryptography python-dotenv httpx python-multipart
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

启动前先按下一节「接入大模型」把 `backend/.env` 准备好；应用启动时会自动建库建表。

前端（端口 8501）：

```powershell
cd frontend
python -m venv .venv
.\.venv\Scripts\pip.exe install streamlit pandas altair requests
.\.venv\Scripts\python.exe -m streamlit run app.py
```

前端默认请求 `http://127.0.0.1:8000`，可用环境变量 `API_URL` 覆盖（例如后端换了端口时）。

## 接入大模型

1. 打开 `backend/.env.example`，填写两项：

   - `DB_PASSWORD`：本机 MySQL 的 root 密码；
   - `AI_API_KEY`：DeepSeek 密钥，在 https://platform.deepseek.com 申请。

2. 保存后把文件重命名为 `.env`（同在 `backend/` 目录，文件名以点开头）。
   想保留模板的话，复制一份再改名。

3. 重启后端：在后端终端按 `Ctrl + C` 停掉当前进程，再执行启动命令：

   ```powershell
   .\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
   ```

接入模型后，项目可以完成以下功能：

- 一句话记账：把「昨天午饭在沙县吃了 23 块」转成金额、商户、类别、时间四个字段的待确认草稿；
- 自然语言查账：把「这个月吃饭花了多少」转成类别与时间条件，直接返回合计、笔数和明细；
- 小票/截图识别：上传消费凭证图片，识别金额、商户与交易时间；
- 智能助手：多轮对话，能接着上一轮继续追问。

默认使用 DeepSeek 官方接口的 `deepseek-flash` 模型，文本与图片由同一个模型处理，不需要分开配置。

确认接入是否成功：访问 `http://127.0.0.1:8000/ai/status`，`configured` 为 `true` 说明密钥已生效；
或调用 `POST /transactions/parse`，返回的 `engine` 为 `llm` 表示走模型，为 `rule` 表示退回了规则解析。

想换模型或网关时，在 `.env` 里追加下面几项，平时不需要写：

| 变量 | 默认值 | 作用 |
|---|---|---|
| `AI_BASE_URL` | `https://api.deepseek.com/v1` | OpenAI 兼容地址 |
| `AI_MODEL` | `deepseek-flash` | 文本与图片共用这一个模型 |
| `AI_TIMEOUT` / `AI_RETRIES` | `20` / `2` | 超时（秒）与重试次数 |
| `AI_THINKING` | 关闭思考 | 设 `on` 打开深度思考 |

## 待开发领域

| 领域 | 当前状态 | 计划 |
|---|---|---|
| 前端（Streamlit） | 单文件实现记账、查账、统计、预算与智能助手五个功能区；查账与删除已接入真实接口 | 优化交互细节 |
| 账单删除 | 前端已调用 `DELETE /transactions/{id}`，删除前二次确认 | 支持批量删除与撤销 |
| 数据库 | 已接入 MySQL，启动时自动建库建表并补齐索引 | 补充迁移脚本，支持表结构调整与生产实例部署 |
| Docker 部署 | Dockerfile 已准备 | 完成镜像构建、容器编排、环境变量与数据卷联调 |
| AI 文本抽取 | 已接入大模型，输出 JSON 后统一校验；无密钥或调用失败退回规则 | 补抽取准确率评测集，按错误类型迭代提示词 |
| 小票/图片识别 | 图片交给 `deepseek-flash` 直接识别；识别失败时返回空草稿由用户补全 | 补真实小票样本，验证金额与日期识别率 |
| 自然语言查账 | 后端 `/query/parse`、`/query/ask`：问句转类别与时间区间并直接返回结果 | 前端已接入 `/query/ask`，支持快捷问题 |
| 预算管理 | 后端已实现：按月设置总预算/类别预算并给出超支状态 | 前端预算页已接入，后续支持修改历史与提醒推送 |
