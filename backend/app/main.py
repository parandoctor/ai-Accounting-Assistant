"""FastAPI 入口。只做三件事：建应用、挂中间件、挂路由。

启动：uvicorn app.main:app --reload --port 8000
文档：http://127.0.0.1:8000/docs
契约：http://127.0.0.1:8000/openapi.json  ← 这个文件导入 Apifox 就是接口契约
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
        "2026 GDUF AI Studio 二轮考核 · 选题 A（个人记账与消费分析）\n\n"
        "## 约定\n"
        "- 字段命名：snake_case\n"
        "- 金额单位：**分**（整数），字段名以 `_cents` 结尾\n"
        "- 时间：ISO 8601 带时区，数据库存 UTC\n"
        "- 错误：`{\"code\": \"...\", \"message\": \"...\", \"detail\": {}}`\n"
    ),
)

# 开发期允许前端本地端口跨域。上线由 nginx 反代，就不需要这条了。
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
    """compose 的 healthcheck 和评审验证用。"""
    return {"status": "ok", "version": app.version}


@app.get("/categories", tags=["元数据"], summary="列出所有消费类别")
def list_categories() -> list[dict]:
    """前端下拉框直接消费这个接口，避免前端硬编码一套类别。"""
    return [{"value": c.value, "label": c.value} for c in Category]
