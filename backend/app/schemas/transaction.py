"""记账相关的请求与响应模型。

字段命名统一为 snake_case；金额使用以「分」为单位的整数 amount_cents；
时间使用带时区的 ISO 8601 字符串，数据库存储 UTC。
"""

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class Category(str, Enum):
    """消费类别枚举。前端下拉框、数据库取值与模型提示词共用同一套取值。"""

    FOOD = "餐饮"
    TRANSPORT = "交通"
    SHOPPING = "购物"
    ENTERTAINMENT = "娱乐"
    MEDICAL = "医疗"
    HOUSING = "居住"
    OTHER = "其他"


class Source(str, Enum):
    """记账来源。"""

    TEXT = "text"
    RECEIPT = "receipt"


class ParseTextRequest(BaseModel):
    """一句话记账的请求体。"""

    text: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description="用户输入的自然语言文本",
        examples=["午饭 25，和同事 AA"],
    )


class TransactionDraft(BaseModel):
    """解析结果，供调用方确认或修正后提交。

    各字段允许为 null，表示该项未被识别。
    """

    draft_id: str = Field(..., description="草稿 id，提交时回传")
    source: Source = Field(..., description="来源：text=一句话，receipt=小票图片")
    amount_cents: int | None = Field(None, description="金额，单位「分」；未识别时为 null")
    merchant: str | None = Field(None, description="商户名；未识别时为 null")
    occurred_at: datetime | None = Field(None, description="消费时间，ISO 8601；未识别时为 null")
    category: Category | None = Field(None, description="分类结果；未识别时为 null")
    confidence: float = Field(0.0, ge=0.0, le=1.0, description="本次抽取的置信度，0~1")
    missing_fields: list[str] = Field(
        default_factory=list,
        description="未识别出的字段名列表",
    )
    raw_text: str | None = Field(None, description="原始文本")


class CreateTransactionRequest(BaseModel):
    """保存一笔账的请求体。"""

    draft_id: str | None = Field(None, description="来源草稿 id")
    amount_cents: int = Field(..., gt=0, description="金额，单位「分」，必须大于 0")
    merchant: str = Field(..., min_length=1, max_length=100, description="商户名")
    occurred_at: datetime = Field(..., description="消费时间，ISO 8601")
    category: Category = Field(..., description="消费类别")
    note: str | None = Field(None, max_length=200, description="备注")


class Transaction(BaseModel):
    """已保存的一笔账。"""

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
    """分页查询结果。"""

    items: list[Transaction]
    total: int = Field(..., description="符合条件的总数")
