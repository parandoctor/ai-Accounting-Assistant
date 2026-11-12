"""FastAPI 应用入口：创建应用、注册中间件与路由。

启动：uvicorn app.main:app --reload --port 8000
文档：http://127.0.0.1:8000/docs
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import receipts, stats, transactions
from app.core.errors import register_error_handlers
from app.schemas.transaction import Category

app = FastAPI(
    title="记账助手 API",
    version="0.1.0",
    description=(
        "个人记账与消费分析接口。\n\n"
        "## 接口约定\n"
        "- 字段命名：snake_case\n"
        "- 金额：整数，单位「分」，字段名以 `_cents` 结尾\n"
        "- 时间：ISO 8601 带时区，数据库存储 UTC\n"
        "- 错误：`{\"code\": \"...\", \"message\": \"...\", \"detail\": {}}`\n"
    ),
)

# 开发期允许本地前端跨域。
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

register_error_handlers(app)

app.include_router(transactions.router)
app.include_router(receipts.router)
app.include_router(stats.router)


@app.get("/health", tags=["运维"], summary="健康检查")
def health() -> dict:
    """返回服务状态与版本号，供容器探针与可用性检测使用。"""
    return {"status": "ok", "version": app.version}


@app.get("/categories", tags=["元数据"], summary="列出所有消费类别")
def list_categories() -> list[dict]:
    """返回全部消费类别，作为下拉框数据源。"""
    return [{"value": c.value, "label": c.value} for c in Category]
