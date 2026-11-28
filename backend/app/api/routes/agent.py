"""智能助手接口：多轮对话与会话历史。"""

from typing import Annotated

from fastapi import APIRouter, Path as PathParam, status

from app import operations
from app.ai import chat as ai_chat
from app.api.dependencies import ConnDep
from app.core.errors import VALIDATION_ERROR_RESPONSES, ApiError, ErrorResponse
from app.schemas import ChatMessage, ChatRequest, ChatResponse, SessionHistory

agent_router = APIRouter(
    prefix="/agent",
    tags=["智能助手"],
    responses=VALIDATION_ERROR_RESPONSES,
)


@agent_router.post("/chat", response_model=ChatResponse, summary="与助手多轮对话")
def chat(payload: ChatRequest, conn: ConnDep) -> ChatResponse:
    """处理一条用户消息并写入会话历史。

    参数:
        payload: session_id 可为空；为空时新建会话。
        conn: 请求级连接。

    返回:
        ChatResponse：回复文本、生成方式与会话标识；下一轮把 session_id 带回来即可。
    """
    reply = ai_chat.chat(conn, payload.message, payload.session_id)
    return ChatResponse(
        session_id=reply.session_id,
        conversation_id=reply.conversation_id,
        reply=reply.reply,
        engine=reply.engine,
    )


@agent_router.get(
    "/sessions/{session_id}",
    response_model=SessionHistory,
    summary="读取会话历史",
    responses={404: {"description": "会话不存在"}},
)
def history(
    session_id: Annotated[str, PathParam(max_length=32, description="会话 id")],
    conn: ConnDep,
) -> SessionHistory:
    """按会话 id 返回历史消息；会话不存在时返回 404。"""
    session = operations.get_chat_session(conn, session_id)
    if not session:
        raise ApiError(
            code="SESSION_NOT_FOUND",
            message="会话不存在",
            status_code=status.HTTP_404_NOT_FOUND,
        )
    return SessionHistory(
        session_id=session.session_id,
        conversation_id=session.conversation_id,
        messages=[ChatMessage(**item) for item in operations.list_chat_messages(conn, session_id)],
    )
