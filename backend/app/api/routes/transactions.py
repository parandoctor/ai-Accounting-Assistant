"""记账接口：一句话解析、保存账目、列表与删除。"""

from typing import Annotated

from fastapi import APIRouter, Query, status

from app import operations
from app.ai import tasks
from app.api.dependencies import ConnDep
from app.core.errors import VALIDATION_ERROR_RESPONSES, ApiError, ErrorResponse
from app.models import Source
from app.schemas import (
    CreateTransactionRequest,
    ParseTextRequest,
    Transaction,
    TransactionDraft,
    TransactionList,
    TransactionQuery,
)
from app.utils import MAX_AMOUNT_CENTS

transactions_router = APIRouter(
    prefix="/transactions",
    tags=["记账"],
    # 三个路由的 422 都使用统一 ErrorResponse，而不是 FastAPI 默认结构。
    responses=VALIDATION_ERROR_RESPONSES,
)

# POST /transactions/parse 的 OpenAPI 200 示例。
# 只用于文档展示；实际返回由 TransactionDraft 序列化，未识别字段会返回 null。
_DRAFT_EXAMPLE = {
    "draft_id": "6f1c9b2e8a4d4c1f9b7e5a3d2c1b0a9e",
    "source": "text",
    "amount_cents": 2500,
    "merchant": "食堂",
    "occurred_at": "2026-11-11T04:30:00Z",
    "category": "餐饮",
    "confidence": 0.92,
    "missing_fields": [],
    "raw_text": "午饭 25，和同事 AA",
    "engine": "llm",
}


@transactions_router.post(
    "/parse",
    response_model=TransactionDraft,
    summary="一句话记账：解析消费信息",
    description=(
        "将自然语言转换为结构化记账信息。\n\n"
        "engine 为 llm 表示大模型解析，rule 表示模型不可用时的规则兜底。"
        "amount_cents 可能为 null，表示未解析出金额，补齐后调用 POST /transactions 保存。"
    ),
    responses={
        200: {"content": {"application/json": {"example": _DRAFT_EXAMPLE}}},
        400: {"model": ErrorResponse, "description": "识别出的金额超出可记录范围"},
    },
)
def parse_text(payload: ParseTextRequest, conn: ConnDep) -> TransactionDraft:
    """将一句话解析成待确认的草稿，并写入 transaction_drafts。

    参数:
        payload: 请求体。text 已由 ParseTextRequest 去掉首尾空格，长度 1~200。
        conn: 请求级连接，交给 operations.save_draft() 写入草稿。

    返回:
        TransactionDraft：source 固定为 text；engine 标明解析引擎；
        missing_fields 列出所有值为 None 的字段。

    调用关系:
        tasks.extract() 优先调大模型、失败退回规则 ->
        operations.new_draft_id() 生成 draft_id -> operations.save_draft() 写入草稿。
    """
    # 上层 schema 已完成去空格和长度校验，这里直接使用清理后的文本。
    text = payload.text
    extraction = tasks.extract(text)

    # 抽取出的金额同样写进 transaction_drafts.amount，超过 decimal(10,2) 会在写库时
    # 抛 Out of range 变成 500；这里提前拦下，给可读的 400。
    if extraction.amount_cents is not None and extraction.amount_cents > MAX_AMOUNT_CENTS:
        raise ApiError(
            code="AMOUNT_TOO_LARGE",
            message="金额超出可记录范围（上限 99999999.99 元），请核对后重试",
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"max_cents": MAX_AMOUNT_CENTS},
        )

    draft = TransactionDraft(
        # draft_id 与 transaction_drafts.draft_id 对应，保存账目时回传这个值。
        draft_id=operations.new_draft_id(),
        source=Source.TEXT,
        amount_cents=extraction.amount_cents,
        merchant=extraction.merchant,
        occurred_at=extraction.occurred_at,
        category=extraction.category,
        confidence=extraction.confidence,
        # 只收集值为 None 的字段名，顺序与 TransactionDraft 的声明顺序一致；
        # 前端按这个数组高亮待补字段。
        missing_fields=[
            name
            for name, value in (
                ("amount_cents", extraction.amount_cents),
                ("merchant", extraction.merchant),
                ("occurred_at", extraction.occurred_at),
                ("category", extraction.category),
            )
            if value is None
        ],
        raw_text=text,
        engine=extraction.engine,
    )
    # save_draft() 写入 transaction_drafts 并 commit；返回同一 draft 作为响应。
    return operations.save_draft(conn, draft)


@transactions_router.post(
    "",
    response_model=Transaction,
    status_code=status.HTTP_201_CREATED,
    summary="保存一笔账",
    description=(
        "保存一笔已确认的账目。\n\n"
        "传入 draft_id 时按草稿推断来源并删除草稿；不传时视为手工记账。"
    ),
    responses={
        201: {
            "content": {
                "application/json": {
                    "example": {
                        "id": 1,
                        "amount_cents": 2500,
                        "merchant": "食堂",
                        "occurred_at": "2026-11-11T04:30:00Z",
                        "category": "餐饮",
                        "source": "text",
                        "note": "和同事 AA",
                        "created_at": "2026-11-11T04:31:05Z",
                    }
                }
            }
        }
    },
)
def create_transaction(payload: CreateTransactionRequest, conn: ConnDep) -> Transaction:
    """把已确认的账目写入 transactions。

    参数:
        payload: CreateTransactionRequest；金额、商户、时间、类别已通过 schema 校验，
                 occurred_at 已转 UTC，note 空字符串已转 None。
        conn: 请求级连接，交给 operations.create_transaction()。

    返回:
        Transaction：入库后的账目，包含数据库生成的 id 和 created_at。

    调用关系:
        operations.create_transaction() 会按 payload.draft_id 查草稿：
        - 找到草稿：优先使用请求的 source，否则用草稿的 source，并删除草稿；
        - 未找到或未传：source 为空时记为 manual。
        草稿删除与账目插入在同一次 commit 中完成。
    """
    # 字段原样交给 store；来源推断、草稿消费和事务提交都在 store 内完成。
    return operations.create_transaction(
        conn,
        amount_cents=payload.amount_cents,
        merchant=payload.merchant,
        occurred_at=payload.occurred_at,
        category=payload.category,
        source=payload.source,
        note=payload.note,
        draft_id=payload.draft_id,
    )


@transactions_router.delete(
    "/{transaction_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除一笔账",
    description="按 id 删除账目，成功返回 204 且没有响应体；id 不存在时返回 404。",
    responses={404: {"model": ErrorResponse, "description": "账目不存在"}},
)
def delete_transaction(transaction_id: int, conn: ConnDep) -> None:
    """删除 transactions 中的一条记录。

    参数:
        transaction_id: 路径参数，对应 transactions.id，由 FastAPI 转成 int。
        conn: 请求级连接，交给 operations.delete_transaction()。

    返回:
        成功时无响应体（204）；id 不存在时抛 ApiError，由统一异常处理器转成
        {"code": "NOT_FOUND", "message": "账目不存在", "detail": {}}。
    """
    if not operations.delete_transaction(conn, transaction_id):
        raise ApiError(
            code="NOT_FOUND",
            message="账目不存在",
            status_code=status.HTTP_404_NOT_FOUND,
        )


@transactions_router.get(
    "",
    response_model=TransactionList,
    summary="查询账目列表",
    description="支持时间范围、类别筛选与分页。",
)
def list_transactions(
    params: Annotated[TransactionQuery, Query()],
    conn: ConnDep,
) -> TransactionList:
    """从 transactions 读取分页账目。

    参数:
        params: TransactionQuery；FastAPI 从 query string 解析
                from/to/category/limit/offset。DateRangeQuery 已校验时区并转 UTC，
                且保证 from 不晚于 to。
        conn: 请求级连接，交给 operations.list_transactions()。

    返回:
        TransactionList：items 是当前页账目，total 是符合筛选条件的总条数。

    调用关系:
        operations.list_transactions() 先执行 COUNT 得到 total，再执行
        SELECT + ORDER BY occurred_at DESC, id DESC + LIMIT/OFFSET。
    """
    items, total = operations.list_transactions(
        conn,
        date_from=params.date_from,
        date_to=params.date_to,
        category=params.category,
        limit=params.limit,
        offset=params.offset,
    )
    return TransactionList(items=items, total=total)
