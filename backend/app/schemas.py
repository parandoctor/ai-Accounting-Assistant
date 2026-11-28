"""接口数据结构：请求与响应模型。"""

import re
from datetime import datetime
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models import Category, Source
from app.utils import APP_TZ, MAX_AMOUNT_CENTS, ensure_utc

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
    amount_cents: int = Field(
        ...,
        gt=0,
        le=MAX_AMOUNT_CENTS,
        description="金额，单位「分」，必须大于 0 且不超过 99999999.99 元",
    )
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
        """消费时间必须带时区，转成 UTC 后写入 transactions.occurred_at。

        只允许补记已经发生的消费：按东八区日期比较，晚于今天的直接拒绝，
        避免「提示保存成功、明细里却看不到」这种未来日期脏数据。
        """
        occurred = ensure_utc(value)
        if occurred.astimezone(APP_TZ).date() > datetime.now(APP_TZ).date():
            raise ValueError("消费日期不能晚于今天")
        return occurred

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
    intent: str = Field(
        ...,
        description="total=合计金额，count=笔数，top=哪类最多，list=明细，unknown=没解析出条件",
    )
    confidence: float = Field(..., ge=0.0, le=1.0, description="解析置信度")
    engine: str = Field(..., description="llm=大模型解析，rule=规则兜底")
    understood: bool = Field(
        ...,
        description="是否解析出可执行的查询条件；false 时 /query/ask 只给提示，不做汇总",
    )


class QueryAnswer(BaseModel):
    """POST /query/ask 的响应：查询条件 + 查到的数据 + 一句可直接展示的结论。"""

    question: str = Field(..., description="原始问题")
    intent: str = Field(..., description="total/count/top/list，没解析出条件时为 unknown")
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
    amount_cents: int = Field(
        ...,
        gt=0,
        le=MAX_AMOUNT_CENTS,
        description="预算金额，单位「分」，必须大于 0 且不超过 99999999.99 元",
    )

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
