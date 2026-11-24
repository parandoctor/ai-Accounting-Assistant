"""存储层：领域枚举、MySQL 连接、建表与全部 SQL。

- Category / Source：数据库里存的中文值与来源值，接口、草稿、统计共用；
- ensure_utc：时间统一按 UTC，schema 校验与模型输出清洗都调用它；
- create_conn / get_conn / ConnDep：按请求创建并注入连接，异常时回滚；
- init_db：启动时建库建表，并给老库补列补索引；
- 其余函数是账目、草稿、预算、会话的原生 SQL 读写，写操作在函数内 commit。

金额约定：数据库存「元」（DECIMAL），接口用「分」（int），换算只在本层做。
时间约定：数据库存 naive UTC，读出来统一补回 UTC 时区。
"""

import json
import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Annotated
from uuid import uuid4

import pymysql
from dotenv import load_dotenv
from fastapi import Depends
from pymysql.connections import Connection
from pymysql.cursors import DictCursor

if TYPE_CHECKING:
    from app.api import TransactionDraft


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


def ensure_utc(value: datetime) -> datetime:
    """校验 datetime 带时区并转成 UTC；无时区时抛 ValueError（框架转 422）。"""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("时间必须带时区，例如 2026-11-01T00:00:00+08:00")
    return value.astimezone(UTC)


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


# 读 backend/.env：路径按文件位置推算，不受启动目录影响。
_DOTENV_PATH = Path(__file__).resolve().parents[1] / ".env"
load_dotenv(_DOTENV_PATH)

# 环境变量缺省值与本机开发环境一致；生产部署用环境变量覆盖。
DB_NAME = os.getenv("DB_NAME", "ledger")
_DB_CONFIG = {
    "host": os.getenv("DB_HOST", "127.0.0.1"),
    "port": int(os.getenv("DB_PORT", "3306")),
    "user": os.getenv("DB_USER", "root"),
    "password": os.getenv("DB_PASSWORD", ""),
    "charset": "utf8mb4",
}

# 建表脚本：backend/schema.sql 是唯一的表结构来源，应用启动时读取并执行。
# 脚本里的 create database / use 由代码跳过，库名统一走 DB_NAME。
_SCHEMA_PATH = Path(__file__).resolve().parents[1] / "schema.sql"


def _schema_statements() -> list[str]:
    """把 schema.sql 拆成单条可执行语句。

    只保留建表语句：create database 与 use 会按环境变量另做处理，
    否则脚本里写死的 ledger 会覆盖 DB_NAME。
    """
    raw = _SCHEMA_PATH.read_text(encoding="utf-8")
    # 去掉整行注释，避免注释里的分号干扰拆分。
    body = "\n".join(
        line for line in raw.splitlines() if not line.strip().startswith("--")
    )
    statements: list[str] = []
    for chunk in body.split(";"):
        statement = chunk.strip()
        if not statement:
            continue
        lowered = statement.lower()
        if lowered.startswith("create database") or lowered.startswith("use "):
            continue
        statements.append(statement)
    return statements


def create_conn(*, with_database: bool = True) -> Connection:
    """新建一条 MySQL 连接。

    参数:
        with_database: 为 False 时不带库名，用于建库前的连接。

    返回:
        pymysql Connection；行数据以 dict 返回（DictCursor），autocommit 关闭。

    异常:
        连不上时抛 RuntimeError，并把 host / port / 用户带在消息里，方便定位配置问题。
    """
    config = dict(_DB_CONFIG)
    if with_database:
        config["database"] = DB_NAME
    try:
        return pymysql.connect(cursorclass=DictCursor, autocommit=False, **config)
    except pymysql.MySQLError as exc:
        raise RuntimeError(
            f"MySQL 连接失败：{config['host']}:{config['port']}，"
            f"用户 {config['user']}，库 {DB_NAME}；原始错误：{exc}"
        ) from exc


def get_conn() -> Iterator[Connection]:
    """为单个请求创建连接，请求结束后关闭。

    路由函数处理中抛异常时先回滚，避免半截写入留在事务里；
    正常结束时由各 store 函数自己 commit。
    """
    conn = create_conn()
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# 路由参数写 conn: ConnDep，FastAPI 会调用 get_conn() 注入请求级连接。
ConnDep = Annotated[Connection, Depends(get_conn)]


def _column_exists(cur: DictCursor, table: str, column: str) -> bool:
    """查询 information_schema，判断表里是否已有该列。"""
    cur.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema = DATABASE() AND table_name = %s AND column_name = %s",
        (table, column),
    )
    return cur.fetchone() is not None


def _index_exists(cur: DictCursor, table: str, index: str) -> bool:
    """查询 information_schema，判断表里是否已有该索引。"""
    cur.execute(
        "SELECT 1 FROM information_schema.statistics "
        "WHERE table_schema = DATABASE() AND table_name = %s AND index_name = %s",
        (table, index),
    )
    return cur.fetchone() is not None


def _upgrade_schema(cur: DictCursor) -> None:
    """给按 schema.sql 建好的老库补齐新增的列与索引。

    只做加法：已有列和数据都不动；merchant 等字段长度以 schema.sql 为准。
    """
    if not _column_exists(cur, "transactions", "note"):
        cur.execute("ALTER TABLE transactions ADD COLUMN note VARCHAR(200) NULL AFTER status")
    if not _index_exists(cur, "transactions", "ix_transactions_occurred"):
        cur.execute("ALTER TABLE transactions ADD INDEX ix_transactions_occurred (occurred_at)")
    if not _index_exists(cur, "transactions", "ix_transactions_category_occurred"):
        cur.execute(
            "ALTER TABLE transactions "
            "ADD INDEX ix_transactions_category_occurred (category, occurred_at)"
        )
    if not _index_exists(cur, "budgets", "uq_budgets_period_category"):
        cur.execute(
            "ALTER TABLE budgets "
            "ADD UNIQUE KEY uq_budgets_period_category (period, category)"
        )


def init_db() -> None:
    """应用启动时确保库和表存在。

    先不带库名连接执行 CREATE DATABASE，再连库执行建表与补列；
    已存在的表和列不会重建，字段类型变更需要单独的迁移脚本。
    """
    bootstrap = create_conn(with_database=False)
    try:
        with bootstrap.cursor() as cur:
            cur.execute(f"CREATE DATABASE IF NOT EXISTS `{DB_NAME}` DEFAULT CHARSET utf8mb4")
        bootstrap.commit()
    finally:
        bootstrap.close()

    conn = create_conn()
    try:
        with conn.cursor() as cur:
            for statement in _schema_statements():
                cur.execute(statement)
            _upgrade_schema(cur)
        conn.commit()
    finally:
        conn.close()


# 数据库金额是两位小数，换算后用这个模板把多余的精度截掉。
_CENTS = Decimal("0.01")


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


def _to_amount(cents: int) -> Decimal:
    """金额从「分」转成入库的「元」，保留两位小数。"""
    return (Decimal(cents) / 100).quantize(_CENTS)


def _to_cents(amount: Decimal | None) -> int | None:
    """金额从库里的「元」转回接口用的「分」；空值原样返回 None。"""
    if amount is None:
        return None
    return int((Decimal(amount) * 100).to_integral_value(rounding=ROUND_HALF_UP))


def _to_db_datetime(value: datetime | None) -> datetime | None:
    """带时区的时间转成入库用的 naive UTC。

    MySQL 的 DATETIME 不存时区，统一按 UTC 落库，读出来再补 UTC，
    这样接口出入参都是 UTC，不受服务器本地时区影响。
    """
    if value is None:
        return None
    return value.astimezone(UTC).replace(tzinfo=None)


def _from_db_datetime(value: datetime | None) -> datetime | None:
    """库里的 naive UTC 补上 UTC 时区，供 Pydantic 响应模型序列化。"""
    if value is None:
        return None
    return value.replace(tzinfo=UTC)


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


@dataclass(frozen=True, slots=True)
class ChatSession:
    """chat_sessions 表的一行。

    conversation_id 是 Dify 侧的会话标识，为空表示还没产生过对话。
    """

    session_id: str
    conversation_id: str | None
    created_at: datetime


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
