"""智能助手：多轮对话的编排与会话持久化。

三种生成方式，按可用性依次降级：
- Dify 对话型应用：上下文与工具调用交给 Dify，后端只存 conversation_id；
- OpenAI 兼容接口：后端把会话历史随每次请求带上，模型据此续上下文；
- 本地单轮解析：没有模型服务或调用失败时，至少把这句话解析成一笔草稿。

记账写入仍然走「草稿 -> 用户确认 -> POST /transactions」，助手不会直接落库。
"""

import logging
from dataclasses import dataclass

from pymysql.connections import Connection

from app import ai_client, ai_tasks, db, dify
from app.ai_client import AiUnavailable

logger = logging.getLogger(__name__)

# 随请求带上的历史消息条数上限：够接住上下文，又不至于让每次请求变慢。
_HISTORY_LIMIT = 20

_SYSTEM_PROMPT = """你是「AI 记账助手」里的对话助手，面向个人记账场景。

你能做：
- 把用户的一句话消费描述整理成待确认的草稿（金额、类别、商户、时间）；
- 解释记账、分类、预算的用法；
- 给一般性的消费分析建议。

边界：
1. 你没有直接读写账本的权限：查账要引导用户去「查账」页，记账要让用户在「记账」页确认后保存；
2. 不编造金额、笔数、预算等任何账本数据，不知道就说不知道；
3. 删除账目必须由用户在页面上二次确认，不要替用户做；
4. 用中文回复，尽量简短，不超过三句话。
"""


@dataclass(frozen=True, slots=True)
class AgentReply:
    """一次对话的结果。

    engine 为 dify 表示由 Dify 对话应用生成，llm 表示由兼容接口按历史生成，
    local 表示本地单轮兜底。
    """

    session_id: str
    reply: str
    engine: str
    conversation_id: str | None


def _local_reply(extraction: ai_tasks.ExpenseExtraction) -> str:
    """把本地单轮解析结果转成一句可读回复。"""
    if extraction.amount_cents:
        category = extraction.category.value if extraction.category else "其他"
        merchant = extraction.merchant or "未识别商户"
        return (
            f"我识别到一笔支出：{extraction.amount_cents / 100:.2f} 元，"
            f"类别「{category}」，商户「{merchant}」。确认后我再帮你记进账本。"
        )
    return "这句里还没看出金额。可以具体一点，例如「午饭 25」；也可以问我「这个月花了多少」。"


def _history(conn: Connection, session_id: str) -> list[dict[str, str]]:
    """取会话最近的用户与助手消息，作为模型的上下文输入。

    返回:
        list[dict]：按时间正序的 {"role", "content"}，最后一条是本次用户输入。
    """
    rows = db.list_chat_messages(conn, session_id, limit=_HISTORY_LIMIT)
    return [
        {"role": row["role"], "content": row["content"]}
        for row in rows
        if row["role"] in ("user", "assistant")
    ]


def _chat_with_model(conn: Connection, session_id: str) -> str:
    """走 OpenAI 兼容接口：把系统提示与历史消息一起发给模型。"""
    return ai_client.chat(_SYSTEM_PROMPT, _history(conn, session_id))


def _chat_with_dify(conn: Connection, session: db.ChatSession, message: str) -> tuple[str, str | None]:
    """走 Dify 对话应用，返回回复与最新 conversation_id（新会话时回写）。"""
    result = dify.chat(message, session.conversation_id)
    conversation_id = result["conversation_id"] or session.conversation_id
    if conversation_id and conversation_id != session.conversation_id:
        db.set_chat_conversation_id(conn, session.session_id, conversation_id)
    return result["answer"], conversation_id


def chat(conn: Connection, message: str, session_id: str | None = None) -> AgentReply:
    """处理一条用户消息，返回助手回复并落库。

    参数:
        conn: 请求级连接。
        message: 用户输入，长度由 schema 校验。
        session_id: 前端持有的会话 id；为空时新建会话。

    返回:
        AgentReply：会话 id、回复文本、生成方式与 Dify 会话标识。
    """
    session = db.get_or_create_chat_session(conn, session_id)
    db.add_chat_message(conn, session.session_id, "user", message)

    conversation_id = session.conversation_id
    if not ai_client.chat_configured():
        reply = _local_reply(ai_tasks.extract(message))
        engine = "local"
    elif ai_client.provider() == "dify":
        try:
            reply, conversation_id = _chat_with_dify(conn, session, message)
            engine = "dify"
        except AiUnavailable as exc:
            # Dify 临时不可用时退回本地解析，保证对话不中断。
            logger.warning("Dify 对话失败，退回本地解析：%s", exc)
            reply = _local_reply(ai_tasks.extract(message))
            engine = "local"
    else:
        try:
            reply = _chat_with_model(conn, session.session_id)
            engine = "llm"
        except AiUnavailable as exc:
            logger.warning("模型对话失败，退回本地解析：%s", exc)
            reply = _local_reply(ai_tasks.extract(message))
            engine = "local"

    db.add_chat_message(conn, session.session_id, "assistant", reply)
    return AgentReply(
        session_id=session.session_id,
        reply=reply,
        engine=engine,
        conversation_id=conversation_id,
    )
