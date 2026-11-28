"""数据操作：全部原生 SQL，以及数据库行到结构体的映射。"""

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import uuid4

from pymysql.connections import Connection

from app.models import BudgetRow, Category, CategoryTotal, ChatSession, Source, TransactionRow
from app.utils import _from_db_datetime, _to_amount, _to_cents, _to_db_datetime

if TYPE_CHECKING:
    from app.schemas import TransactionDraft

def _row_to_transaction(row: dict) -> TransactionRow:
    """把 transactions 的一行转成接口响应模型。"""
    return TransactionRow(
        id=row["id"],
        amount_cents=_to_cents(row["amount"]) or 0,
        merchant=row["merchant"],
        occurred_at=_from_db_datetime(row["occurred_at"]),
        category=Category(row["category"]),
        source=Source(row["source"]),
        note=row["note"],
        created_at=_from_db_datetime(row["created_at"]),
    )


def new_draft_id() -> str:
    """生成 32 位 uuid4 hex，作为 transaction_drafts.draft_id 和接口 draft_id。"""
    return uuid4().hex


def save_draft(conn: Connection, draft: "TransactionDraft") -> "TransactionDraft":
    """把 TransactionDraft 写入 transaction_drafts 并提交。

    参数:
        conn: 请求级连接。
        draft: api 层构造的草稿，字段与 transaction_drafts 列一一对应。

    返回:
        传入的 draft 本身；调用方直接作为响应返回。
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO transaction_drafts "
            "(draft_id, source, amount, merchant, occurred_at, category, "
            " confidence, missing_fields, raw_text, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                draft.draft_id,
                draft.source.value,
                _to_amount(draft.amount_cents) if draft.amount_cents is not None else None,
                draft.merchant,
                _to_db_datetime(draft.occurred_at),
                draft.category.value if draft.category else None,
                draft.confidence,
                json.dumps(draft.missing_fields, ensure_ascii=False),
                draft.raw_text,
                _to_db_datetime(datetime.now(UTC)),
            ),
        )
    conn.commit()
    return draft


def create_transaction(
    conn: Connection,
    *,
    amount_cents: int,
    merchant: str,
    occurred_at: datetime,
    category: Category,
    source: Source | None = None,
    note: str | None = None,
    draft_id: str | None = None,
) -> TransactionRow:
    """保存一笔账，并处理 draft_id 对应的草稿。

    参数:
        conn: 请求级连接。
        amount_cents / merchant / occurred_at / category: 已由 CreateTransactionRequest 校验。
        source: 请求显式指定的来源；为 None 时尝试从草稿读取，最后回退 manual。
        note: 备注，可为 None。
        draft_id: 草稿 id；对应行存在时读取 source 并删除。

    返回:
        Transaction：入库后的账目，包含自增 id 和 created_at。

    事务:
        查草稿、删草稿、插账目在同一次 commit 里完成，中途失败一起回滚。
    """
    with conn.cursor() as cur:
        if draft_id:
            # 只有请求带了 draft_id 才查草稿；查不到时按普通账目处理，不报错。
            cur.execute(
                "SELECT source FROM transaction_drafts WHERE draft_id = %s",
                (draft_id,),
            )
            draft = cur.fetchone()
            if draft is not None:
                # 请求显式传了 source 就优先用它，否则继承草稿的 source。
                source = source or Source(draft["source"])
                cur.execute("DELETE FROM transaction_drafts WHERE draft_id = %s", (draft_id,))

        cur.execute(
            "INSERT INTO transactions "
            "(amount, merchant, category, occurred_at, created_at, source, note, status) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                _to_amount(amount_cents),
                merchant,
                category.value,
                _to_db_datetime(occurred_at),
                _to_db_datetime(datetime.now(UTC)),
                # 没有草稿且请求没传 source 时，视为手工录入。
                (source or Source.MANUAL).value,
                note,
                "confirmed",
            ),
        )
        transaction_id = cur.lastrowid
        cur.execute(
            "SELECT id, amount, merchant, category, occurred_at, created_at, source, note "
            "FROM transactions WHERE id = %s",
            (transaction_id,),
        )
        row = cur.fetchone()
    conn.commit()
    return _row_to_transaction(row)


def delete_transaction(conn: Connection, transaction_id: int) -> bool:
    """按 id 删除一笔账目。

    返回:
        True 表示删除成功；False 表示 id 不存在。
    """
    with conn.cursor() as cur:
        affected = cur.execute("DELETE FROM transactions WHERE id = %s", (transaction_id,))
    conn.commit()
    return affected > 0


def list_transactions(
    conn: Connection,
    *,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    category: Category | None = None,
    limit: int = 20,
    offset: int = 0,
) -> tuple[list[TransactionRow], int]:
    """按时间和类别筛选 transactions，返回当前页与总数。

    参数:
        date_from / date_to: 可选的时间边界，已转 UTC；闭区间。
        category: 可选类别。
        limit / offset: 分页参数，由 TransactionQuery 限制 limit 1~100、offset >= 0。

    返回:
        (当前页 Transaction 列表, 符合筛选条件的总条数)。

    SQL:
        先 COUNT(*) 得到 total，再执行带排序和分页的 SELECT；
        排序为 occurred_at DESC, id DESC，保证同一时间的账目分页稳定。
    """
    # conditions 收集非空条件，COUNT 和 SELECT 复用同一组。
    conditions: list[str] = []
    params: list[object] = []
    if date_from is not None:
        conditions.append("occurred_at >= %s")
        params.append(_to_db_datetime(date_from))
    if date_to is not None:
        conditions.append("occurred_at <= %s")
        params.append(_to_db_datetime(date_to))
    if category is not None:
        conditions.append("category = %s")
        params.append(category.value)
    where = f" WHERE {' AND '.join(conditions)}" if conditions else ""

    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) AS total FROM transactions{where}", params)
        total = int(cur.fetchone()["total"])
        cur.execute(
            "SELECT id, amount, merchant, category, occurred_at, created_at, source, note "
            f"FROM transactions{where} "
            "ORDER BY occurred_at DESC, id DESC LIMIT %s OFFSET %s",
            (*params, limit, offset),
        )
        rows = cur.fetchall()
    return [_row_to_transaction(row) for row in rows], total


def category_totals(
    conn: Connection,
    *,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> tuple[list[CategoryTotal], int]:
    """按 category 聚合 transactions，返回各类汇总和总支出。

    参数:
        date_from / date_to: 可选的时间边界，已转 UTC。

    返回:
        (CategoryTotal 列表, 总支出分)。列表按 total_cents 降序；
        没有数据时返回 ([], 0)。

    SQL:
        SELECT category, SUM(amount), COUNT(*)
        ... WHERE ... GROUP BY category
    """
    conditions: list[str] = []
    params: list[object] = []
    if date_from is not None:
        conditions.append("occurred_at >= %s")
        params.append(_to_db_datetime(date_from))
    if date_to is not None:
        conditions.append("occurred_at <= %s")
        params.append(_to_db_datetime(date_to))
    where = f" WHERE {' AND '.join(conditions)}" if conditions else ""

    with conn.cursor() as cur:
        cur.execute(
            "SELECT category, SUM(amount) AS total_amount, COUNT(*) AS count "
            f"FROM transactions{where} GROUP BY category",
            params,
        )
        rows = cur.fetchall()

    # 数据库返回 Decimal 金额和计数，这里统一转成「分」并按金额降序。
    items = [
        CategoryTotal(
            category=Category(row["category"]),
            total_cents=_to_cents(row["total_amount"]) or 0,
            count=int(row["count"]),
        )
        for row in rows
    ]
    items.sort(key=lambda item: item.total_cents, reverse=True)
    return items, sum(item.total_cents for item in items)


def _row_to_budget(row: dict) -> BudgetRow:
    """把 budgets 的一行转成 BudgetRow。"""
    return BudgetRow(
        id=row["id"],
        month=row["period"],
        category=row["category"] or "",
        amount_cents=_to_cents(row["amount"]) or 0,
        # threshold 可能为 NULL，缺省按 0.8 处理。
        threshold=float(row["threshold"]) if row["threshold"] is not None else 0.8,
    )


def list_budgets(conn: Connection, month: str) -> list[BudgetRow]:
    """读取某个月的全部预算行。

    参数:
        month: 月份，格式 YYYY-MM。

    返回:
        BudgetRow 列表；排序（总预算在前）由 api/budgets.py 负责。
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, period, category, amount, threshold FROM budgets WHERE period = %s",
            (month,),
        )
        rows = cur.fetchall()
    return [_row_to_budget(row) for row in rows]


def upsert_budget(
    conn: Connection,
    *,
    month: str,
    category: str,
    amount_cents: int,
) -> BudgetRow:
    """按 period + category 写入或更新一条预算。

    参数:
        month: 月份，格式 YYYY-MM，写入 period 列。
        category: 类别中文值；空字符串表示整月总预算。
        amount_cents: 预算金额，单位「分」，已由请求模型确保大于 0。

    返回:
        落库后的 BudgetRow，包含自增 id。
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM budgets WHERE period = %s AND category = %s",
            (month, category),
        )
        existing = cur.fetchone()
        if existing is None:
            cur.execute(
                "INSERT INTO budgets (period, category, amount) VALUES (%s, %s, %s)",
                (month, category, _to_amount(amount_cents)),
            )
            budget_id = cur.lastrowid
        else:
            budget_id = existing["id"]
            cur.execute(
                "UPDATE budgets SET amount = %s WHERE id = %s",
                (_to_amount(amount_cents), budget_id),
            )
        cur.execute(
            "SELECT id, period, category, amount, threshold FROM budgets WHERE id = %s",
            (budget_id,),
        )
        row = cur.fetchone()
    conn.commit()
    return _row_to_budget(row)


def delete_budget(conn: Connection, budget_id: int) -> bool:
    """删除一条预算。

    返回:
        True 表示删除成功；False 表示 id 不存在。
    """
    with conn.cursor() as cur:
        affected = cur.execute("DELETE FROM budgets WHERE id = %s", (budget_id,))
    conn.commit()
    return affected > 0


def _row_to_chat_session(row: dict) -> ChatSession:
    """把数据库行转成 ChatSession，时间补回 UTC 时区。"""
    return ChatSession(
        session_id=row["session_id"],
        conversation_id=row["dify_conversation_id"],
        created_at=_from_db_datetime(row["created_at"]),
    )


def new_session_id() -> str:
    """生成会话 id；与草稿 id 一样用 32 位十六进制，对应 char(32)。"""
    return uuid4().hex


def get_chat_session(conn: Connection, session_id: str) -> ChatSession | None:
    """按 id 读会话；不存在返回 None。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT session_id, dify_conversation_id, created_at "
            "FROM chat_sessions WHERE session_id = %s",
            (session_id,),
        )
        row = cur.fetchone()
    return _row_to_chat_session(row) if row else None


def create_chat_session(conn: Connection, session_id: str | None = None) -> ChatSession:
    """新建一条会话记录并提交。

    参数:
        session_id: 调用方指定的 id；为空时自动生成。
    """
    session_id = session_id or new_session_id()
    now = datetime.now(UTC)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO chat_sessions (session_id, created_at, updated_at) "
            "VALUES (%s, %s, %s)",
            (session_id, _to_db_datetime(now), _to_db_datetime(now)),
        )
    conn.commit()
    return ChatSession(session_id=session_id, conversation_id=None, created_at=now)


def get_or_create_chat_session(conn: Connection, session_id: str | None) -> ChatSession:
    """按 id 取会话，取不到就新建；前端不传 id 时也会得到一条新会话。"""
    if session_id:
        existing = get_chat_session(conn, session_id)
        if existing:
            return existing
    return create_chat_session(conn, session_id)


def set_chat_conversation_id(conn: Connection, session_id: str, conversation_id: str) -> None:
    """保存 Dify 返回的 conversation_id，并刷新 updated_at。"""
    now = _to_db_datetime(datetime.now(UTC))
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE chat_sessions SET dify_conversation_id = %s, updated_at = %s "
            "WHERE session_id = %s",
            (conversation_id, now, session_id),
        )
    conn.commit()


def add_chat_message(conn: Connection, session_id: str, role: str, content: str) -> None:
    """追加一条消息并刷新会话时间；role 由调用方保证是 user / assistant。"""
    now = _to_db_datetime(datetime.now(UTC))
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO chat_messages (session_id, role, content, created_at) "
            "VALUES (%s, %s, %s, %s)",
            (session_id, role, content, now),
        )
        cur.execute(
            "UPDATE chat_sessions SET updated_at = %s WHERE session_id = %s",
            (now, session_id),
        )
    conn.commit()


def list_chat_messages(conn: Connection, session_id: str, limit: int = 200) -> list[dict]:
    """按时间正序返回最近 limit 条消息，供前端渲染会话历史。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT role, content, created_at FROM chat_messages "
            "WHERE session_id = %s ORDER BY id DESC LIMIT %s",
            (session_id, limit),
        )
        rows = cur.fetchall()
    return [
        {
            "role": row["role"],
            "content": row["content"],
            "created_at": _from_db_datetime(row["created_at"]),
        }
        for row in reversed(rows)
    ]
