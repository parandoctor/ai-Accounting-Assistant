"""数据库连接与建表：连接、请求级依赖、启动建库与补列。"""

from collections.abc import Iterator

import pymysql
from pymysql.connections import Connection
from pymysql.cursors import DictCursor

from app.core.config import DB_NAME, _DB_CONFIG, _SCHEMA_PATH

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
