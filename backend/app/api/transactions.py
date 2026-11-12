"""记账接口：解析、保存与查询。"""

import re
from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Query, status

from app import store
from app.schemas.transaction import (
    Category,
    CreateTransactionRequest,
    ParseTextRequest,
    Source,
    Transaction,
    TransactionDraft,
    TransactionList,
)

router = APIRouter(prefix="/transactions", tags=["记账"])

# 关键词到消费类别的映射，用于规则匹配。
_KEYWORD_CATEGORY = {
    "饭": Category.FOOD, "午餐": Category.FOOD, "晚饭": Category.FOOD,
    "早饭": Category.FOOD, "外卖": Category.FOOD, "咖啡": Category.FOOD,
    "打车": Category.TRANSPORT, "地铁": Category.TRANSPORT, "公交": Category.TRANSPORT,
    "加油": Category.TRANSPORT,
    "买": Category.SHOPPING, "淘宝": Category.SHOPPING, "超市": Category.SHOPPING,
    "电影": Category.ENTERTAINMENT, "游戏": Category.ENTERTAINMENT,
    "药": Category.MEDICAL, "医院": Category.MEDICAL,
    "房租": Category.HOUSING, "水电": Category.HOUSING,
}

_DRAFT_EXAMPLE = {
    "draft_id": "6f1c9b2e8a4d4c1f9b7e5a3d2c1b0a9e",
    "source": "text",
    "amount_cents": 2500,
    "merchant": None,
    "occurred_at": "2026-11-11T12:30:00+08:00",
    "category": "餐饮",
    "confidence": 0.75,
    "missing_fields": ["merchant"],
    "raw_text": "午饭 25，和同事 AA",
}


@router.post(
    "/parse",
    response_model=TransactionDraft,
    summary="一句话记账：解析消费信息",
    description=(
        "将自然语言转换为结构化记账信息。\n\n"
        "amount_cents 可能为 null，表示未解析出金额，补齐后调用 POST /transactions 保存。"
    ),
    responses={200: {"content": {"application/json": {"example": _DRAFT_EXAMPLE}}}},
)
def parse_text(payload: ParseTextRequest) -> TransactionDraft:
    """从自然语言中提取金额与消费类别。"""
    text = payload.text

    # 提取文本中的第一个数值作为金额。
    match = re.search(r"(\d+(?:\.\d{1,2})?)", text)
    amount_cents: int | None = None
    if match:
        # 使用 Decimal 避免浮点乘法误差。
        amount_cents = int((Decimal(match.group(1)) * 100).to_integral_value())

    category: Category | None = None
    for keyword, value in _KEYWORD_CATEGORY.items():
        if keyword in text:
            category = value
            break

    draft = TransactionDraft(
        draft_id=store.new_draft_id(),
        source=Source.TEXT,
        amount_cents=amount_cents,
        merchant=None,
        # 规则匹配不涉及时间解析。
        occurred_at=None,
        category=category,
        confidence=0.4,
        missing_fields=[
            name
            for name, value in (
                ("amount_cents", amount_cents),
                ("merchant", None),
                ("occurred_at", None),
                ("category", category),
            )
            if value is None
        ],
        raw_text=text,
    )
    return store.save_draft(draft)


@router.post(
    "",
    response_model=Transaction,
    status_code=status.HTTP_201_CREATED,
    summary="保存一笔账",
    description="保存一笔已确认的账目。",
    responses={
        201: {
            "content": {
                "application/json": {
                    "example": {
                        "id": 1,
                        "amount_cents": 2500,
                        "merchant": "食堂",
                        "occurred_at": "2026-11-11T12:30:00+08:00",
                        "category": "餐饮",
                        "source": "text",
                        "note": "和同事 AA",
                        "created_at": "2026-11-11T12:31:05+08:00",
                    }
                }
            }
        }
    },
)
def create_transaction(payload: CreateTransactionRequest) -> Transaction:
    # 草稿已确认，移除后再次提交同一 draft_id 不会重复建账。
    if payload.draft_id:
        store.delete_draft(payload.draft_id)

    return store.create_transaction(
        amount_cents=payload.amount_cents,
        merchant=payload.merchant,
        occurred_at=payload.occurred_at,
        category=payload.category,
        note=payload.note,
    )


@router.get(
    "",
    response_model=TransactionList,
    summary="查询账目列表",
    description="支持时间范围、类别筛选与分页。",
)
def list_transactions(
    # from 为 Python 关键字，对外参数名仍为 from。
    date_from: datetime | None = Query(None, alias="from", description="起始时间（含）"),
    date_to: datetime | None = Query(None, alias="to", description="结束时间（含）"),
    category: Category | None = Query(None, description="按类别筛选"),
    limit: int = Query(20, ge=1, le=100, description="每页条数"),
    offset: int = Query(0, ge=0, description="偏移量"),
) -> TransactionList:
    items, total = store.list_transactions(
        date_from=date_from,
        date_to=date_to,
        category=category,
        limit=limit,
        offset=offset,
    )
    return TransactionList(items=items, total=total)
