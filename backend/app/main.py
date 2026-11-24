"""FastAPI 应用入口。

启动顺序：
1. 导入本模块时创建 app，注册 OpenAPI 元数据；
2. lifespan 调用 db.init_db() 建库建表；
3. 注册 CORS 与统一异常处理器；
4. 挂载记账、查账、小票、统计、预算、智能助手六个路由；
5. 直接定义 /health、/categories、/ai/status 三个基础接口。
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import ai_client, db
from app.error import register_error_handlers
from app.api import (
    AiStatusResponse,
    CategoryOption,
    Category,
    HealthResponse,
    agent_router,
    budgets_router,
    query_router,
    receipts_router,
    stats_router,
    transactions_router,
)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """应用启动时建库建表；已存在的表和数据都不动。"""
    db.init_db()
    yield


app = FastAPI(
    title="记账助手 API",
    version="0.1.0",
    description=(
        "个人记账与消费分析接口。\n\n"
        "## 接口约定\n"
        "- 字段命名：snake_case\n"
        "- 金额：整数，单位「分」，字段名以 `_cents` 结尾\n"
        "- 时间：入参为 ISO 8601 带时区，接口返回 UTC\n"
        '- 错误：`{"code": "...", "message": "...", "detail": {}}`\n'
    ),
    lifespan=lifespan,
    openapi_tags=[
        {"name": "记账", "description": "解析、保存与查询账目"},
        {"name": "查账", "description": "自然语言提问，返回查询条件或直接给结果"},
        {"name": "小票识别", "description": "上传小票或支付截图，生成待确认的草稿"},
        {"name": "智能助手", "description": "多轮对话：由 Dify 或兼容接口维护上下文，本地兜底单轮解析"},
        {"name": "统计", "description": "按类别汇总支出"},
        {"name": "预算", "description": "按月设置预算并判断是否接近或超出上限"},
        {"name": "元数据", "description": "类别下拉框等基础数据"},
        {"name": "运维", "description": "健康检查"},
    ],
)

# 浏览器跨域，先写着备用，streamlit 不用这个；换 React 时可直接复用。
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:8501",
        "http://127.0.0.1:8501",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

register_error_handlers(app)
app.include_router(transactions_router)
app.include_router(query_router)
app.include_router(receipts_router)
app.include_router(stats_router)
app.include_router(budgets_router)
app.include_router(agent_router)


@app.get("/health", tags=["运维"], summary="健康检查", response_model=HealthResponse)
def health() -> HealthResponse:
    """返回服务状态与版本号，用于部署探活。"""
    return HealthResponse(status="ok", version=app.version)


@app.get(
    "/ai/status",
    tags=["运维"],
    summary="大模型接入状态",
    response_model=AiStatusResponse,
)
def ai_status() -> AiStatusResponse:
    """返回当前模型配置，供前端显示「AI 已接入 / 规则模式」。"""
    return AiStatusResponse(
        configured=ai_client.configured(),
        provider=ai_client.provider(),
        model=ai_client.model_name(),
        vision_model=ai_client.vision_model_name(),
        vision_configured=ai_client.vision_configured(),
        missing_config=ai_client.missing_config(),
        fallback="规则解析（关键词 + 正则）",
    )


@app.get(
    "/categories",
    tags=["元数据"],
    summary="列出所有消费类别",
    response_model=list[CategoryOption],
)
def list_categories() -> list[CategoryOption]:
    """返回 Category 枚举的全部取值，供前端下拉框使用。"""
    return [CategoryOption(value=category, label=category.value) for category in Category]
