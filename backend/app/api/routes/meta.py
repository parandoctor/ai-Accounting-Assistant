"""元数据与运维接口：类别下拉、健康检查、模型接入状态。"""

from fastapi import APIRouter

from app.ai import client
from app.core.config import API_VERSION
from app.models import Category
from app.schemas import AiStatusResponse, CategoryOption, HealthResponse

router = APIRouter()


@router.get("/health", tags=["运维"], summary="健康检查", response_model=HealthResponse)
def health() -> HealthResponse:
    """返回服务状态与版本号，用于部署探活。"""
    return HealthResponse(status="ok", version=API_VERSION)


@router.get(
    "/ai/status",
    tags=["运维"],
    summary="大模型接入状态",
    response_model=AiStatusResponse,
)
def ai_status() -> AiStatusResponse:
    """返回当前模型配置，供前端显示「AI 已接入 / 规则模式」。"""
    return AiStatusResponse(
        configured=client.configured(),
        provider=client.provider(),
        model=client.model_name(),
        vision_model=client.vision_model_name(),
        vision_configured=client.vision_configured(),
        missing_config=client.missing_config(),
        fallback="规则解析（关键词 + 正则）",
    )


@router.get(
    "/categories",
    tags=["元数据"],
    summary="列出所有消费类别",
    response_model=list[CategoryOption],
)
def list_categories() -> list[CategoryOption]:
    """返回 Category 枚举的全部取值，供前端下拉框使用。"""
    return [CategoryOption(value=category, label=category.value) for category in Category]
