"""领域模型：类别与来源枚举，以及数据库行的只读结构。"""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

class Category(str, Enum):
    """消费类别枚举，API、数据库和 /categories 接口共用这组中文值。"""

    FOOD = "餐饮"
    TRANSPORT = "交通"
    SHOPPING = "购物"
    ENTERTAINMENT = "娱乐"
    MEDICAL = "医疗"
    HOUSING = "居住"
    OTHER = "其他"


class Source(str, Enum):
    """记账来源：text=一句话草稿，receipt=小票草稿，manual=手工录入。"""

    TEXT = "text"
    RECEIPT = "receipt"
    MANUAL = "manual"


@dataclass(frozen=True, slots=True)
class TransactionRow:
    """transactions 表的一行，供 api 层构造 Transaction 响应模型。"""

    id: int
    amount_cents: int
    merchant: str
    occurred_at: datetime
    category: Category
    source: Source
    note: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class CategoryTotal:
    """store.category_totals() 的聚合结果行。

    api/stats.py 用它组装 CategoryStat，并计算 ratio。
    """

    category: Category
    total_cents: int
    count: int


@dataclass(frozen=True, slots=True)
class BudgetRow:
    """budgets 表的一行查询结果，供 api/budgets.py 组装响应。

    month 来自 period 列；category 为空字符串表示整月总预算。
    """

    id: int
    month: str
    category: str
    amount_cents: int
    threshold: float


@dataclass(frozen=True, slots=True)
class ChatSession:
    """chat_sessions 表的一行。

    conversation_id 是 Dify 侧的会话标识，为空表示还没产生过对话。
    """

    session_id: str
    conversation_id: str | None
    created_at: datetime
