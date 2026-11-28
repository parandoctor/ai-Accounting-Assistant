"""页面：多轮对话助手（上下文由后端保存，未配置模型时本地解析兜底）。"""

from __future__ import annotations

import streamlit as st

from api_client import ApiError, api_get, api_post


CHAT_STARTERS = ["这个月餐饮花了多少", "帮我记一笔：午饭 25", "我的预算还够吗"]


def _send_chat(message: str) -> None:
    """发送一条消息，会话 id 由后端返回后存进 session_state。"""
    payload = {"message": message}
    session_id = st.session_state.get("chat_session_id")
    if session_id:
        payload["session_id"] = session_id
    try:
        answer = api_post("/agent/chat", json=payload)
    except ApiError as exc:
        st.error(f"助手暂时不可用：{exc.message}")
        return
    st.session_state["chat_session_id"] = answer["session_id"]
    st.rerun()


def render_assistant_page() -> None:
    """页面 5：多轮对话。

    历史消息以后端为准，每次渲染都从 /agent/sessions/{id} 拉取，
    这样刷新页面、换设备都能接着聊；未配置模型时后端退化为本地单轮解析。
    """
    st.header("💬 智能助手")
    st.caption("多轮对话：上下文由后端保存，未配置模型时退回本地单轮解析。")

    session_id = st.session_state.get("chat_session_id")
    data = None
    if session_id:
        try:
            data = api_get(f"/agent/sessions/{session_id}")
        except ApiError:
            # 会话在库里找不到（比如换了数据库）时当新会话处理
            st.session_state["chat_session_id"] = None
            session_id = None

    messages = (data or {}).get("messages") or []

    if messages:
        for item in messages:
            with st.chat_message(item["role"]):
                st.markdown(item["content"])
        st.caption("对话记录由后端保存，可以接着往下问")
    else:
        st.info("先打个招呼，或者点下面的示例开始。")
        cols = st.columns(len(CHAT_STARTERS))
        for index, text in enumerate(CHAT_STARTERS):
            if cols[index].button(text, key=f"chat_starter_{index}"):
                _send_chat(text)

    prompt = st.chat_input("说点什么，比如「午饭 25」或「这个月餐饮花了多少」")
    if prompt:
        _send_chat(prompt)

    if messages:
        st.divider()
        if st.button("开始新会话", key="chat_new_session"):
            st.session_state["chat_session_id"] = None
            st.rerun()
