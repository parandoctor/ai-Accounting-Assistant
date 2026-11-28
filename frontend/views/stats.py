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


RANGE_OPTIONS = ["本月", "上月", "指定月份", "近 30 天", "今年", "全部", "自定义"]


def resolve_range(
    option: str, custom_start: date, custom_end: date, month: str | None = None
):
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
    if option == "指定月份" and month:
        return month_bounds(month)
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


def previous_period(
    option: str, month: str | None = None
) -> tuple[str | None, str | None]:
    """上一个等长周期的起止时间，用于环比；不支持的选项返回 (None, None)。

    「本月」页面统计的是月初到当天，对比基期也截到同一天数（上月没有这一天时
    取月末），避免不足一个月和整月相减；「上月」和「指定月份」都是整月，对比前月整月。
    """
    today = date.today()
    last_month_end = today.replace(day=1) - timedelta(days=1)
    last_month_start = last_month_end.replace(day=1)
    if option == "本月":
        same_day = min(today.day, last_month_end.day)
        return iso_at(last_month_start), iso_at(
            last_month_end.replace(day=same_day), time(23, 59, 59)
        )
    if option in ("上月", "指定月份") and (option == "上月" or month):
        if option == "上月" or not month:
            anchor = last_month_start
        else:
            year, mon = (int(part) for part in month.split("-"))
            anchor = date(year, mon, 1)
        prev_end = anchor - timedelta(days=1)
        return (
            iso_at(prev_end.replace(day=1)),
            iso_at(prev_end, time(23, 59, 59)),
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


def _use_month() -> None:
    """回调：改动月份时把时间范围切到「指定月份」。"""
    st.session_state["stats_range"] = "指定月份"


def _clear_pick_day() -> None:
    """回调：清空「按天查看」，恢复按时间范围筛选。"""
    st.session_state["stats_pick_day"] = None


def render_stats_page() -> None:
    """页面 3：账单明细 + 分类统计。"""
    st.header("📊 账单与统计")
    st.caption("按时间范围查看消费构成与明细，可以在这里删除账单。")

    col_range, col_month = st.columns([3, 2])
    with col_range:
        option = st.selectbox("时间范围", RANGE_OPTIONS, key="stats_range")
    with col_month:
        month_choice = st.selectbox(
            "月份",
            month_options(24),
            key="stats_month",
            on_change=_use_month,
            help="选一个月份会自动把时间范围切到「指定月份」",
        )
    custom_start, custom_end = date.today().replace(day=1), date.today()
    if option == "自定义":
        col_start, col_end = st.columns(2)
        with col_start:
            custom_start = st.date_input("开始日期", value=custom_start, key="stats_from")
        with col_end:
            custom_end = st.date_input("结束日期", value=custom_end, key="stats_to")

    if option != "指定月份":
        month_choice = None

    date_from, date_to = resolve_range(option, custom_start, custom_end, month_choice)

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
    count = sum(item["count"] for item in items)

    # 选中某个类别时，指标与明细都只看这一类；图表保留全量分布并高亮
    chosen = None
    if selected != "全部":
        chosen = next((item for item in items if item["category"] == selected), None)
        total_cents = chosen["total_cents"] if chosen else 0
        count = chosen["count"] if chosen else 0

    # 环比：只对能算出上一等长周期的选项计算，且只在「全部」类别时显示
    delta = None
    change_cents = None
    prev_total = None
    if selected == "全部":
        prev_from, prev_to = previous_period(option, month_choice)
        if prev_from and prev_to:
            try:
                prev_total = api_get(
                    "/stats/category", params={"from": prev_from, "to": prev_to}
                ).get("total_cents", 0)
            except ApiError:
                prev_total = None
            if prev_total:
                change_cents = total_cents - prev_total
                delta = f"{change_cents / prev_total * 100:+.0f}%"

    # ---------- 头部：共支出 + 环比（记账本风格） ----------
    label = "共支出" if selected == "全部" else f"「{selected}」支出"
    sub_parts = [f"{count} 笔", f"{len(items)} 类"]
    if delta and change_cents is not None:
        arrow = "↑" if change_cents > 0 else "↓"
        sub_parts.insert(0, f"较上期 {arrow} ¥{abs(change_cents) / 100:,.2f}（{delta}）")
    st.markdown(
        f"""
        <div style="background:linear-gradient(135deg,#4F46E5,#7C6BF0);border-radius:18px;
                    padding:22px 26px;color:#fff;box-shadow:0 8px 20px rgba(79,70,229,.22);">
          <div style="font-size:14px;opacity:.85;">{label}</div>
          <div style="font-size:42px;font-weight:700;line-height:1.15;margin-top:2px;">
            {fmt_money(total_cents)}
          </div>
          <div style="font-size:13px;opacity:.9;margin-top:10px;">{" · ".join(sub_parts)}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    if delta and prev_from and prev_to:
        st.caption(f"环比基期：{fmt_local(prev_from)} ~ {fmt_local(prev_to)}")
    elif selected != "全部":
        st.caption(
            f"全部类别合计 {fmt_money(all_total_cents)}，下方构成仍显示全部类别并高亮选中项。"
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

    st.subheader("🍩 支出构成")
    if items:
        frame = pd.DataFrame(
            [
                {
                    "类别": item["category"],
                    "金额": to_yuan(item["total_cents"]),
                    "占比": item["ratio"],
                    "笔数": item["count"],
                    "标签": f"{item['category']} {item['ratio']:.0%}",
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
        donut = (
            alt.Chart(frame)
            .mark_arc(innerRadius=88, outerRadius=145)
            .encode(
                theta=alt.Theta("金额:Q"),
                color=alt.Color("类别:N", scale=color_scale, legend=None),
                opacity=opacity,
                tooltip=[
                    "类别",
                    alt.Tooltip("金额:Q", format=",.2f"),
                    alt.Tooltip("占比:Q", format=".1%"),
                ],
            )
        )
        # 占比过小的类别不标文字，避免标签重叠
        ring_labels = (
            alt.Chart(frame[frame["占比"] >= 0.07])
            .mark_text(radius=170, fontSize=11, color="#4B5563")
            .encode(theta=alt.Theta("金额:Q"), text=alt.Text("标签:N"))
        )
        st.altair_chart(
            (donut + ring_labels).properties(height=420), width="stretch"
        )
        st.caption("占比小于 4% 的类别不标文字，鼠标悬停可看具体金额。")

        # 分类明细列表：图标 + 名称 + 进度条 + 金额（记账本风格）
        st.markdown("##### 分类明细")
        max_cents = max(item["total_cents"] for item in items) or 1
        for item in items:
            cat = item["category"]
            color = (
                CATEGORY_COLORS[CATEGORIES.index(cat)]
                if cat in CATEGORIES
                else "#9CA3AF"
            )
            icon = CATEGORY_ICONS.get(cat, "📦")
            width = max(item["total_cents"] / max_cents * 100, 2.0)
            st.markdown(
                f"""
                <div style="display:flex;align-items:center;gap:12px;padding:9px 0;
                            border-bottom:1px solid #EEF0F6;">
                  <div style="width:32px;height:32px;border-radius:50%;background:{color}1F;
                              display:flex;align-items:center;justify-content:center;
                              font-size:16px;">{icon}</div>
                  <div style="flex:1;">
                    <div style="display:flex;justify-content:space-between;font-size:13px;
                                color:#1F2430;">
                      <span>{cat}</span>
                      <span>{fmt_money(item['total_cents'])}
                        <span style="color:#8A90A2;margin-left:6px;">{item['ratio']:.1%}</span>
                      </span>
                    </div>
                    <div style="height:6px;background:#EEF1FA;border-radius:3px;margin-top:6px;">
                      <div style="width:{width:.1f}%;height:100%;background:{color};
                                  border-radius:3px;"></div>
                    </div>
                  </div>
                </div>
                """,
                unsafe_allow_html=True,
            )
    else:
        empty_state("📭", "这个时间范围没有账单", "换个时间范围试试，或先去记一笔")

    # ---------- 每日对比（本月 / 指定月份） ----------
    st.divider()
    if option == "指定月份" and month_choice:
        year, mon = (int(part) for part in month_choice.split("-"))
        daily_start = date(year, mon, 1)
        daily_end = date(year + (mon // 12), mon % 12 + 1, 1) - timedelta(days=1)
        daily_title = f"📅 每日对比（{month_choice}）"
    else:
        daily_start = date.today().replace(day=1)
        daily_end = date.today()
        daily_title = "📅 每日对比（本月）"
    st.subheader(daily_title)
    try:
        month_records = api_get(
            "/transactions",
            params={
                "from": iso_at(daily_start),
                "to": iso_at(daily_end, time(23, 59, 59)),
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
        # 补齐该月每一天（未来日期不补），缺数据的日子显示 0，横轴按日期顺序
        all_days = [
            daily_start + timedelta(days=offset)
            for offset in range((daily_end - daily_start).days + 1)
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
        st.caption(f"{daily_title[2:]}{scope}的每日支出，今天的柱子用深色标出。")
    else:
        empty_state("📅", "该月还没有账单", "记几笔之后这里会按天显示支出")

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
