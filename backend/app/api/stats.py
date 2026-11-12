"""统计接口。"""

from collections import defaultdict
from datetime import datetime

from fastapi import APIRouter, Query

from app import store
from app.schemas.stats import CategoryStat, CategoryStatsResponse

router = APIRouter(prefix="/stats", tags=["统计"])


@router.get(
    "/category",
    response_model=CategoryStatsResponse,
    summary="按类别统计支出",
    description=(
        "对应题目 MVP 第 2 项「按类别、按时间维度统计」。\n\n"
        "注意金额返回的是 `total_cents`（分），前端展示时除以 100。"
    ),
    responses={
        200: {
            "content": {
                "application/json": {
                    "example": {
                        "date_from": "2026-11-01T00:00:00+08:00",
                        "date_to": "2026-11-30T23:59:59+08:00",
                        "total_cents": 185000,
                        "items": [
                            {"category": "餐饮", "total_cents": 96000, "count": 23, "ratio": 0.519},
                            {"category": "交通", "total_cents": 42000, "count": 11, "ratio": 0.227},
                            {"category": "购物", "total_cents": 47000, "count": 5, "ratio": 0.254},
                        ],
                    }
                }
            }
        }
    },
)
def category_stats(
    date_from: datetime | None = Query(None, alias="from", description="起始时间（含）"),
    date_to: datetime | None = Query(None, alias="to", description="结束时间（含）"),
) -> CategoryStatsResponse:
    items, total_cents = store.category_summary(
        date_from=date_from, date_to=date_to
    )

    grouped: dict[str, list] = defaultdict(list)
    for tx in items:
        grouped[tx.category.value].append(tx)

    stats: list[CategoryStat] = []
    for category_value, txs in grouped.items():
        subtotal = sum(t.amount_cents for t in txs)
        stats.append(
            CategoryStat(
                category=txs[0].category,
                total_cents=subtotal,
                count=len(txs),
                # total 为 0 时避免除零
                ratio=round(subtotal / total_cents, 4) if total_cents else 0.0,
            )
        )

    stats.sort(key=lambda s: s.total_cents, reverse=True)

    return CategoryStatsResponse(
        date_from=date_from,
        date_to=date_to,
        total_cents=total_cents,
        items=stats,
    )
