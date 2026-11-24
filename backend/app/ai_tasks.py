"""三类抽取任务与规则兜底。

- extract()：一句话 -> 支出字段（ExpenseExtraction）；
- recognize()：小票/截图 -> 识别字段（ReceiptRecognition）；
- parse_query()：问句 -> 查询条件（QueryIntent）。

每个任务都优先调用模型，模型不可用或返回不合法时退回规则解析，
返回值里的 engine 标明这次结果来自 llm 还是 rule。
"""

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from app import ai_client
from app.ai_client import (
    APP_TZ,
    amount_cents,
    category,
    confidence,
    date_range,
    merchant,
    occurred_at,
)
from app.db import Category, ensure_utc

logger = logging.getLogger(__name__)

# 关键词 → 类别。按插入顺序匹配，命中即停，所以具体词要排在宽泛词前面：
# 「药」「医院」必须早于「买」，否则「买药」会被判成购物。
CATEGORY_KEYWORDS = {
    # 餐饮
    "饭": Category.FOOD, "午餐": Category.FOOD, "晚饭": Category.FOOD,
    "早饭": Category.FOOD, "早餐": Category.FOOD, "外卖": Category.FOOD,
    "咖啡": Category.FOOD, "奶茶": Category.FOOD, "饮料": Category.FOOD,
    "零食": Category.FOOD,
    # 医疗
    "药": Category.MEDICAL, "医院": Category.MEDICAL, "挂号": Category.MEDICAL,
    "体检": Category.MEDICAL,
    # 交通
    "打车": Category.TRANSPORT, "地铁": Category.TRANSPORT, "公交": Category.TRANSPORT,
    "加油": Category.TRANSPORT, "高铁": Category.TRANSPORT, "机票": Category.TRANSPORT,
    # 居住
    "房租": Category.HOUSING, "水电": Category.HOUSING, "物业": Category.HOUSING,
    "燃气": Category.HOUSING,
    # 娱乐
    "电影": Category.ENTERTAINMENT, "游戏": Category.ENTERTAINMENT,
    "演唱会": Category.ENTERTAINMENT, "旅游": Category.ENTERTAINMENT,
    # 购物：宽泛的「买」放最后，避免抢走前面更具体的类别
    "淘宝": Category.SHOPPING, "京东": Category.SHOPPING, "超市": Category.SHOPPING,
    "商场": Category.SHOPPING, "买": Category.SHOPPING,
}

# 记账用的本地时区，与前端提交的 +08:00 保持一致。
_APP_TZ = timezone(timedelta(hours=8))

# 匹配第一个金额数字，允许一两位小数；不区分「元 / 块」等单位。
_AMOUNT_PATTERN = re.compile(r"(\d+(?:\.\d{1,2})?)")

# 「最近 7 天」这类相对区间。
_RECENT_DAYS = re.compile(r"最近\s*(\d+)\s*天")


def amount_cents_from_text(text: str) -> int | None:
    """从文本里取第一个金额，元转分；取不到返回 None。"""
    match = _AMOUNT_PATTERN.search(text)
    if not match:
        return None
    return int((Decimal(match.group(1)) * 100).to_integral_value())


def category_from_text(text: str) -> Category | None:
    """按关键词表匹配类别，命中第一个就返回，未命中返回 None。"""
    for keyword, category in CATEGORY_KEYWORDS.items():
        if keyword in text:
            return category
    return None


def parse_text(text: str) -> dict:
    """规则抽取一句话记账，返回与模型抽取同名的字段。

    规则不识别商户和消费时间，这两项固定为 None，由用户在预览页补全。
    """
    return {
        "amount_cents": amount_cents_from_text(text),
        "merchant": None,
        "occurred_at": None,
        "category": category_from_text(text),
        # 规则匹配的固定置信度，不是模型概率。
        "confidence": 0.4,
    }


def intent_from_text(text: str) -> str:
    """判断问句意图：top=哪类最多，count=笔数，list=明细，其余按 total 处理。"""
    if any(word in text for word in ("最多", "哪一类", "哪个类别", "最贵")):
        return "top"
    if any(word in text for word in ("几笔", "多少笔", "次数", "几单")):
        return "count"
    if any(word in text for word in ("列表", "明细", "最近", "几条")):
        return "list"
    return "total"


def date_range_from_text(text: str, now: datetime) -> tuple[datetime | None, datetime | None]:
    """按问句里的时间词给出闭区间起止时间。

    参数:
        text: 用户问句。
        now: 当前时间，带时区；用它推算「这个月」「上个月」等相对区间。

    返回:
        (起, 止)，都是带时区的 datetime；没有时间词时返回 (None, None) 表示不限时间。
    """
    local_now = now.astimezone(_APP_TZ)

    if "今天" in text:
        start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        return start, start + timedelta(days=1) - timedelta(microseconds=1)
    if "昨天" in text:
        start = local_now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
        return start, start + timedelta(days=1) - timedelta(microseconds=1)
    if "上个月" in text:
        first_this_month = local_now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        start = (first_this_month - timedelta(days=1)).replace(day=1)
        return start, first_this_month - timedelta(microseconds=1)
    if "这个月" in text or "本月" in text:
        start = local_now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        next_month = (start + timedelta(days=32)).replace(day=1)
        return start, next_month - timedelta(microseconds=1)
    if "今年" in text:
        start = local_now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        return start, local_now.replace(
            month=12, day=31, hour=23, minute=59, second=59, microsecond=999999
        )
    recent = _RECENT_DAYS.search(text)
    if recent:
        start = local_now - timedelta(days=int(recent.group(1)))
        return start, local_now
    return None, None


def parse_question(text: str, now: datetime) -> dict:
    """规则解析自然语言查账，返回与模型输出同名的字段。"""
    date_from, date_to = date_range_from_text(text, now)
    return {
        "category": category_from_text(text),
        "date_from": date_from,
        "date_to": date_to,
        "intent": intent_from_text(text),
        "confidence": 0.35,
    }



_TEXT_PROMPT = """你是个人记账助手的字段抽取器。从用户的一句话里提取一笔消费，只输出一个 JSON 对象：
{"amount": 数字或 null, "merchant": 字符串或 null, "occurred_at": "ISO8601 带时区"或 null, "category": 字符串或 null, "confidence": 0 到 1 的小数}

规则：
1. amount 单位是元，只给数字，不带符号；
2. category 只能是 餐饮、交通、购物、娱乐、医疗、居住、其他 之一，判断不了写 null；
3. occurred_at 根据参考时间推算相对说法（今天、昨天、上周五），必须带 +08:00；
4. 用户没提到的字段一律写 null，不要编造；
5. confidence 表示你对整条结果的把握程度。

参考时间：{now}
"""


@dataclass(frozen=True)
class ExpenseExtraction:
    """一次文本抽取的结果。

    missing_fields 不在这里计算，接口层会根据值为 None 的字段自动得出。
    """

    amount_cents: int | None
    merchant: str | None
    occurred_at: datetime | None
    category: Category | None
    confidence: float
    engine: str


def extract(text: str) -> ExpenseExtraction:
    """把一句话解析成结构化支出。

    参数:
        text: 用户输入的自然语言，已去除首尾空格，长度 1~200。

    返回:
        ExpenseExtraction：未识别字段为 None；engine 为 llm 或 rule。
    """
    if ai_client.configured():
        try:
            payload = ai_client.extract_json(
                ai_client.TASK_TEXT,
                # 提示词里有 JSON 花括号，str.format 会把它当占位符，这里只做字符串替换。
                _TEXT_PROMPT.replace(
                    "{now}",
                    datetime.now(APP_TZ).isoformat(timespec="minutes"),
                ),
                text,
            )
            return ExpenseExtraction(
                amount_cents=amount_cents(payload.get("amount")),
                merchant=merchant(payload.get("merchant")),
                occurred_at=occurred_at(payload.get("occurred_at")),
                category=category(payload.get("category")),
                confidence=confidence(payload.get("confidence")),
                engine="llm",
            )
        except ai_client.AiUnavailable as exc:
            # 模型不可用不影响记账，退回规则解析并在日志里留痕。
            logger.warning("文本抽取退回规则解析：%s", exc)

    parsed = parse_text(text)
    return ExpenseExtraction(
        amount_cents=parsed["amount_cents"],
        merchant=parsed["merchant"],
        occurred_at=parsed["occurred_at"],
        category=parsed["category"],
        confidence=parsed["confidence"],
        engine="rule",
    )



_RECEIPT_PROMPT = """你是小票识别助手。看图片里的消费凭证，输出一个 JSON 对象：
{"amount": 数字或 null, "merchant": 字符串或 null, "occurred_at": "ISO8601 带时区"或 null, "category": 字符串或 null, "raw_text": 图片里的关键文字, "confidence": 0 到 1 的小数}

规则：
1. amount 是实付金额，单位元，只给数字；
2. category 只能是 餐饮、交通、购物、娱乐、医疗、居住、其他 之一；
3. occurred_at 取小票上的交易时间，带 +08:00；看不清写 null；
4. raw_text 保留金额、商户、时间等关键文字，便于人工核对；
5. 图片里没有的信息一律写 null，不要猜。
"""


@dataclass(frozen=True)
class ReceiptRecognition:
    """一次小票识别的结果。

    raw_text 保留识别到的文字，便于人工核对，也可作为规则抽取的输入。
    """

    raw_text: str | None
    amount_cents: int | None
    merchant: str | None
    occurred_at: datetime | None
    category: Category | None
    confidence: float
    engine: str


def _empty() -> ReceiptRecognition:
    """未识别时的空结果，字段全空、置信度为 0。"""
    return ReceiptRecognition(
        raw_text=None,
        amount_cents=None,
        merchant=None,
        occurred_at=None,
        category=None,
        confidence=0.0,
        engine="none",
    )


def recognize(image: bytes, mime: str) -> ReceiptRecognition:
    """识别一张小票或支付截图。

    参数:
        image: 上传文件的内容字节，MIME 和大小已由接口层校验。
        mime: 图片的 MIME 类型，用于拼 data URL。

    返回:
        ReceiptRecognition：未识别字段为 None；engine 为 llm 或 none。
    """
    if not ai_client.vision_configured():
        return _empty()

    try:
        payload = ai_client.extract_json(
            ai_client.TASK_RECEIPT,
            _RECEIPT_PROMPT,
            "请识别这张消费凭证。",
            image=image,
            mime=mime,
        )
    except ai_client.AiUnavailable as exc:
        logger.warning("小票识别失败，返回空草稿：%s", exc)
        return _empty()

    raw_text = payload.get("raw_text")
    return ReceiptRecognition(
        raw_text=raw_text.strip()[:2000] if isinstance(raw_text, str) and raw_text.strip() else None,
        amount_cents=amount_cents(payload.get("amount")),
        merchant=merchant(payload.get("merchant")),
        occurred_at=occurred_at(payload.get("occurred_at")),
        category=category(payload.get("category")),
        confidence=confidence(payload.get("confidence")),
        engine="llm",
    )



_QUERY_PROMPT = """你是记账应用的查询条件解析器。把用户的问题转成一个 JSON 对象：
{"category": 字符串或 null, "date_from": "ISO8601 带时区"或 null, "date_to": "ISO8601 带时区"或 null, "intent": "total" 或 "count" 或 "top" 或 "list", "confidence": 0 到 1 的小数}

规则：
1. category 只能是 餐饮、交通、购物、娱乐、医疗、居住、其他 之一，问题没限定类别就写 null；
2. date_from / date_to 是消费时间的闭区间，根据参考时间推算（这个月、上个月、今年以来），必须带 +08:00；
3. intent：问「多少钱 / 花了多少」用 total；问「几笔 / 几次」用 count；问「哪类最多」用 top；要明细或最近几条用 list；
4. 问题里没有的限定条件一律写 null，不要编造。

参考时间：{now}
"""


@dataclass(frozen=True)
class QueryIntent:
    """一次问句解析的结果。"""

    category: Category | None
    date_from: datetime | None
    date_to: datetime | None
    intent: str
    confidence: float
    engine: str


def _intent(value: object) -> str:
    """模型给的意图必须落在四个取值内，其余按 total 处理。"""
    allowed = {"total", "count", "top", "list"}
    return value if isinstance(value, str) and value in allowed else "total"


def parse_query(question: str, now: datetime) -> QueryIntent:
    """把问句解析成查询条件。

    参数:
        question: 用户问题，已去首尾空格。
        now: 当前时间，用于规则兜底推算相对日期。

    返回:
        QueryIntent：没有限定条件时 category 与起止时间都为 None。
    """
    if ai_client.configured():
        try:
            payload = ai_client.extract_json(
                ai_client.TASK_QUERY,
                # 提示词里有 JSON 花括号，str.format 会把它当占位符，这里只做字符串替换。
                _QUERY_PROMPT.replace(
                    "{now}",
                    now.astimezone(APP_TZ).isoformat(timespec="minutes"),
                ),
                question,
            )
            date_from, date_to = date_range(
                payload.get("date_from"), payload.get("date_to")
            )
            return QueryIntent(
                category=category(payload.get("category")),
                date_from=date_from,
                date_to=date_to,
                intent=_intent(payload.get("intent")),
                confidence=confidence(payload.get("confidence")),
                engine="llm",
            )
        except ai_client.AiUnavailable as exc:
            logger.warning("查询解析退回规则：%s", exc)

    parsed = parse_question(question, now)
    return QueryIntent(
        category=parsed["category"],
        date_from=parsed["date_from"],
        date_to=parsed["date_to"],
        intent=parsed["intent"],
        confidence=parsed["confidence"],
        engine="rule",
    )
