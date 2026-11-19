"""AI 记账助手 —— Streamlit 前端。

页面结构：
    1. 记账：一句话 / 小票图片 → 后端解析出草稿 → 用户确认或修正后入库
    2. 查账：自然语言提问 → 后端解析类别与时间范围 → 直接返回查询结果
    3. 统计：按时间范围看分类占比、账单明细，支持删除账单
    4. 预算：按月设置总额 / 分类预算，展示已用比例与超支提醒

所有数据都经由后端 FastAPI 接口读写，前端不保存业务数据；
接口地址可用环境变量 API_URL 覆盖（默认 http://127.0.0.1:8000）。
"""

from __future__ import annotations

import os
from datetime import date, datetime, time, timedelta, timezone

import altair as alt
import pandas as pd
import requests
import streamlit as st

# ===================== 基础配置 =====================

# 后端地址；容器化部署时用环境变量覆盖
API_URL = os.getenv("API_URL", "http://127.0.0.1:8000").rstrip("/")

# 东八区：后端统一用 UTC 存储，前端展示与提交都按本地时区处理
TZ = timezone(timedelta(hours=8))

# 类别必须与后端 schemas/transaction.py 里的 Category 枚举保持一致
CATEGORIES = ["餐饮", "交通", "购物", "娱乐", "医疗", "居住", "其他"]
DEFAULT_CATEGORY = "其他"

# 图表配色：与 CATEGORIES 一一对应，柱状图与饼图共用同一套颜色
CATEGORY_COLORS = [
    "#F59E0B",  # 餐饮
    "#3B82F6",  # 交通
    "#EC4899",  # 购物
    "#8B5CF6",  # 娱乐
    "#10B981",  # 医疗
    "#6366F1",  # 居住
    "#9CA3AF",  # 其他
]

PAGES = {
    "record": "📝 一句话记账",
    "query": "🔍 自然语言查账",
    "stats": "📊 账单与统计",
    "budget": "🎯 预算管理",
}

# 预设的示例问题，点击后直接发起查询
QUICK_QUESTIONS = [
    "这个月吃饭花了多少钱",
    "这个月哪类消费最多",
    "最近有什么账单",
]

# 表单默认值：解析出草稿或保存成功后重置用
FORM_DEFAULTS = {
    "form_money": 0.0,
    "form_merchant": "",
    "form_category": DEFAULT_CATEGORY,
    "form_date": date.today(),
    "form_note": "",
}


# ===================== 接口访问层 =====================


class ApiError(Exception):
    """接口调用失败。

    status 为 0 表示请求没发出去（后端未启动 / 网络不通）；
    其余为后端返回的 HTTP 状态码。
    """

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _request(method: str, path: str, **kwargs):
    """统一的请求封装：拼接地址、处理超时、把错误响应转成 ApiError。

    返回解析后的 JSON；204 无响应体时返回 None。
    """
    timeout = kwargs.pop("timeout", 15)
    try:
        resp = requests.request(method, API_URL + path, timeout=timeout, **kwargs)
    except requests.RequestException as exc:
        raise ApiError(0, f"连不上后端（{API_URL}），请确认服务已启动：{exc}") from exc

    if resp.status_code == 204:
        return None

    try:
        body = resp.json()
    except ValueError:
        body = None

    if resp.status_code >= 400:
        # 后端错误结构统一为 {code, message, detail}
        message = (body or {}).get("message") if isinstance(body, dict) else None
        raise ApiError(resp.status_code, message or resp.text[:200] or "请求失败")
    return body


def api_get(path: str, params: dict | None = None):
    """GET 请求，params 里为空的键会被忽略。"""
    clean = {k: v for k, v in (params or {}).items() if v not in (None, "")}
    return _request("GET", path, params=clean)


def api_post(path: str, json: dict | None = None, files: dict | None = None):
    """POST 请求，json 与 files 二选一使用。"""
    if files is not None:
        return _request("POST", path, files=files)
    return _request("POST", path, json=json)


def api_put(path: str, json: dict):
    """PUT 请求。"""
    return _request("PUT", path, json=json)


def api_delete(path: str):
    """DELETE 请求，成功时后端返回 204。"""
    return _request("DELETE", path)


# ===================== 格式化与时间工具 =====================


def to_cents(yuan: float) -> int:
    """元 → 分，四舍五入成整数。"""
    return int(round(yuan * 100))


def to_yuan(cents: int | None) -> float:
    """分 → 元。"""
    return (cents or 0) / 100


def fmt_money(cents: int | None) -> str:
    """格式化金额，带千分位。"""
    return f"¥{to_yuan(cents):,.2f}"


def parse_iso(value: str | None) -> datetime | None:
    """解析后端返回的 ISO 时间；失败返回 None。"""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def to_local(value: str | None) -> datetime | None:
    """UTC 时间字符串 → 东八区 datetime。"""
    parsed = parse_iso(value)
    return parsed.astimezone(TZ) if parsed else None


def fmt_local(value: str | None, pattern: str = "%Y-%m-%d") -> str:
    """UTC 时间字符串 → 本地日期文本，缺失时显示占位符。"""
    local = to_local(value)
    return local.strftime(pattern) if local else "-"


def iso_at(day: date, at: time = time(0, 0)) -> str:
    """把「日期 + 时刻」转成带时区的 ISO 字符串，供接口的 from / to 使用。"""
    return datetime.combine(day, at).replace(tzinfo=TZ).isoformat()


def month_options(count: int = 12) -> list[str]:
    """生成最近 count 个月的 YYYY-MM 列表，当月在最前。"""
    today = date.today()
    total = today.year * 12 + (today.month - 1)
    return [f"{(total - i) // 12:04d}-{(total - i) % 12 + 1:02d}" for i in range(count)]


def flash(message: str, level: str = "success") -> None:
    """记录一条提示，下一次渲染时显示（配合 st.rerun 使用）。"""
    st.session_state["flash"] = (level, message)


def show_flash() -> None:
    """展示并清除待显示的提示。"""
    payload = st.session_state.pop("flash", None)
    if not payload:
        return
    level, message = payload
    {"success": st.success, "warning": st.warning, "error": st.error}.get(
        level, st.info
    )(message)


def reset_form(apply_draft: bool = False) -> None:
    """把预览表单的字段恢复成默认值。

    必须在表单控件创建之前调用；控件已经渲染后再改 session_state 会抛
    StreamlitWidgetAlreadyInstantiatedError。
    """
    for key, value in FORM_DEFAULTS.items():
        st.session_state[key] = value
    if not apply_draft:
        st.session_state["draft_datetime"] = None


def queue_form_reset() -> None:
    """标记「下次渲染前重置表单」，供保存成功后使用。"""
    st.session_state["pending_form_reset"] = True


# ===================== 页面：记账 =====================


def apply_draft(draft: dict) -> None:
    """把后端返回的草稿转换成预览表单的初始值。

    后端草稿里的字段可能为 null（未识别），这里统一兜底：
    金额 0、商户空、类别「其他」、日期取草稿时间或今天。
    """
    occurred = to_local(draft.get("occurred_at"))
    st.session_state["draft"] = draft
    st.session_state["draft_datetime"] = occurred
    st.session_state["form_money"] = to_yuan(draft.get("amount_cents"))
    st.session_state["form_merchant"] = draft.get("merchant") or ""
    st.session_state["form_note"] = draft.get("raw_text") or ""
    st.session_state["form_date"] = occurred.date() if occurred else date.today()
    category = draft.get("category")
    st.session_state["form_category"] = (
        category if category in CATEGORIES else DEFAULT_CATEGORY
    )


def handle_parse_text(text: str) -> None:
    """调用 /transactions/parse 解析一句话描述。"""
    if not text.strip():
        st.warning("请先输入消费描述，例如「午饭 25」。")
        return
    try:
        draft = api_post("/transactions/parse", json={"text": text.strip()})
    except ApiError as exc:
        st.error(f"文字识别失败：{exc.message}")
        return
    apply_draft(draft)


def handle_parse_receipt(image) -> None:
    """调用 /receipts/parse 识别小票或支付截图。"""
    if image is None:
        st.warning("请先选择一张小票或支付截图。")
        return
    mime = image.type or "image/png"
    try:
        draft = api_post(
            "/receipts/parse",
            files={"file": (image.name, image.getvalue(), mime)},
        )
    except ApiError as exc:
        st.error(f"小票识别失败：{exc.message}")
        return
    apply_draft(draft)


def render_draft_preview(draft: dict) -> None:
    """渲染「解析结果 → 用户确认」的表单，保存时写入后端。

    草稿里未识别的字段在标签上标注，提示用户补充。
    """
    missing = set(draft.get("missing_fields") or [])
    engine = draft.get("engine")
    st.divider()
    st.subheader("📝 解析结果（可修改后保存）")

    if engine == "none":
        st.warning("未启用视觉模型，这次没识别出内容，请手动填写下面的字段。")
    elif engine == "rule":
        st.caption("当前由规则解析（未配置大模型），识别结果可能有偏差，请核对。")

    with st.form("save_transaction_form"):
        col_a, col_b = st.columns(2)
        with col_a:
            st.number_input(
                "金额（元）", min_value=0.0, step=0.5, format="%.2f", key="form_money"
            )
            st.text_input(
                "商户（必填）" + ("　⚠️ 未识别" if "merchant" in missing else ""),
                key="form_merchant",
            )
        with col_b:
            st.selectbox("消费类别", CATEGORIES, key="form_category")
            st.date_input("消费日期", key="form_date")
        st.text_input("备注", key="form_note")
        submitted = st.form_submit_button("💾 保存账单", type="primary")

    if not submitted:
        return

    amount = st.session_state["form_money"]
    if amount <= 0:
        st.error("金额必须大于 0。")
        return

    # 商户是后端必填字段；识别不出来时用类别名兜底，避免卡住保存
    category = st.session_state["form_category"]
    merchant = st.session_state["form_merchant"].strip() or category

    # 时间：优先保留解析出的时刻，日期用用户确认后的值，统一按东八区提交
    base = st.session_state.get("draft_datetime") or datetime.now(TZ)
    occurred = datetime.combine(
        st.session_state["form_date"], base.replace(tzinfo=None).time()
    ).replace(tzinfo=TZ)

    payload = {
        "draft_id": draft.get("draft_id"),
        "amount_cents": to_cents(amount),
        "merchant": merchant,
        "occurred_at": occurred.isoformat(),
        "category": category,
        "note": st.session_state["form_note"].strip() or None,
    }
    try:
        api_post("/transactions", json=payload)
    except ApiError as exc:
        st.error(f"保存失败：{exc.message}")
        return

    st.session_state.pop("draft", None)
    queue_form_reset()
    flash("保存成功，已写入数据库。")
    st.rerun()


def render_record_page() -> None:
    """页面 1：一句话记账 / 小票识别。"""
    # 保存成功后的下一次渲染：先把表单字段复位，再创建控件
    if st.session_state.pop("pending_form_reset", False):
        reset_form()

    st.header("📝 AI 智能记账")
    st.caption("输入一句消费描述，或上传小票、支付截图；识别结果确认后再入库。")

    col_text, col_image = st.columns([3, 2])
    with col_text:
        text = st.text_input(
            "消费描述", placeholder="午饭 25，和同事 AA", key="record_text"
        )
        if st.button("🔍 识别文字", type="primary", key="parse_text_button"):
            handle_parse_text(text)
    with col_image:
        image = st.file_uploader(
            "小票 / 截图", type=["png", "jpg", "jpeg"], key="receipt_uploader"
        )
        if st.button("📷 识别小票", key="parse_receipt_button"):
            handle_parse_receipt(image)

    draft = st.session_state.get("draft")
    if draft:
        render_draft_preview(draft)
    else:
        st.divider()
        st.info("识别结果会显示在这里：金额、商户、类别、日期都可以手动改。")


# ===================== 页面：查账 =====================


def run_query(question: str) -> None:
    """调用 /query/ask：后端解析问题条件并直接返回查询结果。"""
    if not question.strip():
        st.warning("请先输入问题。")
        return
    try:
        answer = api_post("/query/ask", json={"question": question.strip()})
    except ApiError as exc:
        st.error(f"查询失败：{exc.message}")
        return
    st.session_state["query_result"] = answer


def _fill_question(question: str) -> None:
    """快捷问题按钮的回调。

    on_click 回调在脚本重新执行、控件创建之前运行，此时写 session_state 才合法；
    直接在按钮的 if 分支里写会触发 StreamlitWidgetAlreadyInstantiatedError。
    """
    st.session_state["query_text"] = question
    st.session_state["query_pending"] = question


def render_query_page() -> None:
    """页面 2：自然语言查账。"""
    st.header("🔍 自然语言查账")
    st.caption("用一句话提问，系统解析类别与时间范围后直接查库，而不是让模型自由发挥。")

    st.text_input(
        "你的问题",
        placeholder="我上个月吃饭花了多少？",
        key="query_text",
    )
    cols = st.columns(len(QUICK_QUESTIONS))
    for index, question in enumerate(QUICK_QUESTIONS):
        cols[index].button(
            question,
            key=f"quick_question_{index}",
            on_click=_fill_question,
            args=(question,),
        )

    # 快捷问题点击后待执行的查询
    pending = st.session_state.pop("query_pending", None)
    if pending:
        run_query(pending)

    if st.button("🔎 查询", type="primary", key="run_query_button"):
        run_query(st.session_state.get("query_text", ""))

    answer = st.session_state.get("query_result")
    st.divider()
    if not answer:
        st.info("结果会显示在这里：结论、合计金额、命中笔数，以及明细列表。")
        return

    st.subheader("结论")
    st.markdown(f"### {answer.get('answer', '-')}")

    col_1, col_2, col_3, col_4 = st.columns(4)
    col_1.metric("合计金额", fmt_money(answer.get("total_cents")))
    col_2.metric("命中笔数", f"{answer.get('count', 0)} 笔")
    col_3.metric("类别", answer.get("category") or "全部")
    period = "-"
    if answer.get("date_from") or answer.get("date_to"):
        start_dt = to_local(answer.get("date_from")) if answer.get("date_from") else None
        end_dt = to_local(answer.get("date_to")) if answer.get("date_to") else None
        if start_dt and end_dt:
            # 同一年只显示 月-日，避免指标卡里被截断
            fmt = "%m-%d" if start_dt.year == end_dt.year else "%Y-%m-%d"
            period = f"{start_dt.strftime(fmt)} ~ {end_dt.strftime(fmt)}"
        else:
            period = (start_dt or end_dt).strftime("%Y-%m-%d")
    col_4.metric("时间范围", period)

    engine = answer.get("engine")
    st.caption(
        "解析方式：大模型" if engine == "llm" else "解析方式：规则兜底（未配置大模型时）"
    )

    items = answer.get("items") or []
    if items:
        st.subheader("明细")
        st.dataframe(transactions_frame(items), use_container_width=True, hide_index=True)


# ===================== 页面：统计 =====================

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


def transactions_frame(items: list[dict]) -> pd.DataFrame:
    """把后端账目列表转成展示用的表格。"""
    if not items:
        return pd.DataFrame(columns=["日期", "商户", "类别", "金额", "备注"])
    return pd.DataFrame(
        [
            {
                "日期": fmt_local(item.get("occurred_at"), "%Y-%m-%d %H:%M"),
                "商户": item.get("merchant") or "-",
                "类别": item.get("category") or "-",
                "金额": fmt_money(item.get("amount_cents")),
                "备注": item.get("note") or "",
            }
            for item in items
        ]
    )


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

    # ---------- 分类统计 ----------
    try:
        stats = api_get("/stats/category", params={"from": date_from, "to": date_to})
    except ApiError as exc:
        st.error(f"读取统计失败：{exc.message}")
        return

    total_cents = stats.get("total_cents", 0)
    items = stats.get("items") or []
    with st.container(border=True):
        col_1, col_2, col_3 = st.columns(3)
        col_1.metric("总支出", fmt_money(total_cents))
        col_2.metric("类别数", f"{len(items)} 类")
        top = max(items, key=lambda i: i["total_cents"]) if items else None
        col_3.metric("最大类别", f"{top['category']} {fmt_money(top['total_cents'])}" if top else "-")

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
        col_bar, col_pie = st.columns(2)
        with col_bar:
            st.altair_chart(
                alt.Chart(frame)
                .mark_bar(cornerRadiusTopLeft=4, cornerRadiusTopRight=4)
                .encode(
                    x=alt.X("类别:N", sort="-y", title=None, axis=alt.Axis(labelAngle=0)),
                    y=alt.Y("金额:Q", title="金额（元）"),
                    color=alt.Color("类别:N", scale=color_scale, legend=None),
                    tooltip=["类别", alt.Tooltip("金额:Q", format=",.2f"), "笔数"],
                ),
                use_container_width=True,
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
                    tooltip=[
                        "类别",
                        alt.Tooltip("金额:Q", format=",.2f"),
                        alt.Tooltip("占比:Q", format=".1%"),
                    ],
                ),
                use_container_width=True,
            )
    else:
        st.info("这个时间范围内还没有账单。")

    # ---------- 账单明细 ----------
    st.divider()
    st.subheader("🧾 账单明细")
    try:
        listing = api_get(
            "/transactions",
            params={"from": date_from, "to": date_to, "limit": 100},
        )
    except ApiError as exc:
        st.error(f"读取账单失败：{exc.message}")
        return

    records = listing.get("items") or []
    if records:
        st.dataframe(transactions_frame(records), use_container_width=True, hide_index=True)
        st.caption(f"共 {listing.get('total', len(records))} 条，当前展示最近 {len(records)} 条。")
        render_delete_transaction(records)
    else:
        st.info("还没有账单，先去「一句话记账」添加一笔。")


# ===================== 页面：预算 =====================


def render_budget_row(item: dict) -> None:
    """单条预算：类别、已用 / 预算、进度条、状态与删除按钮。"""
    name = item.get("category") or "整月总预算"
    ratio = float(item.get("ratio") or 0)
    status = item.get("status")
    spent, amount = item.get("spent_cents", 0), item.get("amount_cents", 0)
    remain = amount - spent

    with st.container(border=True):
        head, action = st.columns([5, 1])
        with head:
            st.markdown(f"**{name}**　{fmt_money(spent)} / {fmt_money(amount)}")
        with action:
            if st.button("删除", key=f"budget_delete_{item['id']}"):
                st.session_state["pending_budget_id"] = item["id"]

        st.progress(min(ratio, 1.0))

        if status == "exceeded":
            st.error(f"已超支 {fmt_money(-remain)}（{ratio:.0%}）")
        elif status == "warning":
            st.warning(f"接近上限，已用 {ratio:.0%}，剩余 {fmt_money(remain)}")
        else:
            st.caption(f"已用 {ratio:.0%}，剩余 {fmt_money(remain)}")

        if st.session_state.get("pending_budget_id") == item["id"]:
            st.warning("确认删除这条预算？")
            yes, no = st.columns(2)
            if yes.button("确认删除", type="primary", key=f"budget_confirm_{item['id']}"):
                try:
                    api_delete(f"/budgets/{item['id']}")
                except ApiError as exc:
                    st.error(f"删除失败：{exc.message}")
                else:
                    st.session_state.pop("pending_budget_id", None)
                    flash("预算已删除。")
                    st.rerun()
            if no.button("取消", key=f"budget_cancel_{item['id']}"):
                st.session_state.pop("pending_budget_id", None)
                st.rerun()


def render_budget_page() -> None:
    """页面 4：预算设置与超支提醒。"""
    st.header("🎯 预算管理")
    st.caption("按月份设置总额或分类预算，接近上限和超支时给出提醒。")

    month = st.selectbox("月份", month_options(), key="budget_month")

    try:
        overview = api_get("/budgets", params={"month": month})
    except ApiError as exc:
        st.error(f"读取预算失败：{exc.message}")
        return

    items = overview.get("items") or []
    spent = overview.get("spent_cents", 0)
    total_budget = next(
        (item["amount_cents"] for item in items if item.get("category") is None), None
    )

    col_1, col_2, col_3 = st.columns(3)
    col_1.metric("本月支出", fmt_money(spent))
    col_2.metric("本月总预算", fmt_money(total_budget) if total_budget else "未设置")
    if total_budget:
        remain = total_budget - spent
        col_3.metric("剩余" if remain >= 0 else "已超支", fmt_money(abs(remain)))
    else:
        col_3.metric("剩余", "-")

    st.divider()
    st.subheader("预算使用情况")
    if items:
        for item in items:
            render_budget_row(item)
    else:
        st.info(f"{month} 还没有设置预算，先在下面添加一条。")

    st.divider()
    st.subheader("设置预算")
    st.caption("同一月份、同一类别重复设置会覆盖原金额；「整月总预算」不分类别。")
    with st.form("budget_form"):
        col_cat, col_amount = st.columns(2)
        with col_cat:
            target = st.selectbox("预算类别", ["整月总预算"] + CATEGORIES)
        with col_amount:
            amount = st.number_input(
                "预算金额（元）", min_value=1.0, step=100.0, value=1000.0, format="%.2f"
            )
        saved = st.form_submit_button("保存预算", type="primary")

    if saved:
        payload = {
            "month": month,
            "category": None if target == "整月总预算" else target,
            "amount_cents": to_cents(amount),
        }
        try:
            api_put("/budgets", json=payload)
        except ApiError as exc:
            st.error(f"保存失败：{exc.message}")
        else:
            flash(f"{month} 的「{target}」已保存。")
            st.rerun()


# ===================== 应用入口 =====================


@st.cache_data(ttl=20, show_spinner=False)
def backend_online() -> bool:
    """探测后端是否在线，结果缓存 20 秒避免每步都请求。"""
    try:
        requests.get(API_URL + "/health", timeout=3)
    except requests.RequestException:
        return False
    return True


def main() -> None:
    """应用入口：侧边栏导航 + 页面分发。"""
    st.set_page_config(page_title="AI 记账助手", page_icon="📒", layout="wide")

    # 支持通过 ?page=budget 直接打开某个页面（演示、截图、分享都方便）
    default_key = st.query_params.get("page", "record")
    keys = list(PAGES)
    index = keys.index(default_key) if default_key in keys else 0

    st.sidebar.title("📒 AI 记账助手")
    selected_label = st.sidebar.radio("功能菜单", list(PAGES.values()), index=index)
    selected_key = keys[list(PAGES.values()).index(selected_label)]

    st.sidebar.divider()
    st.sidebar.caption("记账 · 查账 · 统计 · 预算")
    if backend_online():
        st.sidebar.success("后端已连接")
    else:
        st.sidebar.error("后端未连接")
        st.sidebar.caption(f"请确认 {API_URL} 正在运行")

    show_flash()
    {
        "record": render_record_page,
        "query": render_query_page,
        "stats": render_stats_page,
        "budget": render_budget_page,
    }[selected_key]()


if __name__ == "__main__":
    main()
