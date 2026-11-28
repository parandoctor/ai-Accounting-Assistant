"""页面：账单明细与分类统计，含删除、趋势图与 CSV 导出。"""

from __future__ import annotations

from datetime import date, time, timedelta

import altair as alt
import pandas as pd
import streamlit as st

from config import CATEGORIES, CATEGORY_COLORS, CATEGORY_ICONS
from api_client import ApiError, api_delete, api_get
from formatting import (
    export_csv,
    fmt_local,
    fmt_money,
    iso_at,
    month_options,
    to_local,
    to_yuan,
    transactions_frame,
)
from components import empty_state, flash


RANGE_OPTIONS = ["本月", "上月", "近 30 天", "今年", "全部", "自定义"]


def resolve_range(option: str, custom_start: date, custom_end: date):
    """把时间范围选项换算成 (from, to) 两个 ISO 字符串；返回 None 表示不限。"""
    today = date.today()
    if option == "本月":
        return iso_at(today.replace(day=1)), iso_at(today, time(23, 59, 59))
    if option == "上月":
        first = today.replace(day=1)
        last_month_end = first - timedelta(days=1)
        return (
            iso_at(last_month_end.replace(day=1)),
            iso_at(last_month_end, time(23, 59, 59)),
        )
    if option == "近 30 天":
        return iso_at(today - timedelta(days=29)), iso_at(today, time(23, 59, 59))
    if option == "今年":
        return iso_at(today.replace(month=1, day=1)), iso_at(today, time(23, 59, 59))
    if option == "自定义":
        return iso_at(custom_start), iso_at(custom_end, time(23, 59, 59))
    return None, None


def month_bounds(month: str) -> tuple[str, str]:
    """把 YYYY-MM 换算成当月起止的 ISO 时间（东八区，含首尾）。"""
    year, mon = (int(part) for part in month.split("-"))
    start = date(year, mon, 1)
    end = date(year + (mon // 12), mon % 12 + 1, 1) - timedelta(days=1)
    return iso_at(start), iso_at(end, time(23, 59, 59))


def previous_period(option: str) -> tuple[str | None, str | None]:
    """上一个等长周期的起止时间，用于环比；不支持的选项返回 (None, None)。

    「本月」页面统计的是月初到当天，对比基期也截到同一天数（上月没有这一天时
    取月末），避免不足一个月和整月相减；「上月」是整月，对比前月整月。
    """
    today = date.today()
    last_month_end = today.replace(day=1) - timedelta(days=1)
    last_month_start = last_month_end.replace(day=1)
    if option == "本月":
        same_day = min(today.day, last_month_end.day)
        return iso_at(last_month_start), iso_at(
            last_month_end.replace(day=same_day), time(23, 59, 59)
        )
    if option == "上月":
        prev_month_end = last_month_start - timedelta(days=1)
        return (
            iso_at(prev_month_end.replace(day=1)),
            iso_at(prev_month_end, time(23, 59, 59)),
        )
    if option == "近 30 天":
        end = today - timedelta(days=30)
        start = end - timedelta(days=29)
        return iso_at(start), iso_at(end, time(23, 59, 59))
    return None, None


@st.cache_data(ttl=120, show_spinner=False)
def monthly_series(months: tuple[str, ...]) -> list[dict]:
    """近若干个月的支出合计，供趋势图使用。

    后端没有「按月汇总」的接口，这里按月各查一次 /stats/category；
    结果按 120 秒缓存，切换页面或拖动表单不会反复打后端。
    """
    rows: list[dict] = []
    for month in months:
        start, end = month_bounds(month)
        data = api_get("/stats/category", params={"from": start, "to": end})
        rows.append(
            {"月份": month[5:], "全月": month, "金额": to_yuan(data.get("total_cents", 0))}
        )
    return rows


def render_delete_transaction(items: list[dict]) -> None:
    """账单删除：先选一条，再二次确认，确认后调用后端 DELETE。"""
    if not items:
        return

    labels = {
        f"{fmt_local(item.get('occurred_at'))}  {item.get('merchant')}  "
        f"{fmt_money(item.get('amount_cents'))}（#{item['id']}）": item["id"]
        for item in items
    }
    choice = st.selectbox("选择要删除的账单", list(labels), key="delete_choice")

    if st.button("🗑️ 删除这笔账单", key="delete_request"):
        st.session_state["pending_delete_id"] = labels[choice]

    pending = st.session_state.get("pending_delete_id")
    if not pending:
        return

    st.warning("确认删除？删除后无法恢复。")
    col_yes, col_no = st.columns(2)
    if col_yes.button("确认删除", type="primary", key="delete_confirm"):
        try:
            api_delete(f"/transactions/{pending}")
        except ApiError as exc:
            st.error(f"删除失败：{exc.message}")
        else:
            st.session_state.pop("pending_delete_id", None)
            flash("账单已删除。")
            st.rerun()
    if col_no.button("取消", key="delete_cancel"):
        st.session_state.pop("pending_delete_id", None)
        st.rerun()


def _clear_pick_day() -> None:
    """回调：清空「按天查看」，恢复按时间范围筛选。"""
    st.session_state["stats_pick_day"] = None


def render_stats_page() -> None:
    """页面 3：账单明细 + 分类统计。"""
    st.header("📊 账单与统计")
    st.caption("按时间范围查看消费构成与明细，可以在这里删除账单。")

    col_range, col_start, col_end = st.columns([2, 1, 1])
    with col_range:
        option = st.selectbox("时间范围", RANGE_OPTIONS, key="stats_range")
    custom_start, custom_end = date.today().replace(day=1), date.today()
    if option == "自定义":
        with col_start:
            custom_start = st.date_input("开始日期", value=custom_start, key="stats_from")
        with col_end:
            custom_end = st.date_input("结束日期", value=custom_end, key="stats_to")

    date_from, date_to = resolve_range(option, custom_start, custom_end)

    # ---------- 类型筛选：全部 + 七个类别 ----------
    st.caption("类型筛选")
    selected = st.session_state.get("stats_category", "全部")
    filter_options = ["全部"] + CATEGORIES
    for start in (0, 4):
        row = filter_options[start : start + 4]
        cols = st.columns(len(row))
        for col, name in zip(cols, row):
            with col:
                label = "🗂 全部" if name == "全部" else f"{CATEGORY_ICONS.get(name, '📦')} {name}"
                if st.button(
                    label,
                    key=f"stats_cat_{name}",
                    type="primary" if name == selected else "secondary",
                    use_container_width=True,
                ):
                    st.session_state["stats_category"] = name
                    st.rerun()

    # ---------- 分类统计 ----------
    try:
        stats = api_get("/stats/category", params={"from": date_from, "to": date_to})
    except ApiError as exc:
        st.error(f"读取统计失败：{exc.message}")
        return

    total_cents = stats.get("total_cents", 0)
    all_total_cents = total_cents
    items = stats.get("items") or []
    top = max(items, key=lambda i: i["total_cents"]) if items else None
    count = sum(item["count"] for item in items)

    # 选中某个类别时，指标与明细都只看这一类；图表保留全量分布并高亮
    chosen = None
    if selected != "全部":
        chosen = next((item for item in items if item["category"] == selected), None)
        total_cents = chosen["total_cents"] if chosen else 0
        count = chosen["count"] if chosen else 0

    # 环比：只对能算出上一等长周期的选项计算，且只在「全部」类别时显示
    delta = None
    if selected == "全部":
        prev_from, prev_to = previous_period(option)
        if prev_from and prev_to:
            try:
                prev_total = api_get(
                    "/stats/category", params={"from": prev_from, "to": prev_to}
                ).get("total_cents", 0)
            except ApiError:
                prev_total = 0
            if prev_total:
                delta = f"{(total_cents - prev_total) / prev_total * 100:+.0f}% 环比"

    with st.container(border=True):
        col_1, col_2, col_3 = st.columns(3)
        col_1.metric(
            "总支出" if selected == "全部" else f"{selected}支出",
            fmt_money(total_cents),
            delta=delta,
            delta_color="inverse",
        )
        col_2.metric("笔数", f"{count} 笔")
        if selected == "全部":
            col_3.metric("类别数", f"{len(items)} 类")
        else:
            col_3.metric("占全部支出", f"{chosen['ratio']:.0%}" if chosen else "0%")

    # 最大类别单独一行说明，避免塞进指标卡被截断
    if selected == "全部" and top:
        st.caption(
            f"最大类别：**{top['category']}** {fmt_money(top['total_cents'])}"
            f"（占 {top['ratio']:.0%}）"
        )
    elif selected != "全部":
        st.caption(
            f"当前只看「{selected}」；全部类别合计 {fmt_money(all_total_cents)}，"
            "下方图表仍显示全部类别并高亮选中项。"
        )

    # 本月视图额外带上预算进度，和「预算管理」页呼应
    if option == "本月":
        try:
            overview = api_get("/budgets", params={"month": month_options(1)[0]})
        except ApiError:
            overview = None
        if overview:
            budget_total = next(
                (
                    row["amount_cents"]
                    for row in overview.get("items", [])
                    if row.get("category") is None
                ),
                None,
            )
            spent = overview.get("spent_cents", 0)
            if budget_total:
                ratio = spent / budget_total
                st.progress(
                    min(ratio, 1.0),
                    text=(
                        f"本月总预算 {fmt_money(budget_total)}，已用 {ratio:.0%}，"
                        f"剩余 {fmt_money(max(budget_total - spent, 0))}"
                    ),
                )

    if items:
        frame = pd.DataFrame(
            [
                {
                    "类别": item["category"],
                    "金额": to_yuan(item["total_cents"]),
                    "占比": item["ratio"],
                    "笔数": item["count"],
                }
                for item in items
            ]
        )
        color_scale = alt.Scale(domain=CATEGORIES, range=CATEGORY_COLORS)
        # 选中某个类别时把其余类别的图形变淡，保留整体分布
        opacity = (
            alt.value(1.0)
            if selected == "全部"
            else alt.condition(
                alt.datum.类别 == selected, alt.value(1.0), alt.value(0.22)
            )
        )
        col_bar, col_pie = st.columns(2)
        with col_bar:
            st.altair_chart(
                alt.Chart(frame)
                .mark_bar(cornerRadiusTopLeft=4, cornerRadiusTopRight=4)
                .encode(
                    x=alt.X("类别:N", sort="-y", title=None, axis=alt.Axis(labelAngle=0)),
                    y=alt.Y("金额:Q", title="金额（元）"),
                    color=alt.Color("类别:N", scale=color_scale, legend=None),
                    opacity=opacity,
                    tooltip=["类别", alt.Tooltip("金额:Q", format=",.2f"), "笔数"],
                ),
                width="stretch",
            )
        with col_pie:
            st.altair_chart(
                alt.Chart(frame)
                .mark_arc(innerRadius=50)
                .encode(
                    theta=alt.Theta("金额:Q"),
                    color=alt.Color(
                        "类别:N",
                        scale=color_scale,
                        legend=alt.Legend(title="类别", orient="right"),
                    ),
                    opacity=opacity,
                    tooltip=[
                        "类别",
                        alt.Tooltip("金额:Q", format=",.2f"),
                        alt.Tooltip("占比:Q", format=".1%"),
                    ],
                ),
                width="stretch",
            )
    else:
        empty_state("📭", "这个时间范围没有账单", "换个时间范围试试，或先去记一笔")

    # ---------- 每日对比（本月） ----------
    st.divider()
    st.subheader("📅 每日对比（本月）")
    month_start = date.today().replace(day=1)
    try:
        month_records = api_get(
            "/transactions",
            params={
                "from": iso_at(month_start),
                "to": iso_at(date.today(), time(23, 59, 59)),
                "category": None if selected == "全部" else selected,
                "limit": 100,
            },
        ).get("items") or []
    except ApiError as exc:
        st.error(f"读取每日数据失败：{exc.message}")
        month_records = []

    daily: dict[date, float] = {}
    for row in month_records:
        local = to_local(row.get("occurred_at"))
        if not local:
            continue
        daily[local.date()] = daily.get(local.date(), 0.0) + to_yuan(row.get("amount_cents"))

    if daily:
        today = date.today()
        # 补齐 1 号到今天的每一天，缺数据的日子显示 0，横轴按日期顺序
        all_days = [
            month_start + timedelta(days=offset)
            for offset in range((today - month_start).days + 1)
        ]
        daily_frame = pd.DataFrame(
            [
                {
                    "日期": f"{day.month:02d}-{day.day:02d}",
                    "金额": daily.get(day, 0.0),
                    "今天": day == today,
                }
                for day in all_days
            ]
        )
        daily_bars = (
            alt.Chart(daily_frame)
            .mark_bar(cornerRadiusTopLeft=3, cornerRadiusTopRight=3)
            .encode(
                x=alt.X(
                    "日期:N",
                    title=None,
                    axis=alt.Axis(labelAngle=0, labelOverlap=True),
                ),
                y=alt.Y("金额:Q", title="支出（元）"),
                color=alt.condition(
                    alt.datum.今天, alt.value("#4F46E5"), alt.value("#C7D2FE")
                ),
                tooltip=["日期", alt.Tooltip("金额:Q", format=",.2f")],
            )
        )
        # 只在有消费的柱子上标金额，避免 0 值标签挤满横轴
        daily_labels = (
            alt.Chart(daily_frame[daily_frame["金额"] > 0])
            .mark_text(dy=-6, fontSize=10, color="#6B7280")
            .encode(
                x=alt.X("日期:N"),
                y=alt.Y("金额:Q"),
                text=alt.Text("金额:Q", format=",.0f"),
            )
        )
        st.altair_chart(daily_bars + daily_labels, width="stretch")
        scope = "全部类别" if selected == "全部" else f"「{selected}」"
        st.caption(f"本月{scope}的每日支出，今天的柱子用深色标出。")
    else:
        empty_state("📅", "本月还没有账单", "记几笔之后这里会按天显示支出")

    # ---------- 月度对比 ----------
    st.divider()
    st.subheader("📆 月度对比（近 6 个月）")
    try:
        series = monthly_series(tuple(month_options(6)[::-1]))
    except ApiError as exc:
        st.error(f"读取月度数据失败：{exc.message}")
        series = []
    if series and any(row["金额"] for row in series):
        trend_frame = pd.DataFrame(series)
        current_month = month_options(1)[0][5:]
        bars = (
            alt.Chart(trend_frame)
            .mark_bar(cornerRadiusTopLeft=4, cornerRadiusTopRight=4)
            .encode(
                x=alt.X("月份:N", title=None, axis=alt.Axis(labelAngle=0)),
                y=alt.Y("金额:Q", title="支出（元）"),
                color=alt.condition(
                    alt.datum.月份 == current_month,
                    alt.value("#4F46E5"),
                    alt.value("#C7D2FE"),
                ),
                tooltip=[
                    alt.Tooltip("全月:N", title="月份"),
                    alt.Tooltip("金额:Q", format=",.2f"),
                ],
            )
        )
        labels = (
            alt.Chart(trend_frame)
            .mark_text(dy=-8, color="#6B7280", fontSize=11)
            .encode(x=alt.X("月份:N"), y=alt.Y("金额:Q"), text=alt.Text("金额:Q", format=",.0f"))
        )
        st.altair_chart(bars + labels, width="stretch")
        st.caption("本月用深色标出；月度合计为全部类别，不随类型筛选变化。")
    else:
        empty_state("📆", "还没有月度数据", "记几笔账之后，这里会按月对比支出")

    # ---------- 账单明细 ----------
    st.divider()
    st.subheader("🧾 账单明细")

    # 按天查看：用日历选一天，只看当天；点「全部」恢复时间范围
    col_day, col_clear = st.columns([4, 1])
    with col_day:
        pick_day = st.date_input(
            "按天查看",
            value=None,
            key="stats_pick_day",
            help="点开日历选一天，只看当天账单；点右侧「全部」恢复时间范围",
        )
    with col_clear:
        st.button(
            "全部",
            key="stats_clear_day",
            on_click=_clear_pick_day,
            use_container_width=True,
        )

    day_from, day_to = date_from, date_to
    if pick_day:
        day_from = iso_at(pick_day)
        day_to = iso_at(pick_day, time(23, 59, 59))

    try:
        listing = api_get(
            "/transactions",
            params={
                "from": day_from,
                "to": day_to,
                "category": None if selected == "全部" else selected,
                "limit": 100,
            },
        )
    except ApiError as exc:
        st.error(f"读取账单失败：{exc.message}")
        return

    records = listing.get("items") or []
    if records:
        st.dataframe(transactions_frame(records), width="stretch", hide_index=True)
        scope = "全部类别" if selected == "全部" else f"类别「{selected}」"
        if pick_day:
            scope = f"{scope} · {pick_day:%Y-%m-%d}"
        st.caption(
            f"{scope}：共 {listing.get('total', len(records))} 条，"
            f"当前展示最近 {len(records)} 条。"
        )
        st.download_button(
            "⬇️ 导出 CSV",
            export_csv(records),
            file_name=f"账单_{date.today():%Y%m%d}.csv",
            mime="text/csv",
            key="export_csv_button",
        )
        render_delete_transaction(records)
    else:
        empty_state("📒", "还没有账单", "先去「一句话记账」添加一笔")
