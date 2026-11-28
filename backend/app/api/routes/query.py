"""查账接口：问句解析与直接返回结果。"""

from datetime import UTC, datetime

from fastapi import APIRouter

from app import operations
from app.ai import client, tasks
from app.api.dependencies import ConnDep
from app.core.errors import VALIDATION_ERROR_RESPONSES
from app.models import CategoryTotal
from app.schemas import QueryAnswer, QueryIntentOut, QueryRequest, Transaction
from app.utils import ensure_utc

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
    start = date_from.astimezone(client.APP_TZ).strftime("%Y-%m-%d") if date_from else "最早"
    end = date_to.astimezone(client.APP_TZ).strftime("%Y-%m-%d") if date_to else "现在"
    return f"{start} 至 {end}"


def _pick_category(
    totals: list[CategoryTotal],
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
    intent = tasks.parse_query(payload.question, datetime.now(UTC))
    return QueryIntentOut(
        category=intent.category,
        # 接口约定时间统一用 UTC，规则兜底给的是北京时间，这里转换。
        date_from=ensure_utc(intent.date_from) if intent.date_from else None,
        date_to=ensure_utc(intent.date_to) if intent.date_to else None,
        intent=intent.intent,
        confidence=intent.confidence,
        engine=intent.engine,
        understood=intent.understood,
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
    intent = tasks.parse_query(question, datetime.now(UTC))

    # 没解析出时间也没解析出类别时，不做全量汇总：全量数字看起来像答案，
    # 用户会当成「查询条件生效了」，实际是在读全部账目。
    if not intent.understood:
        return QueryAnswer(
            question=question,
            intent="unknown",
            category=intent.category,
            date_from=None,
            date_to=None,
            total_cents=0,
            count=0,
            answer=(
                "这句话里没认出时间或类别，暂时没法统计。"
                "可以说「2017 年花了多少」「这个月餐饮支出」，或者问「总共花了多少」。"
            ),
            items=[],
            engine=intent.engine,
        )

    totals, month_total = operations.category_totals(
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
        items, _ = operations.list_transactions(
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
