"""数据访问层，封装记账数据的读写。

当前使用进程内存储，进程重启后数据丢失。
"""

from datetime import UTC, datetime
from uuid import uuid4

from app.schemas.transaction import (
    Category,
    Source,
    Transaction,
    TransactionDraft,
)

_DRAFTS: dict[str, TransactionDraft] = {}
_TRANSACTIONS: list[Transaction] = []
_NEXT_ID = 1


def save_draft(draft: TransactionDraft) -> TransactionDraft:
    """保存解析草稿。"""
    _DRAFTS[draft.draft_id] = draft
    return draft


def get_draft(draft_id: str) -> TransactionDraft | None:
    """按 id 读取解析草稿。"""
    return _DRAFTS.get(draft_id)


def delete_draft(draft_id: str) -> None:
    """删除解析草稿，id 不存在时不报错。"""
    _DRAFTS.pop(draft_id, None)


def create_transaction(
    amount_cents: int,
    merchant: str,
    occurred_at: datetime,
    category: Category,
    source: Source = Source.TEXT,
    note: str | None = None,
) -> Transaction:
    """写入一笔账。"""
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
    """按条件查询，返回 (当前页, 总数)。"""
    items = _TRANSACTIONS
    if date_from is not None:
        items = [t for t in items if t.occurred_at >= date_from]
    if date_to is not None:
        items = [t for t in items if t.occurred_at <= date_to]
    if category is not None:
        items = [t for t in items if t.category == category]

    total = len(items)
    # 按消费时间倒序。
    items = sorted(items, key=lambda t: t.occurred_at, reverse=True)
    return items[offset : offset + limit], total


def category_summary(
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> tuple[list[Transaction], int]:
    """返回时间范围内的明细与总支出，金额单位为分。"""
    items = _TRANSACTIONS
    if date_from is not None:
        items = [t for t in items if t.occurred_at >= date_from]
    if date_to is not None:
        items = [t for t in items if t.occurred_at <= date_to]
    return list(items), sum(t.amount_cents for t in items)


def new_draft_id() -> str:
    """生成草稿 id。"""
    return uuid4().hex
