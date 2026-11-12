"""数据访问层。

⚠️ 现在用内存 list 顶替，目的是让前端和测试能立刻联调，不等数据库。
等 DB 同学交付 schema.sql 后，这里整体换成 SQL 实现，
**函数签名不要变**，上层（api 层）一行都不用改。

这就是"数据库不归我管"时该有的隔离方式：你只依赖这几个函数的名字和语义。
"""

from datetime import UTC, datetime
from uuid import uuid4

from app.schemas.transaction import (
    Category,
    Source,
    Transaction,
    TransactionDraft,
)

# 进程内存储。重启就没了，只用于开发联调。
_DRAFTS: dict[str, TransactionDraft] = {}
_TRANSACTIONS: list[Transaction] = []
_NEXT_ID = 1


def save_draft(draft: TransactionDraft) -> TransactionDraft:
    """保存解析草稿，返回带 draft_id 的副本。"""
    _DRAFTS[draft.draft_id] = draft
    return draft


def get_draft(draft_id: str) -> TransactionDraft | None:
    return _DRAFTS.get(draft_id)


def create_transaction(
    amount_cents: int,
    merchant: str,
    occurred_at: datetime,
    category: Category,
    source: Source = Source.TEXT,
    note: str | None = None,
) -> Transaction:
    """落库一笔账。

    TODO(数据库)：换成 INSERT INTO transactions (...)，金额用整数分。
    """
    global _NEXT_ID
    tx = Transaction(
        id=_NEXT_ID,
        amount_cents=amount_cents,
        merchant=merchant,
        occurred_at=occurred_at,
        category=category,
        source=source,
        note=note,
        created_at=datetime.now(UTC),
    )
    _TRANSACTIONS.append(tx)
    _NEXT_ID += 1
    return tx


def list_transactions(
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    category: Category | None = None,
    limit: int = 20,
    offset: int = 0,
) -> tuple[list[Transaction], int]:
    """按条件查询，返回 (当前页, 总数)。

    TODO(数据库)：换成带 WHERE 的 SQL，记得用参数化查询防注入。
    """
    items = _TRANSACTIONS
    if date_from is not None:
        items = [t for t in items if t.occurred_at >= date_from]
    if date_to is not None:
        items = [t for t in items if t.occurred_at <= date_to]
    if category is not None:
        items = [t for t in items if t.category == category]

    total = len(items)
    # 按时间倒序，最近的在前
    items = sorted(items, key=lambda t: t.occurred_at, reverse=True)
    return items[offset : offset + limit], total


def category_summary(
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> tuple[list[Transaction], int]:
    """统计用的原始数据：返回 (范围内的明细, 总支出分)。

    TODO(数据库)：这里应该是一条 GROUP BY category 的聚合 SQL，
    现在为了让你能看懂逻辑，先取明细在 Python 里算。
    """
    items = _TRANSACTIONS
    if date_from is not None:
        items = [t for t in items if t.occurred_at >= date_from]
    if date_to is not None:
        items = [t for t in items if t.occurred_at <= date_to]
    return list(items), sum(t.amount_cents for t in items)


def new_draft_id() -> str:
    return uuid4().hex
