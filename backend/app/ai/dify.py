"""Dify 接入：任务工作流、文件上传与多轮对话。

三条抽取任务各对应一个工作流，输入变量约定：
- text：用户原话或问句（必填）；
- system_prompt：提示词（可选，工作流不需要时忽略）；
- 图片：小票识别先走 /v1/files/upload 拿 upload_file_id，再以 DIFY_FILE_INPUT
  指定的变量名（默认 image）以 local_file 方式传入工作流。

对话型应用（chatflow / agent）走 /v1/chat-messages，由 Dify 维护多轮上下文和工具调用；
后端只保存 conversation_id 与消息记录。

工作流输出约定：outputs.result 为 JSON 字符串或 JSON 对象，字段与 app.schemas 的
TransactionDraft / QueryIntent 对齐。
"""

import json
import os

import httpx

from app.ai.client import AiUnavailable, BadRequest, post_json, post_multipart

# 任务名 → 工作流名环境变量；未配置时回退到 DIFY_WORKFLOW_NAME。
_WORKFLOW_ENV = {
    "text_extract": "DIFY_WORKFLOW_TEXT",
    "query_parse": "DIFY_WORKFLOW_QUERY",
    "receipt_ocr": "DIFY_WORKFLOW_RECEIPT",
}

# 图片 MIME → 上传文件名后缀，Dify 侧据此识别类型。
_SUFFIX = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp"}

# Dify 要求每次请求带 user 标识，用于区分调用方；这里固定成一个应用级标识。
_USER = "ai-accounting"


def base_url() -> str:
    """Dify 服务根地址，去掉结尾斜杠。"""
    return os.getenv("DIFY_BASE_URL", "").strip().rstrip("/")


def workflow_key() -> str:
    """工作流应用密钥。"""
    return os.getenv("DIFY_API_KEY", "").strip()


def chat_key() -> str:
    """对话型应用密钥，未单独配置时回退到工作流密钥。"""
    return os.getenv("DIFY_CHAT_API_KEY", "").strip() or workflow_key()


def configured() -> bool:
    """工作流是否可用：地址与密钥都齐全。"""
    return bool(base_url() and workflow_key())


def chat_configured() -> bool:
    """对话型应用是否可用：地址与对话密钥都齐全。"""
    return bool(base_url() and chat_key())


def workflow_name(task: str) -> str:
    """按任务取工作流名；单任务没配时回退到 DIFY_WORKFLOW_NAME。"""
    env_name = _WORKFLOW_ENV.get(task)
    if env_name:
        name = os.getenv(env_name, "").strip()
        if name:
            return name
    return os.getenv("DIFY_WORKFLOW_NAME", task).strip() or task


def file_input() -> str:
    """工作流里接收图片的变量名。"""
    return os.getenv("DIFY_FILE_INPUT", "image").strip() or "image"


def _headers(key: str) -> dict[str, str]:
    """Dify 统一用 Bearer 鉴权。"""
    return {"Authorization": f"Bearer {key}"}


def _post(path: str, payload: dict, key: str) -> httpx.Response:
    """向 Dify 发 JSON 请求，把 400 转成 AiUnavailable（Dify 侧降级空间很小）。"""
    try:
        return post_json(f"{base_url()}{path}", payload, _headers(key))
    except BadRequest as exc:
        raise AiUnavailable(f"Dify 拒绝请求：{exc.body[:200]}") from exc


def upload_file(image: bytes, mime: str = "image/png", *, key: str = "") -> str:
    """把图片上传到 Dify，返回 upload_file_id。

    参数:
        image: 图片字节；大小与类型由接口层校验。
        mime: 图片 MIME，用于文件名后缀与 multipart 的 content-type。
        key: 鉴权密钥；工作流与对话应用是不同应用时各自上传，留空回退到工作流密钥。

    返回:
        Dify 分配的 upload_file_id，供工作流或对话的 file 变量引用。
    """
    files = {"file": (f"upload{_SUFFIX.get(mime, '.png')}", image, mime)}
    try:
        resp = post_multipart(
            f"{base_url()}/v1/files/upload",
            {"user": _USER},
            files,
            _headers(key or workflow_key()),
        )
    except BadRequest as exc:
        raise AiUnavailable(f"Dify 拒绝上传：{exc.body[:200]}") from exc

    try:
        return resp.json()["id"]
    except (KeyError, TypeError, ValueError) as exc:
        raise AiUnavailable("Dify 上传响应缺少文件 id") from exc


def _file_payload(file_id: str) -> dict:
    """Dify 文件变量的取值结构。"""
    return {"type": "image", "transfer_method": "local_file", "upload_file_id": file_id}


def run_workflow(
    task: str,
    system_prompt: str,
    text: str,
    *,
    image: bytes = b"",
    mime: str = "image/png",
) -> str:
    """跑一次任务工作流，返回 outputs.result 的文本。

    参数:
        task: text_extract / query_parse / receipt_ocr，决定用哪个工作流。
        system_prompt: 提示词，工作流里可以引用也可以忽略。
        text: 用户原话或问句；小票识别时作为附带说明。
        image / mime: 非空时先上传文件，再把 file 变量传给工作流。

    返回:
        工作流输出的 JSON 文本，由调用方统一解析。
    """
    inputs: dict = {"text": text, "system_prompt": system_prompt}
    if image:
        inputs[file_input()] = _file_payload(upload_file(image, mime))

    resp = _post(
        "/v1/workflows/run",
        {"inputs": inputs, "response_mode": "blocking", "user": _USER},
        workflow_key(),
    )
    try:
        outputs = resp.json()["data"]["outputs"]
    except (KeyError, TypeError, ValueError) as exc:
        raise AiUnavailable("Dify 响应缺少 outputs") from exc

    if isinstance(outputs, dict) and "result" in outputs:
        return str(outputs["result"])
    return json.dumps(outputs, ensure_ascii=False)


def chat(
    query: str,
    conversation_id: str | None,
    *,
    image: bytes = b"",
    mime: str = "image/png",
) -> dict:
    """调用对话型应用，返回 {"answer": str, "conversation_id": str}。

    conversation_id 为空表示新会话；Dify 返回新的 id 后由调用方保存，
    下一轮原样带回即可延续上下文与工具调用记录。
    """
    payload: dict = {
        "inputs": {},
        "query": query,
        "response_mode": "blocking",
        "user": _USER,
        "conversation_id": conversation_id or "",
    }
    if image:
        payload["files"] = [_file_payload(upload_file(image, mime, key=chat_key()))]

    resp = _post("/v1/chat-messages", payload, chat_key())
    try:
        body = resp.json()
    except ValueError as exc:
        raise AiUnavailable("Dify 对话响应不是 JSON") from exc

    answer = body.get("answer")
    if not isinstance(answer, str):
        raise AiUnavailable("Dify 对话响应缺少 answer")
    return {
        "answer": answer,
        "conversation_id": body.get("conversation_id") or conversation_id or "",
    }
