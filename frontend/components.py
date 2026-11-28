"""通用界面组件：全局样式、空状态、预算进度条与轻提示。"""

from __future__ import annotations

import streamlit as st



APP_CSS = """
<style>
/* 顶部工具栏：只隐藏 Deploy / 主菜单 / 运行状态，保留「展开侧边栏」按钮 */
[data-testid="stToolbar"] {background: transparent;}
[data-testid="stToolbar"] [data-testid="stBaseButton-header"],
[data-testid="stToolbar"] [data-testid="stMainMenuButton"],
[data-testid="stToolbar"] [data-testid="stStatusWidget"] {display: none;}
/* 侧边栏折叠后，展开按钮不能跟着被藏掉 */
[data-testid="stExpandSidebarButton"],
[data-testid="stSidebarCollapsedControl"] {
  display: inline-flex !important;
  visibility: visible !important;
  opacity: 1 !important;
}
footer {visibility: hidden;}

/* 内容区留白：宽屏下不让正文铺满，读起来更舒服 */
.block-container {
  padding-top: 2.6rem;
  padding-bottom: 3rem;
  max-width: 1180px;
}

/* 侧边栏：白底 + 细分隔线，和主内容区分开 */
section[data-testid="stSidebar"] {
  background: #FFFFFF;
  border-right: 1px solid #E6E8F0;
}
section[data-testid="stSidebar"] .block-container {padding-top: 1.2rem;}

/* 侧边栏导航：把单选组做成列表项的样子 */
section[data-testid="stSidebar"] div[role="radiogroup"] label {
  padding: 7px 10px;
  border-radius: 10px;
  transition: background 0.15s ease;
}
section[data-testid="stSidebar"] div[role="radiogroup"] label:hover {
  background: #EEF1FA;
}

/* 卡片：st.container(border=True) 用白底 + 圆角，和浅灰页面背景拉开层次 */
div[data-testid="stVerticalBlockBorderWrapper"] > div {
  background: #FFFFFF;
  border-radius: 14px;
}

/* 指标卡：白底、圆角、极浅阴影 */
div[data-testid="stMetric"] {
  background: #FFFFFF;
  border: 1px solid #E6E8F0;
  border-radius: 14px;
  padding: 14px 18px;
  box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04);
}

/* 按钮与下载按钮：统一圆角与字重 */
.stButton > button, .stDownloadButton > button {
  border-radius: 10px;
  font-weight: 500;
}

/* 进度条与提示块圆角，和卡片保持一致 */
div[data-testid="stProgress"] > div > div > div {border-radius: 6px;}
div[data-testid="stAlert"] {border-radius: 12px;}

/* 表格外框圆角，避免直角边框显得生硬 */
div[data-testid="stDataFrame"] {
  border: 1px solid #E6E8F0;
  border-radius: 12px;
  overflow: hidden;
}
</style>
"""


def inject_styles() -> None:
    """注入全局样式；每次渲染调用一次即可。"""
    st.markdown(APP_CSS, unsafe_allow_html=True)


def empty_state(icon: str, title: str, hint: str) -> None:
    """统一的空状态占位：图标 + 标题 + 一句引导。

    比 st.info 更醒目，也避免每个页面各写一套文案。
    """
    st.markdown(
        f"""
        <div style="text-align:center;padding:34px 16px;border:1px dashed #D7DBE7;
                    border-radius:14px;background:#FBFCFE;">
          <div style="font-size:30px;line-height:1.2">{icon}</div>
          <div style="font-weight:600;margin-top:8px;color:#1F2430">{title}</div>
          <div style="color:#7A8299;font-size:13px;margin-top:4px">{hint}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


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


def budget_bar(ratio: float, status: str | None) -> None:
    """按状态着色的预算进度条：正常主色、接近上限黄色、超支红色。

    Streamlit 自带的 st.progress 只能统一配色，这里用一段 HTML 实现，
    让超支/接近上限在视觉上更直观。
    """
    color = {"exceeded": "#EF4444", "warning": "#F59E0B"}.get(status or "", "#4F46E5")
    width = max(0.0, min(ratio, 1.0)) * 100
    st.markdown(
        f"""
        <div style="background:#EEF1FA;border-radius:6px;height:9px;overflow:hidden;
                    margin:2px 0 8px;">
          <div style="width:{width:.1f}%;height:100%;background:{color};
                      border-radius:6px;"></div>
        </div>
        """,
        unsafe_allow_html=True,
    )
