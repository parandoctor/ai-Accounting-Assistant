"""统计接口：按类别汇总支出。"""

from typing import Annotated

from fastapi import APIRouter, Query

from app import operations
from app.api.dependencies import ConnDep
from app.core.errors import VALIDATION_ERROR_RESPONSES
from app.schemas import CategoryStat, CategoryStatsResponse, DateRangeQuery

stats_router = APIRouter(
    prefix="/stats",
    tags=["统计"],
    # 422 使用统一 ErrorResponse，见 app/core/errors.py。
    responses=VALIDATION_ERROR_RESPONSES,
)


# 200 示例只用于 OpenAPI 文档展示；真实的 items、金额和 ratio
# 由 operations.category_totals() 的聚合结果和下方计算逻辑决定。
@stats_router.get(
    "/category",
    response_model=CategoryStatsResponse,
    summary="按类别统计支出",
    description=(
        "按类别与时间范围统计支出。\n\n"
        "金额字段为 total_cents，单位为分；items 按金额从高到低排序。"
    ),
    responses={
        200: {
            "content": {
                "application/json": {
                    "example": {
                        "date_from": "2026-11-01T00:00:00Z",
                        "date_to": "2026-11-30T23:59:59Z",
                        "total_cents": 185000,
                        "items": [
                            {"category": "餐饮", "total_cents": 96000, "count": 23, "ratio": 0.519},
                            {"category": "购物", "total_cents": 47000, "count": 5, "ratio": 0.254},
                            {"category": "交通", "total_cents": 42000, "count": 11, "ratio": 0.227},
                        ],
                    }
                }
            }
        }
    },
)
def category_stats(
    params: Annotated[DateRangeQuery, Query()],
    conn: ConnDep,
) -> CategoryStatsResponse:
    """按类别汇总指定时间范围内的支出。

    参数:
        params: DateRangeQuery，包含可选的 from / to；两者都会转成 UTC，
                且 from 不能晚于 to。
        conn: 请求级连接，交给 operations.category_totals()。

    返回:
        CategoryStatsResponse：total_cents 是范围内总支出；
        items 按类别总额降序，每项 ratio = 该类 total_cents / 总 total_cents，
        保留 4 位小数。

    调用关系:
        operations.category_totals() 执行
        GROUP BY category + SUM(amount_cents) + COUNT(id)，
        返回 CategoryTotal 列表和总金额；本函数只负责组装响应和计算 ratio。
    """
    items, total_cents = operations.category_totals(
        conn,
        date_from=params.date_from,
        date_to=params.date_to,
    )

    return CategoryStatsResponse(
        date_from=params.date_from,
        date_to=params.date_to,
        total_cents=total_cents,
        items=[
            CategoryStat(
                category=item.category,
                total_cents=item.total_cents,
                count=item.count,
                # total_cents 为 0 说明范围内没有账目，避免除零，ratio 统一为 0。
                ratio=round(item.total_cents / total_cents, 4) if total_cents else 0.0,
            )
            for item in items
        ],
    )
