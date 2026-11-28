"""页面：一句话记账 / 小票识别，含草稿预览与保存。"""

from __future__ import annotations

import re
from datetime import date, datetime

import streamlit as st

from config import CATEGORIES, CATEGORY_ICONS, DEFAULT_CATEGORY, TZ
from api_client import ApiError, api_post
from formatting import to_cents, to_local, to_yuan
from components import empty_state, flash


# 预览表单的字段名；实际控件 key 会拼上草稿 id 后缀，见 form_keys()
FORM_FIELDS = (
    "form_money",
    "form_total",
    "form_split",
    "form_merchant",
    "form_category",
    "form_date",
    "form_note",
)

# 单笔金额上限（元）。数据库是 DECIMAL(10,2)，上限约 9999 万；
# 这里收得更紧一些，避免用户误输入天文数字导致后端写入失败。
MAX_AMOUNT_YUAN = 1_000_000.0

# 识别到这些字样时默认按 AA 分摊（人数 2）
_AA_PATTERN = re.compile(r"(?i)\baa\b|平摊|对半|各付")


def form_keys(draft: dict) -> dict[str, str]:
    """给每份草稿生成一套独立的控件 key。

    用草稿 id 做后缀：同一份草稿里用户改的值会保留（校验失败后可以接着改），
    换一份新草稿时整组控件重建，从根上避免「上一轮的旧值残留在表单里」。
    """
    suffix = (draft.get("draft_id") or "pending")[:8]
    return {name: f"{name}_{suffix}" for name in FORM_FIELDS}



def apply_draft(draft: dict) -> None:
    """把后端返回的草稿转换成预览表单的初始值。

    后端草稿里的字段可能为 null（未识别），这里统一兜底：
    金额 0、商户空、类别「其他」、日期取草稿时间或今天。
    """
    keys = form_keys(draft)
    occurred = to_local(draft.get("occurred_at"))
    total = to_yuan(draft.get("amount_cents"))
    raw_text = (draft.get("raw_text") or "").strip()
    # 识别到「AA / 平摊」时默认按 2 人分摊，金额先算成人均
    split = 2 if _AA_PATTERN.search(raw_text) else 1

    st.session_state["draft"] = draft
    st.session_state["draft_datetime"] = occurred
    st.session_state[keys["form_total"]] = total
    st.session_state[keys["form_split"]] = split
    st.session_state[keys["form_money"]] = round(total / split, 2)
    st.session_state[keys["form_merchant"]] = draft.get("merchant") or ""
    # 备注上限 200 字：一句话记账的原文就是一句备注，可以直接预填；
    # 小票 OCR 原文可能上千字，超过上限就不塞进备注，改在预览区以原文形式展示。
    st.session_state[keys["form_note"]] = raw_text if len(raw_text) <= 200 else ""
    st.session_state[keys["form_date"]] = (
        occurred.date() if occurred else date.today()
    )
    category = draft.get("category")
    st.session_state[keys["form_category"]] = (
        category if category in CATEGORIES else DEFAULT_CATEGORY
    )


def recalc_split(keys: dict[str, str]) -> None:
    """AA 人数变化时，用「识别总额 ÷ 人数」重算人均金额。"""
    total = float(st.session_state.get(keys["form_total"], 0.0) or 0.0)
    split = int(st.session_state.get(keys["form_split"], 1) or 1)
    st.session_state[keys["form_money"]] = round(total / split, 2)


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
        st.warning(
            "后端未配置视觉模型（AI_VISION_MODEL / AI_VISION_API_KEY），图片识别暂不可用；"
            "可以先手动填写下面的字段，或让管理员配置后重试。"
        )
    elif engine == "rule":
        st.caption("当前由规则解析（未配置大模型），识别结果可能有偏差，请核对。")

    if "amount_cents" in missing:
        st.warning("没有识别到金额，请手动填写；金额为 0 时不能保存。")

    # 原文超过备注上限时单独展开显示，保证小票识别出的文字不会丢。
    raw_text = (draft.get("raw_text") or "").strip()
    if raw_text and len(raw_text) > 200:
        with st.expander("识别原文"):
            st.text(raw_text)

    keys = form_keys(draft)

    # ---------- 金额：大字输入 ----------
    st.caption("金额（元）")
    st.number_input(
        "金额（元）",
        min_value=0.0,
        max_value=MAX_AMOUNT_YUAN,
        step=0.5,
        format="%.2f",
        key=keys["form_money"],
        label_visibility="collapsed",
        help="识别出的金额，可以直接修改",
    )

    # ---------- AA 分摊 ----------
    col_split, col_hint = st.columns([1, 2])
    with col_split:
        st.number_input(
            "AA 分摊人数",
            min_value=1,
            max_value=999,
            step=1,
            key=keys["form_split"],
            on_change=recalc_split,
            args=(keys,),
            help="默认 1（不分摊）；识别到 AA / 平摊时自动设为 2，最多 999 人",
        )
    with col_hint:
        total = float(st.session_state.get(keys["form_total"], 0.0) or 0.0)
        split = int(st.session_state.get(keys["form_split"], 1) or 1)
        if split > 1 and total > 0:
            st.caption(
                f"识别总额 ¥{total:,.2f} ÷ {split} 人 = ¥{total / split:,.2f}"
            )
        else:
            st.caption("识别到「AA / 平摊」会自动按人数分摊，也可以手动改人数")

    # ---------- 类别：图标网格 ----------
    st.caption("消费类别")
    selected_category = st.session_state[keys["form_category"]]
    for row in (CATEGORIES[:4], CATEGORIES[4:]):
        cols = st.columns(len(row))
        for col, cat in zip(cols, row):
            with col:
                if st.button(
                    f"{CATEGORY_ICONS.get(cat, '📦')} {cat}",
                    key=f"cat_{keys['form_money']}_{cat}",
                    type="primary" if cat == selected_category else "secondary",
                    use_container_width=True,
                ):
                    st.session_state[keys["form_category"]] = cat
                    st.rerun()

    # ---------- 日期 / 商户 / 备注 ----------
    col_date, col_merchant = st.columns(2)
    with col_date:
        st.date_input(
            "消费日期" + ("　⚠️ 未识别" if "occurred_at" in missing else ""),
            key=keys["form_date"],
            max_value=date.today(),
            help="不允许选择未来日期",
        )
    with col_merchant:
        st.text_input(
            "商户（选填）",
            key=keys["form_merchant"],
            max_chars=30,
            placeholder="可留空，自动记为「未填写」",
        )
    st.text_input("备注", key=keys["form_note"], max_chars=200)

    submitted = st.button("💾 保存账单", type="primary", use_container_width=True)

    if not submitted:
        return

    amount = float(st.session_state[keys["form_money"]])
    if amount <= 0:
        st.error("金额必须大于 0；如果是识别失败，请手动填写实际金额。")
        return

    # 商户选填：留空时记为「未填写」，既不打断记账，也不产生空字符串
    category = st.session_state[keys["form_category"]]
    merchant = str(st.session_state[keys["form_merchant"]]).strip() or "未填写"

    # 时间：优先保留解析出的时刻，日期用用户确认后的值，统一按东八区提交
    base = st.session_state.get("draft_datetime") or datetime.now(TZ)
    occurred = datetime.combine(
        st.session_state[keys["form_date"]], base.replace(tzinfo=None).time()
    ).replace(tzinfo=TZ)

    payload = {
        "draft_id": draft.get("draft_id"),
        "amount_cents": to_cents(amount),
        "merchant": merchant,
        "occurred_at": occurred.isoformat(),
        "category": category,
        "note": st.session_state[keys["form_note"]].strip() or None,
    }
    try:
        api_post("/transactions", json=payload)
    except ApiError as exc:
        st.error(f"保存失败：{exc.message}")
        return

    st.session_state.pop("draft", None)
    flash("保存成功，已写入数据库。")
    st.rerun()


def render_record_page() -> None:
    """页面 1：一句话记账 / 小票识别。"""
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
        empty_state("🧾", "还没有解析结果", "输入一句消费描述或上传小票，识别后会显示在这里")
