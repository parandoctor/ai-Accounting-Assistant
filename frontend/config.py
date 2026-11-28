"""前端配置常量：后端地址、时区、类别枚举、图表配色与版本号。"""

from __future__ import annotations

import os
from datetime import timedelta, timezone



API_URL = os.getenv("API_URL", "http://127.0.0.1:8000").rstrip("/")


TZ = timezone(timedelta(hours=8))


CATEGORIES = ["餐饮", "交通", "购物", "娱乐", "医疗", "居住", "其他"]


DEFAULT_CATEGORY = "其他"


# 类别图标：记账页的类别网格与统计页的类型筛选共用
CATEGORY_ICONS = {
    "餐饮": "🍜",
    "交通": "🚌",
    "购物": "🛍️",
    "娱乐": "🎮",
    "医疗": "💊",
    "居住": "🏠",
    "其他": "📦",
}


CATEGORY_COLORS = [
    "#F59E0B",  # 餐饮
    "#3B82F6",  # 交通
    "#EC4899",  # 购物
    "#8B5CF6",  # 娱乐
    "#10B981",  # 医疗
    "#6366F1",  # 居住
    "#9CA3AF",  # 其他
]


APP_VERSION = "v0.1.0"
