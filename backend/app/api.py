"""HTTP 层：请求/响应模型与全部路由。

- Pydantic 模型是 HTTP 契约：字段类型、长度、时区与枚举校验都在这里；
- 六个路由分别对应记账、查账、小票识别、统计、预算与智能助手；
- 错误响应统一成 {code, message, detail}，实现见 app.error。

金额单位「分」，时间入参必须带时区、响应统一 UTC；SQL 一律在 app.db 里。
"""

import re
from datetime import UTC, datetime, timedelta, timezone
from typing import Annotated, Self

from fastapi import (
    APIRouter,
    File,
    Path as PathParam,
    Query,
    UploadFile,
    status,
)
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app import ai_chat, ai_client, ai_tasks, db
from app.db import Category, ConnDep, Source, ensure_utc
from app.error import VALIDATION_ERROR_RESPONSES, ApiError, ErrorResponse

class DateRangeQuery(BaseModel):
    """from / to 时间范围参数。

    字段名 date_from / date_to 在 SQL 中分别对应 >= / <=，
    对外 alias 保持 from / to，与接口文档一致。
    两个参数都可选；校验后可直接和数据库中的 UTC 时间比较。
    """

    model_config = ConfigDict(populate_by_name=True)

    date_from: datetime | None = Field(None, alias="from", description="起始时间（含）")
    date_to: datetime | None = Field(None, alias="to", description="结束时间（含）")

    @field_validator("date_from", "date_to")
    @classmethod
    def _normalize(cls, value: datetime | None) -> datetime | None:
        """把每个非空时间转成 UTC；None 原样返回。"""
        return None if value is None else ensure_utc(value)

    @model_validator(mode="after")
    def _check_order(self) -> Self:
        """校验起始时间不晚于结束时间。

        from > to 时直接报 422，避免返回空列表让调用方误以为没有数据。
        """
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("from 不能晚于 to")
        return self


class ParseTextRequest(BaseModel):
    """POST /transactions/parse 的请求体。

    text 由路由函数 parse_text() 读取；长度 1~200，去首尾空格后不能为空。
    """

    text: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description="用户输入的自然语言文本",
        examples=["午饭 25，和同事 AA"],
    )

    @field_validator("text")
    @classmethod
    def _strip_text(cls, value: str) -> str:
        """去掉首尾空格，并拒绝纯空格输入。

        min_length 只能拦住空字符串，拦不住 "   "；
        这里再 strip 一次，保证 parse_text() 拿到的是有效文本。
        """
        value = value.strip()
        if not value:
            raise ValueError("text 不能为空")
        return value


class TransactionDraft(BaseModel):
    """解析接口返回的草稿，也是 store.save_draft() 的入参。

    各字段允许为 null，表示该项未识别；missing_fields 列出所有 null 字段名，
    前端据此把对应输入框标记为待补项。
    """

    # 允许从字典构造；当前路由直接构造草稿。
    model_config = ConfigDict(from_attributes=True)

    draft_id: str = Field(..., description="草稿 id，提交时回传")
    source: Source = Field(..., description="来源：text=一句话，receipt=小票图片")
    amount_cents: int | None = Field(None, description="金额，单位「分」；未识别时为 null")
    merchant: str | None = Field(None, description="商户名；未识别时为 null")
    occurred_at: datetime | None = Field(None, description="消费时间，UTC；未识别时为 null")
    category: Category | None = Field(None, description="分类结果；未识别时为 null")
    confidence: float = Field(0.0, ge=0.0, le=1.0, description="本次抽取的置信度，0~1")
    missing_fields: list[str] = Field(
        default_factory=list,
        description="未识别出的字段名列表",
    )
    raw_text: str | None = Field(None, description="原始文本")
    engine: str = Field(
        "rule",
        description="解析引擎：llm=大模型，rule=规则兜底，none=未识别（图片未配置视觉模型）",
    )

    @field_validator("occurred_at")
    @classmethod
    def _normalize_occurred_at(cls, value: datetime | None) -> datetime | None:
        """草稿时间如果存在，统一转成 UTC。

        当前规则解析不产出时间，值为 None；后续接入模型后仍走同一校验。
        """
        return None if value is None else ensure_utc(value)


class CreateTransactionRequest(BaseModel):
    """POST /transactions 的请求体。

    字段由 store.create_transaction() 使用：
    - draft_id 指向 transaction_drafts，可为空；
    - source 为空时由草稿推断，仍为空则记 manual；
    - 其余字段必填，写进 transactions。
    """

    draft_id: str | None = Field(
        None,
        max_length=32,
        description="解析草稿 id；不传表示手工记账",
    )
    source: Source | None = Field(
        None,
        description="来源；不传时按草稿推断，无草稿则记为 manual",
    )
    amount_cents: int = Field(..., gt=0, description="金额，单位「分」，必须大于 0")
    merchant: str = Field(..., min_length=1, max_length=30, description="商户名")
    occurred_at: datetime = Field(..., description="消费时间，ISO 8601 带时区")
    category: Category = Field(..., description="消费类别")
    note: str | None = Field(None, max_length=200, description="备注")

    @field_validator("merchant")
    @classmethod
    def _strip_merchant(cls, value: str) -> str:
        """去掉商户名首尾空格，并拒绝纯空格输入。

        与数据库 merchant NOT NULL 对应，避免把 "   " 当作有效商户入库。
        """
        value = value.strip()
        if not value:
            raise ValueError("merchant 不能为空")
        return value

    @field_validator("occurred_at")
    @classmethod
    def _normalize_occurred_at(cls, value: datetime) -> datetime:
        """消费时间必须带时区，并转换为 UTC 后写入 transactions.occurred_at。"""
        return ensure_utc(value)

    @field_validator("note")
    @classmethod
    def _blank_note_to_none(cls, value: str | None) -> str | None:
        """备注去首尾空格；空字符串转 None，与数据库 note 允许 NULL 对应。"""
        if value is None:
            return None
        value = value.strip()
        return value or None


class Transaction(BaseModel):
    """保存成功后的账目响应，对应 transactions 表的一行。

    store.create_transaction() 用数据库返回的一行构造；
    id 和 created_at 由数据库生成，时间字段统一为 UTC。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int = Field(..., description="账目 id")
    amount_cents: int = Field(..., description="金额，单位「分」")
    merchant: str = Field(..., description="商户名")
    occurred_at: datetime = Field(..., description="消费时间，UTC")
    category: Category = Field(..., description="消费类别")
    source: Source = Field(..., description="记账来源")
    note: str | None = Field(None, description="备注，可为空")
    created_at: datetime = Field(..., description="入库时间，UTC")


class TransactionQuery(DateRangeQuery):
    """GET /transactions 的查询参数。

    FastAPI 从 query string 解析这些字段；DateRangeQuery 负责 from/to 的时区与顺序校验，
    本类补充 category、limit、offset，最终由 store.list_transactions() 拼 SQL。
    """

    category: Category | None = Field(None, description="按类别筛选")
    limit: int = Field(20, ge=1, le=100, description="每页条数")
    offset: int = Field(0, ge=0, description="偏移量")


class TransactionList(BaseModel):
    """分页查询结果，对应 GET /transactions 的响应。

    items 是当前页账目；total 是符合筛选条件的总条数，用于计算总页数。
    """

    items: list[Transaction] = Field(..., description="当前页账目")
    total: int = Field(..., description="符合条件的总数")


class QueryRequest(BaseModel):
    """POST /query/parse 和 POST /query/ask 的请求体。"""

    question: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description="用户的口语化问题",
        examples=["这个月吃饭花了多少钱"],
    )

    @field_validator("question")
    @classmethod
    def _strip_question(cls, value: str) -> str:
        """去掉首尾空格，并拒绝纯空格输入。"""
        value = value.strip()
        if not value:
            raise ValueError("question 不能为空")
        return value


class QueryIntentOut(BaseModel):
    """POST /query/parse 的响应：只给解析出的查询条件。"""

    category: Category | None = Field(None, description="限定的类别；未限定为 null")
    date_from: datetime | None = Field(None, description="起始时间，UTC；未限定为 null")
    date_to: datetime | None = Field(None, description="结束时间，UTC；未限定为 null")
    intent: str = Field(..., description="total=合计金额，count=笔数，top=哪类最多，list=明细")
    confidence: float = Field(..., ge=0.0, le=1.0, description="解析置信度")
    engine: str = Field(..., description="llm=大模型解析，rule=规则兜底")


class QueryAnswer(BaseModel):
    """POST /query/ask 的响应：查询条件 + 查到的数据 + 一句可直接展示的结论。"""

    question: str = Field(..., description="原始问题")
    intent: str = Field(..., description="total/count/top/list")
    category: Category | None = Field(None, description="限定的类别")
    date_from: datetime | None = Field(None, description="起始时间，UTC")
    date_to: datetime | None = Field(None, description="结束时间，UTC")
    total_cents: int = Field(..., description="命中范围的合计金额，单位「分」")
    count: int = Field(..., description="命中范围的账目笔数")
    answer: str = Field(..., description="结论文案，可直接显示")
    items: list[Transaction] = Field(default_factory=list, description="明细，仅 list 意图有值")
    engine: str = Field(..., description="llm=大模型解析，rule=规则兜底")


class CategoryStat(BaseModel):
    """单个类别的聚合结果。

    category / total_cents / count 来自 store.CategoryTotal；
    ratio 由 api/stats.py 用该类金额除以总金额计算。
    """

    category: Category
    total_cents: int = Field(..., description="该类总支出，单位「分」")
    count: int = Field(..., description="该类笔数")
    ratio: float = Field(..., ge=0.0, le=1.0, description="占总支出比例，0~1")


class CategoryStatsResponse(BaseModel):
    """分类统计接口的完整响应。

    date_from / date_to 是经过 UTC 转换后的查询边界；
    total_cents 是范围内总支出；items 已按 total_cents 降序排列。
    """

    date_from: datetime | None = Field(None, description="统计起始（含），null 表示不限")
    date_to: datetime | None = Field(None, description="统计结束（含），null 表示不限")
    total_cents: int = Field(..., description="总支出，单位「分」")
    items: list[CategoryStat] = Field(..., description="按金额从高到低排序")


# 月份只接受 YYYY-MM，避免把任意字符串写进 budgets.month。
_MONTH_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


class BudgetUpsertRequest(BaseModel):
    """PUT /budgets 的请求体。

    同一个 month + category 重复提交时覆盖原金额，不新增行。
    """

    month: str = Field(..., description="月份，格式 YYYY-MM", examples=["2026-11"])
    category: Category | None = Field(None, description="类别；null 表示整月总预算")
    amount_cents: int = Field(..., gt=0, description="预算金额，单位「分」，必须大于 0")

    @field_validator("month")
    @classmethod
    def _check_month(cls, value: str) -> str:
        """拦截 2026-13、2026-1 之类的非法月份。"""
        if not _MONTH_PATTERN.fullmatch(value):
            raise ValueError("month 格式必须是 YYYY-MM")
        return value


class BudgetItem(BaseModel):
    """一条预算本身，PUT 的响应。"""

    id: int = Field(..., description="预算 id，删除时使用")
    month: str = Field(..., description="月份，YYYY-MM")
    category: Category | None = Field(None, description="类别；null 表示整月总预算")
    amount_cents: int = Field(..., description="预算金额，单位「分」")


class BudgetStatus(BaseModel):
    """预算 + 当月实际支出 + 使用状态，GET /budgets 的列表项。"""

    id: int = Field(..., description="预算 id")
    category: Category | None = Field(None, description="类别；null 表示整月总预算")
    amount_cents: int = Field(..., description="预算金额，单位「分」")
    spent_cents: int = Field(..., description="当月已支出，单位「分」")
    ratio: float = Field(..., ge=0.0, description="已用比例，可能大于 1")
    status: str = Field(..., description="normal=正常，warning=接近上限，exceeded=已超支")


class BudgetOverview(BaseModel):
    """GET /budgets 的响应。

    items 中总预算行（category 为 null）排在最前，其后按类别枚举顺序排列；
    spent_cents 是当月全部类别的总支出，供页面显示整体情况。
    """

    month: str = Field(..., description="月份，YYYY-MM")
    spent_cents: int = Field(..., description="当月总支出，单位「分」")
    items: list[BudgetStatus] = Field(..., description="预算列表，总预算行在前")


class CategoryOption(BaseModel):
    """GET /categories 返回的一项。

    value 是 Category 枚举值，label 是展示文案；
    当前两者都取中文类别名，前端下拉框直接使用。
    """

    value: Category = Field(..., description="类别取值")
    label: str = Field(..., description="展示文案")


class HealthResponse(BaseModel):
    """GET /health 的响应。

    status 固定为 ok；version 来自 FastAPI 应用的 version 字段。
    """

    status: str = Field(..., description="固定为 ok")
    version: str = Field(..., description="应用版本号")


class AiStatusResponse(BaseModel):
    """GET /ai/status 的响应，用于前端和排障时确认模型是否已接上。"""

    configured: bool = Field(..., description="是否配置了可用的模型密钥")
    provider: str = Field(..., description="openai=兼容接口，dify=工作流")
    model: str = Field(..., description="文本模型名")
    vision_model: str = Field(..., description="视觉模型名；空串表示未启用图片识别")
    vision_configured: bool = Field(..., description="图片识别是否可用（模型名与密钥都齐全）")
    missing_config: list[str] = Field(
        default_factory=list,
        description="当前 provider 缺失的环境变量名；空列表表示配置齐全，AI 链路会真的走模型",
    )
    fallback: str = Field(..., description="不可用时的兜底方式")


class ChatRequest(BaseModel):
    """POST /agent/chat 的请求体。"""

    session_id: str | None = Field(
        None,
        max_length=32,
        description="会话 id，由上一轮响应带回；不传表示新会话",
    )
    message: str = Field(..., min_length=1, max_length=2000, description="用户消息")


class ChatResponse(BaseModel):
    """POST /agent/chat 的响应。"""

    session_id: str = Field(..., description="会话 id，下一轮原样带回")
    conversation_id: str | None = Field(
        None, description="Dify 会话 id；兼容接口多轮与本地兜底时为空"
    )
    reply: str = Field(..., description="助手回复")
    engine: str = Field(
        ..., description="dify=Dify 对话应用，llm=兼容接口按历史生成，local=本地兜底"
    )


class ChatMessage(BaseModel):
    """会话历史中的一条消息。"""

    role: str = Field(..., description="user / assistant")
    content: str = Field(..., description="消息内容")
    created_at: datetime = Field(..., description="入库时间，UTC")


class SessionHistory(BaseModel):
    """GET /agent/sessions/{id} 的响应。"""

    session_id: str = Field(..., description="会话 id")
    conversation_id: str | None = Field(None, description="Dify 会话 id")
    messages: list[ChatMessage] = Field(..., description="按时间正序的消息列表")


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
    responses={200: {"content": {"application/json": {"example": _DRAFT_EXAMPLE}}}},
)
def parse_text(payload: ParseTextRequest, conn: ConnDep) -> TransactionDraft:
    """将一句话解析成待确认的草稿，并写入 transaction_drafts。

    参数:
        payload: 请求体。text 已由 ParseTextRequest 去掉首尾空格，长度 1~200。
        conn: 请求级连接，交给 db.save_draft() 写入草稿。

    返回:
        TransactionDraft：source 固定为 text；engine 标明解析引擎；
        missing_fields 列出所有值为 None 的字段。

    调用关系:
        ai_tasks.extract() 优先调大模型、失败退回规则 ->
        db.new_draft_id() 生成 draft_id -> db.save_draft() 写入草稿。
    """
    # 上层 schema 已完成去空格和长度校验，这里直接使用清理后的文本。
    text = payload.text
    extraction = ai_tasks.extract(text)

    draft = TransactionDraft(
        # draft_id 与 transaction_drafts.draft_id 对应，保存账目时回传这个值。
        draft_id=db.new_draft_id(),
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
    return db.save_draft(conn, draft)


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
        conn: 请求级连接，交给 db.create_transaction()。

    返回:
        Transaction：入库后的账目，包含数据库生成的 id 和 created_at。

    调用关系:
        db.create_transaction() 会按 payload.draft_id 查草稿：
        - 找到草稿：优先使用请求的 source，否则用草稿的 source，并删除草稿；
        - 未找到或未传：source 为空时记为 manual。
        草稿删除与账目插入在同一次 commit 中完成。
    """
    # 字段原样交给 store；来源推断、草稿消费和事务提交都在 store 内完成。
    return db.create_transaction(
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
        conn: 请求级连接，交给 db.delete_transaction()。

    返回:
        成功时无响应体（204）；id 不存在时抛 ApiError，由统一异常处理器转成
        {"code": "NOT_FOUND", "message": "账目不存在", "detail": {}}。
    """
    if not db.delete_transaction(conn, transaction_id):
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
        conn: 请求级连接，交给 db.list_transactions()。

    返回:
        TransactionList：items 是当前页账目，total 是符合筛选条件的总条数。

    调用关系:
        db.list_transactions() 先执行 COUNT 得到 total，再执行
        SELECT + ORDER BY occurred_at DESC, id DESC + LIMIT/OFFSET。
    """
    items, total = db.list_transactions(
        conn,
        date_from=params.date_from,
        date_to=params.date_to,
        category=params.category,
        limit=params.limit,
        offset=params.offset,
    )
    return TransactionList(items=items, total=total)


query_router = APIRouter(
    prefix="/query",
    tags=["查账"],
    responses=VALIDATION_ERROR_RESPONSES,
)

# /query/ask 的明细模式返回条数上限。
_LIST_LIMIT = 20


def _yuan(cents: int) -> str:
    """金额按元展示，保留两位小数。"""
    return f"{cents / 100:.2f}"


def _range_text(date_from: datetime | None, date_to: datetime | None) -> str:
    """把查询区间转成中文描述，供结论文案使用。"""
    if date_from is None and date_to is None:
        return "全部时间"
    start = date_from.astimezone(ai_client.APP_TZ).strftime("%Y-%m-%d") if date_from else "最早"
    end = date_to.astimezone(ai_client.APP_TZ).strftime("%Y-%m-%d") if date_to else "现在"
    return f"{start} 至 {end}"


def _pick_category(
    totals: list[db.CategoryTotal],
    category: object,
) -> tuple[int, int]:
    """按类别过滤聚合结果，返回 (合计分, 笔数)。"""
    matched = [item for item in totals if item.category == category]
    return (
        sum(item.total_cents for item in matched),
        sum(item.count for item in matched),
    )


@query_router.post(
    "/parse",
    response_model=QueryIntentOut,
    summary="解析查账问题",
    description=(
        "把口语化问题转成类别与时间区间，不访问数据库。\n\n"
        "engine 为 llm 表示大模型解析，rule 表示规则兜底。"
    ),
)
def parse_question(payload: QueryRequest) -> QueryIntentOut:
    """解析问句并返回查询条件。"""
    intent = ai_tasks.parse_query(payload.question, datetime.now(UTC))
    return QueryIntentOut(
        category=intent.category,
        # 接口约定时间统一用 UTC，规则兜底给的是北京时间，这里转换。
        date_from=ensure_utc(intent.date_from) if intent.date_from else None,
        date_to=ensure_utc(intent.date_to) if intent.date_to else None,
        intent=intent.intent,
        confidence=intent.confidence,
        engine=intent.engine,
    )


@query_router.post(
    "/ask",
    response_model=QueryAnswer,
    summary="用一句话查询并得到结果",
    description=(
        "解析问题后直接查询账目，返回合计金额、笔数和一句结论；"
        "intent=list 时额外返回明细列表。"
    ),
)
def ask_question(payload: QueryRequest, conn: ConnDep) -> QueryAnswer:
    """解析问句、查询账目并组装结论文案。

    参数:
        payload: 用户问题。
        conn: 请求级数据库连接。

    返回:
        QueryAnswer：命中范围的合计金额、笔数、结论文案；明细模式带 items。
    """
    question = payload.question
    intent = ai_tasks.parse_query(question, datetime.now(UTC))
    totals, month_total = db.category_totals(
        conn, date_from=intent.date_from, date_to=intent.date_to
    )
    total_count = sum(item.count for item in totals)

    if intent.category is not None:
        total_cents, count = _pick_category(totals, intent.category)
    else:
        total_cents, count = month_total, total_count

    scope = f"{_range_text(intent.date_from, intent.date_to)}"
    category_text = f"「{intent.category.value}」的" if intent.category else ""
    items: list[Transaction] = []

    if intent.intent == "top":
        if totals:
            top = totals[0]
            answer = (
                f"{scope}支出最多的类别是 {top.category.value}，"
                f"共 {_yuan(top.total_cents)} 元（{top.count} 笔）"
            )
        else:
            answer = f"{scope}还没有账目"
    elif intent.intent == "count":
        answer = f"{scope}{category_text}账单共 {count} 笔，合计 {_yuan(total_cents)} 元"
    elif intent.intent == "list":
        items, _ = db.list_transactions(
            conn,
            date_from=intent.date_from,
            date_to=intent.date_to,
            category=intent.category,
            limit=_LIST_LIMIT,
            offset=0,
        )
        answer = (
            f"{scope}{category_text}最近 {len(items)} 笔账单，"
            f"合计 {_yuan(total_cents)} 元"
        )
    else:
        answer = f"{scope}{category_text}支出合计 {_yuan(total_cents)} 元（{count} 笔）"

    return QueryAnswer(
        question=question,
        intent=intent.intent,
        category=intent.category,
        date_from=ensure_utc(intent.date_from) if intent.date_from else None,
        date_to=ensure_utc(intent.date_to) if intent.date_to else None,
        total_cents=total_cents,
        count=count,
        answer=answer,
        items=items,
        engine=intent.engine,
    )


receipts_router = APIRouter(
    prefix="/receipts",
    tags=["小票识别"],
    # 400/413/415 分别对应 parse_receipt() 里的三个校验分支：
    # 空文件、超过 32MB、MIME 不在 _ALLOWED_TYPES；422 复用统一校验错误结构。
    responses={
        **VALIDATION_ERROR_RESPONSES,
        400: {"model": ErrorResponse, "description": "文件为空"},
        413: {"model": ErrorResponse, "description": "图片超过 32MB"},
        415: {"model": ErrorResponse, "description": "不支持的图片格式"},
    },
)

# 允许的图片 MIME 类型。这里只校验客户端声明的 content_type，
# 实际文件内容需要后续图像库或视觉模型进一步判断。
_ALLOWED_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
# 单张图片上限 32MB；parse_receipt() 会多读 1 字节判断是否超限。
_MAX_BYTES = 32 * 1024 * 1024


@receipts_router.post(
    "/parse",
    response_model=TransactionDraft,
    summary="上传小票/截图，识别消费信息",
    description=(
        "接收 `multipart/form-data`，文件字段名 `file`。\n\n"
        "识别出的字段直接返回；未能识别的字段为 null 并列入 `missing_fields`。"
    ),
    responses={
        200: {
            "content": {
                "application/json": {
                    "example": {
                        "draft_id": "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6",
                        "source": "receipt",
                        "amount_cents": 3250,
                        "merchant": "老乡鸡",
                        "occurred_at": "2026-11-11T04:30:00Z",
                        "category": "餐饮",
                        "confidence": 0.92,
                        "missing_fields": [],
                        "raw_text": None,
                    }
                }
            }
        }
    },
)
def parse_receipt(
    conn: ConnDep,
    file: UploadFile = File(..., description="小票或支付截图"),
) -> TransactionDraft:
    """校验上传文件，识别字段后写入 source=receipt 的草稿。

    参数:
        conn: 请求级连接，交给 db.save_draft() 写入 transaction_drafts。
        file: multipart 字段名为 file；content_type 和文件大小在这里校验。

    返回:
        TransactionDraft：识别出的字段有值，识别不出的为 None 并列入 missing_fields；
        视觉模型不可用或调用失败时四个字段全为 None、confidence 为 0。

    错误:
        ApiError 由 core/errors.py 的异常处理器转成 ErrorResponse：
        - 415 UNSUPPORTED_IMAGE_TYPE：content_type 不在 _ALLOWED_TYPES；
        - 413 IMAGE_TOO_LARGE：读出的字节数超过 _MAX_BYTES；
        - 400 EMPTY_FILE：读出的字节数为 0。
    """
    if file.content_type not in _ALLOWED_TYPES:
        # 先校验 MIME，避免把非图片内容读进来；content_type 由客户端提供，不能完全信任。
        raise ApiError(
            code="UNSUPPORTED_IMAGE_TYPE",
            message=f"不支持的图片格式：{file.content_type}，请使用 JPEG / PNG / GIF / WebP",
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
        )

    # UploadFile.file 是底层临时文件；最多读 _MAX_BYTES + 1 字节，
    # 多出的 1 字节只用于判断是否超限，避免把超大文件整个读进内存。
    content = file.file.read(_MAX_BYTES + 1)
    if len(content) > _MAX_BYTES:
        raise ApiError(
            code="IMAGE_TOO_LARGE",
            message="图片超过 32MB",
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail={"limit_bytes": _MAX_BYTES},
        )
    if not content:
        raise ApiError(code="EMPTY_FILE", message="文件为空")

    # 视觉模型抽取；未配置视觉模型或调用失败时返回空结果，由前端提示用户手动补全。
    recognition = ai_tasks.recognize(content, file.content_type or "image/png")

    draft = TransactionDraft(
        # draft_id 由 db.new_draft_id() 生成，后续保存账目时回传。
        draft_id=db.new_draft_id(),
        source=Source.RECEIPT,
        amount_cents=recognition.amount_cents,
        merchant=recognition.merchant,
        occurred_at=recognition.occurred_at,
        category=recognition.category,
        confidence=recognition.confidence,
        # 与草稿字段顺序一致，前端按这个数组标记待补项。
        missing_fields=[
            name
            for name, value in (
                ("amount_cents", recognition.amount_cents),
                ("merchant", recognition.merchant),
                ("occurred_at", recognition.occurred_at),
                ("category", recognition.category),
            )
            if value is None
        ],
        raw_text=recognition.raw_text,
        engine=recognition.engine,
    )
    # save_draft() 写入 transaction_drafts 并 commit，返回值直接作为响应。
    return db.save_draft(conn, draft)


stats_router = APIRouter(
    prefix="/stats",
    tags=["统计"],
    # 422 使用统一 ErrorResponse，见 app/core/errors.py。
    responses=VALIDATION_ERROR_RESPONSES,
)


# 200 示例只用于 OpenAPI 文档展示；真实的 items、金额和 ratio
# 由 db.category_totals() 的聚合结果和下方计算逻辑决定。
@stats_router.get(
    "/category",
    response_model=CategoryStatsResponse,
    summary="按类别统计支出",
    description=(
        "按类别与时间范围统计支出。\n\n"
        "金额字段为 total_cents，单位为分；items 按金额从高到低排序。"
    ),
    responses={
        200: {
            "content": {
                "application/json": {
                    "example": {
                        "date_from": "2026-11-01T00:00:00Z",
                        "date_to": "2026-11-30T23:59:59Z",
                        "total_cents": 185000,
                        "items": [
                            {"category": "餐饮", "total_cents": 96000, "count": 23, "ratio": 0.519},
                            {"category": "购物", "total_cents": 47000, "count": 5, "ratio": 0.254},
                            {"category": "交通", "total_cents": 42000, "count": 11, "ratio": 0.227},
                        ],
                    }
                }
            }
        }
    },
)
def category_stats(
    params: Annotated[DateRangeQuery, Query()],
    conn: ConnDep,
) -> CategoryStatsResponse:
    """按类别汇总指定时间范围内的支出。

    参数:
        params: DateRangeQuery，包含可选的 from / to；两者都会转成 UTC，
                且 from 不能晚于 to。
        conn: 请求级连接，交给 db.category_totals()。

    返回:
        CategoryStatsResponse：total_cents 是范围内总支出；
        items 按类别总额降序，每项 ratio = 该类 total_cents / 总 total_cents，
        保留 4 位小数。

    调用关系:
        db.category_totals() 执行
        GROUP BY category + SUM(amount_cents) + COUNT(id)，
        返回 CategoryTotal 列表和总金额；本函数只负责组装响应和计算 ratio。
    """
    items, total_cents = db.category_totals(
        conn,
        date_from=params.date_from,
        date_to=params.date_to,
    )

    return CategoryStatsResponse(
        date_from=params.date_from,
        date_to=params.date_to,
        total_cents=total_cents,
        items=[
            CategoryStat(
                category=item.category,
                total_cents=item.total_cents,
                count=item.count,
                # total_cents 为 0 说明范围内没有账目，避免除零，ratio 统一为 0。
                ratio=round(item.total_cents / total_cents, 4) if total_cents else 0.0,
            )
            for item in items
        ],
    )


budgets_router = APIRouter(
    prefix="/budgets",
    tags=["预算"],
    # 422 使用统一 ErrorResponse，与其余业务路由保持一致。
    responses=VALIDATION_ERROR_RESPONSES,
)

# 月份边界按北京时间计算：用户说的「11 月」指 11-01 00:00 到 11-30 23:59:59。
_APP_TZ = timezone(timedelta(hours=8))

# 返回列表时总预算行排最前，其余按 Category 声明顺序。
_CATEGORY_ORDER = {category: index for index, category in enumerate(Category)}

# 月份查询参数只接受 YYYY-MM，与 BudgetUpsertRequest 的校验保持一致。
_MONTH_QUERY = Query(
    ...,
    pattern=r"^\d{4}-(0[1-9]|1[0-2])$",
    description="月份，格式 YYYY-MM",
    examples=["2026-11"],
)


def _month_range(month: str) -> tuple[datetime, datetime]:
    """把 YYYY-MM 转成当月首尾时间（北京时间，含毫秒精度）。

    结束时间取「下月 1 日 00:00」减 1 微秒，
    这样 db.category_totals() 的 <= date_to 不会把下月第一笔算进来。
    """
    year, month_number = int(month[:4]), int(month[5:])
    start = datetime(year, month_number, 1, tzinfo=_APP_TZ)
    next_month = datetime(
        year + (month_number == 12),
        month_number % 12 + 1,
        1,
        tzinfo=_APP_TZ,
    )
    return start, next_month - timedelta(microseconds=1)


def _status_of(ratio: float, threshold: float) -> str:
    """按已用比例与预算阈值给出状态字符串。

    threshold 来自 budgets.threshold（缺省 0.8），只用于 warning 判定；
    花光预算（ratio >= 1）无论阈值多少都算 exceeded。
    """
    if ratio >= 1:
        return "exceeded"
    if ratio >= threshold:
        return "warning"
    return "normal"


@budgets_router.get(
    "",
    response_model=BudgetOverview,
    summary="查询月度预算与使用状态",
    description="返回当月预算列表，并按当月实际支出计算 ratio 与 status。",
)
def get_budgets(month: Annotated[str, _MONTH_QUERY], conn: ConnDep) -> BudgetOverview:
    """读取某月预算并计算使用状态。

    参数:
        month: 查询参数，格式 YYYY-MM。
        conn: 请求级连接。

    返回:
        BudgetOverview：spent_cents 是当月总支出；
        items 中总预算行（category 为 null）排在最前，其后按类别顺序。
    """
    date_from, date_to = _month_range(month)
    rows = db.list_budgets(conn, month)
    totals, month_spent = db.category_totals(
        conn,
        date_from=date_from,
        date_to=date_to,
    )
    spent_by_category = {item.category: item.total_cents for item in totals}

    items: list[BudgetStatus] = []
    for row in rows:
        if row.category:
            category = Category(row.category)
            spent = spent_by_category.get(category, 0)
        else:
            category = None
            spent = month_spent
        ratio = spent / row.amount_cents
        items.append(
            BudgetStatus(
                id=row.id,
                category=category,
                amount_cents=row.amount_cents,
                spent_cents=spent,
                ratio=ratio,
                status=_status_of(ratio, row.threshold),
            )
        )
    items.sort(key=lambda item: (item.category is not None, _CATEGORY_ORDER.get(item.category, 99)))
    return BudgetOverview(month=month, spent_cents=month_spent, items=items)


@budgets_router.put(
    "",
    response_model=BudgetItem,
    summary="设置月度预算",
    description="同一月份同一类别重复提交时覆盖原金额。",
)
def upsert_budget(payload: BudgetUpsertRequest, conn: ConnDep) -> BudgetItem:
    """写入或覆盖一条预算。

    参数:
        payload: month / category / amount_cents；category 为 null 表示总预算。
        conn: 请求级连接。

    返回:
        BudgetItem：落库后的预算，包含 id。
    """
    row = db.upsert_budget(
        conn,
        month=payload.month,
        category=payload.category.value if payload.category else "",
        amount_cents=payload.amount_cents,
    )
    return BudgetItem(
        id=row.id,
        month=row.month,
        category=Category(row.category) if row.category else None,
        amount_cents=row.amount_cents,
    )


@budgets_router.delete(
    "/{budget_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除预算",
    responses={404: {"description": "预算不存在"}},
)
def delete_budget(
    budget_id: Annotated[int, PathParam(description="预算 id")],
    conn: ConnDep,
) -> None:
    """删除一条预算；不存在时返回 404。"""
    if not db.delete_budget(conn, budget_id):
        raise ApiError(
            code="BUDGET_NOT_FOUND",
            message="预算不存在",
            status_code=status.HTTP_404_NOT_FOUND,
        )


agent_router = APIRouter(
    prefix="/agent",
    tags=["智能助手"],
    responses=VALIDATION_ERROR_RESPONSES,
)


@agent_router.post("/chat", response_model=ChatResponse, summary="与助手多轮对话")
def chat(payload: ChatRequest, conn: ConnDep) -> ChatResponse:
    """处理一条用户消息并写入会话历史。

    参数:
        payload: session_id 可为空；为空时新建会话。
        conn: 请求级连接。

    返回:
        ChatResponse：回复文本、生成方式与会话标识；下一轮把 session_id 带回来即可。
    """
    reply = ai_chat.chat(conn, payload.message, payload.session_id)
    return ChatResponse(
        session_id=reply.session_id,
        conversation_id=reply.conversation_id,
        reply=reply.reply,
        engine=reply.engine,
    )


@agent_router.get(
    "/sessions/{session_id}",
    response_model=SessionHistory,
    summary="读取会话历史",
    responses={404: {"description": "会话不存在"}},
)
def history(
    session_id: Annotated[str, PathParam(max_length=32, description="会话 id")],
    conn: ConnDep,
) -> SessionHistory:
    """按会话 id 返回历史消息；会话不存在时返回 404。"""
    session = db.get_chat_session(conn, session_id)
    if not session:
        raise ApiError(
            code="SESSION_NOT_FOUND",
            message="会话不存在",
            status_code=status.HTTP_404_NOT_FOUND,
        )
    return SessionHistory(
        session_id=session.session_id,
        conversation_id=session.conversation_id,
        messages=[ChatMessage(**item) for item in db.list_chat_messages(conn, session_id)],
    )
