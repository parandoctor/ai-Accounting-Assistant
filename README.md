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
├── backend/                        # 后端服务：接口按业务分文件，基础件集中
│   ├── app/
│   │   ├── main.py                 # 入口：建库建表、CORS、异常处理器、挂路由
│   │   ├── api/
│   │   │   ├── main.py             # 路由汇总，把各业务 router 挂到 api_router
│   │   │   ├── dependencies.py     # 路由依赖：请求级数据库连接
│   │   │   └── routes/             # 一个业务一个文件
│   │   │       ├── transactions.py # 记账：解析、保存、列表、删除
│   │   │       ├── receipts.py     # 小票识别
│   │   │       ├── query.py        # 自然语言查账
│   │   │       ├── stats.py        # 分类统计
│   │   │       ├── budgets.py      # 月度预算
│   │   │       ├── agent.py        # 智能助手
│   │   │       └── meta.py         # 类别、健康检查、AI 状态
│   │   ├── core/
│   │   │   ├── config.py           # 配置：数据库参数、脚本路径、版本号
│   │   │   ├── database.py         # 连接、建库建表与补列
│   │   │   └── errors.py           # 统一错误结构 {code, message, detail}
│   │   ├── ai/
│   │   │   ├── client.py           # 模型调用：传输重试 + 兼容接口 + 字段清洗
│   │   │   ├── dify.py             # Dify 接入：任务工作流、文件上传、对话应用
│   │   │   ├── tasks.py            # 抽取任务：一句话记账、查账解析、小票识别、规则兜底
│   │   │   └── chat.py             # 多轮对话：Dify / 兼容接口 / 本地兜底三路降级
│   │   ├── models.py               # 类别与来源枚举、数据库行结构
│   │   ├── schemas.py              # 接口请求与响应模型
│   │   ├── operations.py           # 全部 SQL 与数据库行映射
│   │   └── utils.py                # 时区校验、金额元分换算
│   ├── schema.sql                  # 建表脚本，应用启动时读取执行
│   ├── sample.csv                  # 示例流水，可用 MySQL 客户端导入演示
│   ├── .env.example                # 环境变量示例
│   └── Dockerfile
├── frontend/                       # 前端页面
│   ├── app.py                      # 入口：页面配置、侧边栏导航与页面分发
│   ├── config.py                   # 后端地址、时区、类别与图表配色
│   ├── api_client.py               # 接口请求封装与错误转换
│   ├── formatting.py               # 金额、时间格式化与 CSV 导出
│   ├── components.py               # 全局样式、空状态、进度条等通用组件
│   ├── views/                      # 五个功能页：record / query / stats / budget / assistant
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

## 团队分工

| 成员 | 负责模块 | 主要产出 | 答辩主讲 |
|---|---|---|---|
| `parandoctor`（组长） | 后端与 AI 接入 | FastAPI 接口、MySQL 数据层、大模型接入、多轮对话、后端重构与 README 维护 | 整体架构、AI 调用与降级策略 |
| `K931K` | 前端 | Streamlit 五个功能页、预算 / 查账 / 统计交互、主题样式、前端模块化与 bug 修复 | 前端结构、交互设计与指标口径 |
| `hjt_0123` | 数据库 | 建表脚本 `schema.sql`、索引与字段设计、示例数据 `sample.csv` | 数据模型与索引设计 |
| `wuwu57` | 测试与文档 | 接口测试报告、缺陷记录、方案文档维护 | 测试覆盖与典型缺陷 |
| `5050-all` | 前端初版与素材 | 初版 Streamlit 页面、页面截图与演示素材 | 演示与素材准备 |

> 分工按仓库提交记录与文档整理，答辩前请各自确认所负责的部分。

## 功能清单与验证方式

### 必做功能（MVP）

| 功能 | 入口 | 操作步骤 | 预期结果 |
|---|---|---|---|
| 一句话记账 | 记账页 | 输入「午饭 25」→ 点「识别文字」→ 补商户 → 点「保存账单」 | 提示保存成功，统计页出现该笔 25 元账目 |
| 小票 / 截图识别 | 记账页 | 上传小票或支付截图 → 点「识别小票」 | 已配置视觉模型时回填金额、商户、时间；未配置时提示并允许手动填写 |
| 分类统计 | 统计页 | 切换时间范围（本月 / 上月 / 近 30 天 / 今年 / 自定义） | 显示总支出、笔数、类别数与最大类别，柱状图和饼图按类别分布 |
| 自然语言查账 | 查账页 | 输入问题或点「这个月吃饭花了多少钱」 | 返回结论、合计金额、命中笔数与时间范围，明细与统计页口径一致 |
| 预算与超支提醒 | 预算页 | 选月份 → 设置总预算 2000 元、购物预算 50 元 | 进度条显示已用比例；接近上限标黄，超出标红并显示超支金额 |

### 增强功能

| 功能 | 入口 | 操作步骤 | 预期结果 |
|---|---|---|---|
| 月度趋势 | 统计页 | 向下滚动查看趋势区 | 近 6 个月支出折线图，数值与各月账单一致 |
| 环比 | 统计页 | 切换「本月 / 上月 / 近 30 天」 | 总支出旁显示与上一等长周期的涨跌百分比 |
| 导出明细 | 统计页 | 点「导出 CSV」 | 下载文件可用 Excel 打开，中文不乱码 |
| 删除账单 | 统计页 | 选择一条 → 删除这笔账单 → 确认 | 列表消失，数据库同步删除 |
| 智能助手 | 智能助手页 | 点示例问法或输入「午饭 25」 | 已配置模型时多轮回复；未配置时退回本地解析，功能不中断 |

### 工程与降级验证

| 项目 | 验证方式 |
|---|---|
| 自动建库建表 | 换空数据库启动后端，`GET /health` 返回 ok，表自动创建 |
| 接口文档 | 启动后访问 `/docs`，记账 / 查账 / 小票 / 统计 / 预算 / 助手 六组路由齐全 |
| AI 接入状态 | `GET /ai/status` 的 `configured` 为 `true` 说明密钥生效 |
| 无密钥降级 | 不填 `AI_API_KEY` 启动，记账、查账仍可用，返回的 `engine` 为 `rule` |
| 统一错误结构 | 任意非法请求返回 `{code, message, detail}` |
| 示例数据 | `backend/sample.csv` 可直接导入 MySQL 用于演示 |

已知限制：小票识别依赖视觉模型配置，未配置时只返回空草稿；多轮对话未配置模型时退化为单轮解析；Docker 一键启动尚未完成（见下节）。

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
