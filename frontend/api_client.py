"""后端接口访问层：统一超时、错误转换与 GET/POST/PUT/DELETE 封装。"""

from __future__ import annotations

import requests

from config import API_URL


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
    # 默认 40s：后端调用大模型最多等 AI_TIMEOUT=20s，前端要留出网络与重试的余量。
    timeout = kwargs.pop("timeout", 40)
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
        if not message and resp.status_code >= 500:
            # 5xx 常常没有响应体（如金额超出数据库范围），给一句能看懂的话
            message = f"服务端处理失败（HTTP {resp.status_code}），请检查输入后重试"
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
