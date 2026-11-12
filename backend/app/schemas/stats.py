"""统计相关的响应模型。"""

from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.transaction import Category


class CategoryStat(BaseModel):
    """单个类别的统计结果。"""

    category: Category
    total_cents: int = Field(..., description="该类总支出，单位「分」")
    count: int = Field(..., description="该类笔数")
    ratio: float = Field(..., ge=0.0, le=1.0, description="占总支出比例，0~1")


class CategoryStatsResponse(BaseModel):
    """按类别统计的响应。"""

    date_from: datetime | None = Field(None, description="统计起始（含），null 表示不限")
    date_to: datetime | None = Field(None, description="统计结束（含），null 表示不限")
    total_cents: int = Field(..., description="总支出，单位「分」")
    items: list[CategoryStat] = Field(..., description="按金额从高到低排序")
