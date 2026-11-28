"""通用工具：时区校验与转换、金额元分换算。"""

from datetime import UTC, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

# 项目统一时区：前端按 +08:00 提交，规则解析的「今天 / 这个月」也按它推算。
APP_TZ = timezone(timedelta(hours=8))

# 单笔金额上限，单位为「分」，对应 transactions.amount 的 decimal(10,2)：
# 超过这个值写库会触发 Out of range，必须在入参校验阶段拦下。
MAX_AMOUNT_CENTS = 9_999_999_999


def ensure_utc(value: datetime) -> datetime:
    """校验 datetime 带时区并转成 UTC；无时区时抛 ValueError（框架转 422）。"""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("时间必须带时区，例如 2026-11-01T00:00:00+08:00")
    return value.astimezone(UTC)


# 数据库金额是两位小数，换算后用这个模板把多余的精度截掉。
_CENTS = Decimal("0.01")


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
