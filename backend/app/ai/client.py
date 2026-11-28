"""模型调用：OpenAI 兼容接口与 Dify 两种 provider，外加模型输出的字段清洗。

- 传输层：统一超时、重试与错误归类，调用方只需要捕获 AiUnavailable；
- OpenAI 兼容接口：文本抽取、多轮对话与图片识别都走 /chat/completions；
- Dify 接入见 app.ai.dify；本模块只保留两者共用的传输层与 OpenAI 兼容调用；
- 清洗函数：把模型返回的 JSON 收敛成本系统认识的类型（金额、时间、类别、商户）。

配置全在环境变量里，见 backend/.env.example；未配置密钥时 configured() 为 False，
调用方走规则兜底，接口本身不会失败。
"""

import base64
import json
import logging
import os
import re
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation

import httpx

from app.core import config as _config  # noqa: F401  触发 backend/.env 加载
from app.models import Category
from app.utils import APP_TZ, ensure_utc

logger = logging.getLogger(__name__)

# 重试退避基数（秒），第 n 次重试等待 RETRY_BACKOFF * n。
RETRY_BACKOFF = 0.8

# 进程级连接池：每次请求重建 TLS 连接既慢，也容易被网关中途断开（SSL EOF / RST）。
# httpx.Client 线程安全，FastAPI 的同步路由共用这一个即可；超时按请求传入，
# 因此 AI_TIMEOUT 改了也不用重建。
_client = httpx.Client(
    limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
)


class AiUnavailable(Exception):
    """模型服务不可用：未配置、超时、HTTP 错误或返回无法解析。

    调用方捕获后走规则兜底，接口本身仍然返回 200。
    """


class BadRequest(Exception):
    """服务端返回 400：通常是请求体里有它不认识的字段。

    由调用方判断能否降级（例如去掉 response_format）后再重试。
    """

    def __init__(self, body: str) -> None:
        super().__init__(body)
        self.body = body


def timeout() -> float:
    """单次请求超时（秒），每次调用时读环境变量，便于测试和热改。"""
    return float(os.getenv("AI_TIMEOUT", "20"))


def retries() -> int:
    """额外重试次数，每次调用时读环境变量。"""
    return int(os.getenv("AI_RETRIES", "2"))


def _post(
    url: str,
    *,
    headers: dict[str, str],
    json_body: dict | None = None,
    data: dict | None = None,
    files: dict | None = None,
) -> httpx.Response:
    """发送 POST 并按状态码决定重试、降级还是判不可用。"""
    last_error = ""
    for attempt in range(retries() + 1):
        try:
            resp = _client.post(
                url,
                headers=headers,
                json=json_body,
                data=data,
                files=files,
                timeout=timeout(),
            )
        except httpx.TimeoutException:
            last_error = f"请求超时（{timeout():g}s）"
        except httpx.HTTPError as exc:
            last_error = f"网络错误：{exc}"
        else:
            if resp.status_code == 400:
                raise BadRequest(resp.text[:500])
            if resp.status_code in (429, 500, 502, 503, 504):
                last_error = f"HTTP {resp.status_code}：{resp.text[:200]}"
            elif resp.is_success:
                return resp
            else:
                raise AiUnavailable(
                    f"模型服务返回 HTTP {resp.status_code}：{resp.text[:200]}"
                )

        if attempt < retries():
            delay = RETRY_BACKOFF * (attempt + 1)
            logger.warning(
                "模型请求失败，%.1fs 后重试（第 %s 次）：%s",
                delay,
                attempt + 1,
                last_error,
            )
            time.sleep(delay)

    raise AiUnavailable(f"模型请求失败：{last_error}")


def post_json(url: str, payload: dict, headers: dict[str, str]) -> httpx.Response:
    """发送 JSON 请求，返回 2xx 响应。"""
    return _post(url, headers=headers, json_body=payload)


def post_multipart(
    url: str,
    data: dict,
    files: dict,
    headers: dict[str, str],
) -> httpx.Response:
    """发送 multipart 请求（文件上传），返回 2xx 响应。"""
    return _post(url, headers=headers, data=data, files=files)




# 仅用于 /ai/status 展示；实际请求每次从 transport 读环境变量。
AI_TIMEOUT = timeout()
AI_RETRIES = retries()

# 三种任务共用一套配置，Dify 侧按任务选工作流。
TASK_TEXT = "text_extract"
TASK_RECEIPT = "receipt_ocr"
TASK_QUERY = "query_parse"

# 各 provider 必需的配置项；未配置时接口直接走规则兜底，这里用于说清缺哪一项。
_REQUIRED_ENV = {
    "dify": ("DIFY_BASE_URL", "DIFY_API_KEY"),
    "openai": ("AI_API_KEY",),
}


# 单次请求的输出上限。思考型模型先消耗思考 token 再输出正文，
# 800 这种小上限容易被思考占满、正文为空，这里留够余量。
MAX_TOKENS = 2000

# OpenAI 兼容服务未必都认这些可选字段：遇到 400 时按顺序逐个去掉重试。
_OPTIONAL_FIELDS = ("response_format", "thinking")


def provider() -> str:
    """当前 provider，取值 openai 或 dify；默认走 OpenAI 兼容接口。"""
    return os.getenv("AI_PROVIDER", "openai").strip().lower()


def _thinking_disabled() -> bool:
    """是否让模型跳过思考，默认跳过。

    字段抽取和意图解析都是短任务，思考过程只会多花 token 与等待时间；
    需要更强推理时设 AI_THINKING=on。发的是 DeepSeek 系接口的 thinking 字段，
    服务端不认这个字段返回 400 时，_post_chat() 会去掉它重试。
    """
    return os.getenv("AI_THINKING", "").strip().lower() not in {"on", "true", "1", "enabled"}


def _api_key() -> str:
    """OpenAI 兼容接口的文本模型密钥。"""
    return os.getenv("AI_API_KEY", "").strip()


def configured() -> bool:
    """是否配置了可用的模型服务。"""
    if provider() == "dify":
        return dify.configured()
    return bool(_api_key())


def missing_config() -> list[str]:
    """当前 provider 缺哪些环境变量，用于排障时指名道姓。

    返回:
        缺失的变量名列表；空列表表示配置齐全，AI 链路会真的走模型而不是规则。
        只判断「有没有填」，不校验值是否有效，密钥写错由调用失败时的日志暴露。
    """
    names = _REQUIRED_ENV.get(provider(), _REQUIRED_ENV["openai"])
    return [name for name in names if not os.getenv(name, "").strip()]


def vision_configured() -> bool:
    """图片识别是否可用：与文本模型同一套配置。

    deepseek-flash 这类多模态模型文本与图片共用一个模型和密钥；
    Dify 方式由工作流内部选模型，只要工作流可用即可。
    """
    if provider() == "dify":
        return dify.configured()
    return bool(_api_key())


def chat_configured() -> bool:
    """多轮对话是否可用：Dify 对话应用，或已配置密钥的 OpenAI 兼容接口。"""
    if provider() == "dify":
        return dify.chat_configured()
    return bool(_api_key())


def model_name() -> str:
    """当前文本模型名，用于 /ai/status 展示。"""
    if provider() == "dify":
        return dify.workflow_name(TASK_TEXT)
    return os.getenv("AI_MODEL", "deepseek-flash")


def vision_model_name() -> str:
    """图片识别用的模型名：与文本模型相同，多模态模型不需要单独配置。"""
    return model_name()


def _base_url() -> str:
    """OpenAI 兼容服务的根地址，去掉结尾斜杠。"""
    return os.getenv("AI_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")


def _strip_code_fence(text: str) -> str:
    """去掉模型偶尔包在外面的 ```json 代码块标记。"""
    match = re.search(r"\{.*\}", text, re.S)
    return match.group(0) if match else text


def _parse_json(text: str) -> dict:
    """把模型返回的文本解析成 dict；解析失败视为模型不可用。"""
    try:
        data = json.loads(_strip_code_fence(text))
    except json.JSONDecodeError as exc:
        raise AiUnavailable(f"模型返回不是合法 JSON：{text[:120]}") from exc
    if not isinstance(data, dict):
        raise AiUnavailable("模型返回不是 JSON 对象")
    return data


def _to_data_url(image: bytes, mime: str) -> str:
    """图片转成 data URL，供视觉模型的 image_url 字段使用。"""
    encoded = base64.b64encode(image).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _post_chat(url: str, payload: dict, headers: dict[str, str]) -> httpx.Response:
    """发送 /chat/completions 请求，400 时按可选字段逐个去掉后重试。

    不同兼容服务对 response_format、thinking 这类可选字段的接受程度不一样，
    400 基本就是「不认识这个字段」；去掉再试一次，比直接判模型不可用划算。
    """
    while True:
        try:
            return post_json(url, payload, headers)
        except BadRequest as exc:
            dropped = next(
                (name for name in _OPTIONAL_FIELDS if name in payload and name in exc.body),
                None,
            )
            if dropped is None:
                raise AiUnavailable(f"模型服务拒绝请求：{exc.body[:200]}") from exc
            logger.warning("服务端不支持 %s，去掉后重试：%s", dropped, exc.body[:120])
            payload.pop(dropped, None)


def _response_text(resp: httpx.Response) -> str:
    """取 /chat/completions 响应里的正文，空正文视为模型不可用。

    思考型模型先输出 reasoning_content 再给 content，输出上限被思考占满时
    content 是空串；这种时候不能把思考过程当结果用，只能判定本次调用失败。
    """
    try:
        choice = resp.json()["choices"][0]
        content = (choice.get("message") or {}).get("content")
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise AiUnavailable("模型响应缺少 choices 内容") from exc
    if not isinstance(content, str) or not content.strip():
        raise AiUnavailable(
            "模型没有返回正文（finish_reason="
            f"{choice.get('finish_reason')}），可调大 max_tokens 或设置 AI_THINKING=off"
        )
    return content


def _call_openai(system: str, user: str, model: str, image: bytes, mime: str) -> str:
    """调用 OpenAI 兼容的 /chat/completions，返回模型输出的文本。

    image 非空时把图片作为 image_url 一起发送，用于小票识别；
    图片与文本共用同一个模型和密钥。
    """
    if image:
        content: object = [
            {"type": "text", "text": user},
            {"type": "image_url", "image_url": {"url": _to_data_url(image, mime)}},
        ]
    else:
        content = user

    payload: dict = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": content},
        ],
        "temperature": 0.2,
        "max_tokens": MAX_TOKENS,
        # 兼容服务大多支持 JSON 模式；个别服务不认这个字段，_post_chat 会降级重试。
        "response_format": {"type": "json_object"},
    }
    if _thinking_disabled():
        payload["thinking"] = {"type": "disabled"}

    headers = {"Authorization": f"Bearer {_api_key()}"}
    resp = _post_chat(f"{_base_url()}/chat/completions", payload, headers)
    return _response_text(resp)


def chat(system: str, messages: list[dict[str, str]]) -> str:
    """按完整消息列表调用文本模型，返回助手回复正文。

    参数:
        system: 系统提示，约束助手的角色与边界。
        messages: 历史消息，元素为 {"role": "user"|"assistant", "content": str}，按时间正序；
            最后一条应当是本次用户输入。

    返回:
        str：模型回复正文，不是 JSON。

    异常:
        AiUnavailable：未配置、请求失败或返回正文为空，调用方应走本地兜底。
    """
    if not configured():
        raise AiUnavailable("未配置模型服务")

    payload: dict = {
        "model": model_name(),
        "messages": [{"role": "system", "content": system}, *messages],
        "temperature": 0.3,
        "max_tokens": MAX_TOKENS,
    }
    if _thinking_disabled():
        payload["thinking"] = {"type": "disabled"}

    started = time.perf_counter()
    resp = _post_chat(
        f"{_base_url()}/chat/completions",
        payload,
        {"Authorization": f"Bearer {_api_key()}"},
    )
    answer = _response_text(resp).strip()
    logger.info(
        "多轮对话完成 provider=openai model=%s 轮数=%s 耗时=%.2fs",
        payload["model"],
        len(messages),
        time.perf_counter() - started,
    )
    return answer


def extract_json(
    task: str,
    system: str,
    user: str,
    *,
    image: bytes = b"",
    mime: str = "image/png",
) -> dict:
    """调用模型并返回解析后的 JSON 对象。

    参数:
        task: TASK_TEXT / TASK_RECEIPT / TASK_QUERY，决定 Dify 用哪个工作流。
        system: 系统提示，约束模型输出字段与取值。
        user: 用户输入文本；图片识别时作为附带的文字说明。
        image / mime: 非空时按视觉方式处理。

    返回:
        dict：模型输出的 JSON 对象，字段由调用方校验。

    异常:
        AiUnavailable：未配置、请求失败或返回无法解析，调用方应走规则兜底。
    """
    if not configured():
        raise AiUnavailable("未配置模型服务")

    started = time.perf_counter()
    if provider() == "dify":
        model = dify.workflow_name(task)
        raw = dify.run_workflow(task, system, user, image=image, mime=mime)
    else:
        model = model_name()
        raw = _call_openai(system, user, model, image, mime)

    result = _parse_json(raw)
    logger.info(
        "模型调用完成 provider=%s task=%s model=%s 耗时=%.2fs",
        provider(),
        task,
        model,
        time.perf_counter() - started,
    )
    return result


# 记账用的本地时区；模型给出不带时区的时间时按它理解。定义在 utils，全项目共用一份。

# 商户名与数据库 varchar(30) 对齐。
MERCHANT_MAX = 30


def amount_cents(value: object) -> int | None:
    """金额从「元」转「分」；非法或非正数返回 None。"""
    if value is None:
        return None
    try:
        cents = int((Decimal(str(value)) * 100).to_integral_value())
    except (InvalidOperation, ValueError):
        return None
    return cents if cents > 0 else None


def occurred_at(value: object) -> datetime | None:
    """时间字符串转成带 UTC 时区的 datetime；解析失败返回 None。"""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=APP_TZ)
    return ensure_utc(parsed)


def category(value: object) -> Category | None:
    """类别必须落在枚举内，其余一律视为未识别。"""
    if not isinstance(value, str):
        return None
    try:
        return Category(value.strip())
    except ValueError:
        return None


def merchant(value: object) -> str | None:
    """商户名去空格并截断到列宽；空串返回 None。"""
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned[:MERCHANT_MAX] if cleaned else None


def confidence(value: object) -> float:
    """置信度收敛到 0~1；非法时给 0.5。"""
    try:
        parsed = float(str(value))
    except (TypeError, ValueError):
        return 0.5
    return min(max(parsed, 0.0), 1.0)


def date_range(
    start: object,
    end: object,
) -> tuple[datetime | None, datetime | None]:
    """解析问句里的起止时间，返回带 UTC 时区的闭区间。"""
    return occurred_at(start), occurred_at(end)


# dify.py 复用本模块的传输与错误类型；放在末尾导入以避开循环依赖。
from app.ai import dify  # noqa: E402
