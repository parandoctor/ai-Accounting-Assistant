"""记账相关的请求 / 响应模型。

三条全组必须遵守的约定，写在这里就是唯一来源：
1. 字段命名一律 snake_case（前端自己转 camelCase）
2. 金额一律用「分」为单位的整数 amount_cents，绝不用浮点
3. 时间一律 ISO 8601 带时区，数据库存 UTC
"""

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class Category(str, Enum):
    """消费类别。前端下拉框、数据库取值、提示词都必须用这一套，不许各自发挥。"""

    FOOD = "餐饮"
    TRANSPORT = "交通"
    SHOPPING = "购物"
    ENTERTAINMENT = "娱乐"
    MEDICAL = "医疗"
    HOUSING = "居住"
    OTHER = "其他"


class Source(str, Enum):
    """这笔账从哪来的。"""

    TEXT = "text"
    RECEIPT = "receipt"


class ParseTextRequest(BaseModel):
    """一句话记账的请求体。"""

    text: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description="用户原话",
        examples=["午饭 25，和同事 AA"],
    )


class TransactionDraft(BaseModel):
    """解析出的结构化记账信息，返回给用户确认或修正后提交。

    字段可能为 null（模型没抽出来），由前端让用户补填。
    对应题目要求：「识别结果允许用户确认或修正后再入库」。
    """

    draft_id: str = Field(..., description="草稿 id，确认入库时要带回来")
    source: Source = Field(..., description="来源：text=一句话，receipt=小票图片")
    amount_cents: int | None = Field(
        None, description="金额，单位「分」。识别失败必须为 null，不要瞎猜"
    )
    merchant: str | None = Field(None, description="商户名")
    occurred_at: datetime | None = Field(None, description="消费时间，ISO 8601")
    category: Category | None = Field(None, description="自动分类结果")
    confidence: float = Field(
        0.0, ge=0.0, le=1.0, description="模型对本次抽取的把握，0~1"
    )
    missing_fields: list[str] = Field(
        default_factory=list,
        description="需要用户手动补的字段名，前端据此高亮输入框",
    )
    raw_text: str | None = Field(None, description="原始文本，便于追溯")


class CreateTransactionRequest(BaseModel):
    """用户确认（可能已修正）后，真正入库的请求体。"""

    draft_id: str | None = Field(None, description="来自哪个草稿，便于记录修正行为")
    amount_cents: int = Field(..., gt=0, description="金额，单位「分」，必须大于 0")
    merchant: str = Field(..., min_length=1, max_length=100)
    occurred_at: datetime = Field(..., description="消费时间，ISO 8601")
    category: Category
    note: str | None = Field(None, max_length=200)


class Transaction(BaseModel):
    """已入库的一笔账。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    amount_cents: int
    merchant: str
    occurred_at: datetime
    category: Category
    source: Source
    note: str | None = None
    created_at: datetime


class TransactionList(BaseModel):
    """分页列表。"""

    items: list[Transaction]
    total: int = Field(..., description="符合条件的总数，用于前端分页")
