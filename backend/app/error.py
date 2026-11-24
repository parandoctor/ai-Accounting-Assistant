"""统一错误响应结构与异常处理。

所有非 2xx 响应最终都转成同一个结构：

    {"code": "AMOUNT_MISSING", "message": "金额识别失败", "detail": {}}

- code：稳定错误码，调用方按它分支；
- message：面向用户的文案；
- detail：附加上下文，没有内容时为 {}。

ApiError 由业务代码主动抛出；Pydantic 校验失败和框架层 HTTPException
分别由 register_error_handlers() 中注册的处理器转换。
"""

from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

# 框架层 HTTPException 只有状态码，这里补业务 code 和中文文案；
# 未列出的状态码回退到 HTTP_ERROR + exc.detail。
_HTTP_ERROR_CODES = {
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
}
_HTTP_ERROR_MESSAGES = {
    404: "接口不存在",
    405: "请求方法不支持",
}


class ErrorResponse(BaseModel):
    """所有非 2xx 响应的统一响应体。

    既用于异常处理器实际返回，也用于各路由 responses 中声明 400/413/415/422。
    """

    code: str = Field(..., description="错误码，供程序分支判断")
    message: str = Field(..., description="可直接展示的文案")
    detail: dict[str, Any] = Field(
        default_factory=dict,
        description="附加上下文，没有时为空对象",
    )


# APIRouter 的 responses 参数使用：把 FastAPI 默认的 422 HTTPValidationError
# 替换成 ErrorResponse。api/transactions.py、api/receipts.py、api/stats.py 都引用它。
VALIDATION_ERROR_RESPONSES = {
    422: {
        "model": ErrorResponse,
        "description": "请求参数不合法",
    }
}


class ApiError(Exception):
    """业务代码主动抛出的异常。

    api/*.py 在业务校验失败时 raise；register_error_handlers() 注册的
    _api_error 会读取实例上的 code / message / status_code / detail，
    生成 ErrorResponse 后返回。
    """

    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = status.HTTP_400_BAD_REQUEST,
        detail: dict[str, Any] | None = None,
    ) -> None:
        """保存错误码、展示文案、HTTP 状态码与附加信息。

        参数:
            code: 稳定错误码，写入 ErrorResponse.code。
            message: 展示文案，写入 ErrorResponse.message。
            status_code: HTTP 状态码，默认 400。
            detail: 附加信息；为 None 时处理器输出 {}。
        """
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.detail = detail


def register_error_handlers(app: FastAPI) -> None:
    """把三类异常统一注册到 FastAPI 应用。

    - ApiError -> _api_error：使用异常实例自带的 code/message/status_code/detail；
    - RequestValidationError -> _validation_error：固定 422 + INVALID_REQUEST；
    - StarletteHTTPException -> _http_error：按 _HTTP_ERROR_CODES 映射 404/405 等。
    """

    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        """处理 ApiError：字段直接取自异常实例，detail 为 None 时输出 {}。"""
        body = ErrorResponse(code=exc.code, message=exc.message, detail=exc.detail or {})
        return JSONResponse(status_code=exc.status_code, content=body.model_dump())

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        """处理 Pydantic 参数校验失败。

        detail.errors 是字段级错误列表；jsonable_encoder 先编码，
        避免其中的 ValueError 等对象无法直接 JSON 序列化。
        """
        body = ErrorResponse(
            code="INVALID_REQUEST",
            message="请求参数不合法",
            detail={"errors": jsonable_encoder(exc.errors())},
        )
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content=body.model_dump(),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        """处理框架层 HTTPException。

        404/405 使用 _HTTP_ERROR_MESSAGES 里的中文文案；
        其他状态码回退到 HTTP_ERROR，message 取 exc.detail（字符串时）。
        """
        body = ErrorResponse(
            code=_HTTP_ERROR_CODES.get(exc.status_code, "HTTP_ERROR"),
            message=_HTTP_ERROR_MESSAGES.get(
                exc.status_code,
                exc.detail if isinstance(exc.detail, str) else "请求失败",
            ),
            detail=exc.detail if isinstance(exc.detail, dict) else {},
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=body.model_dump(),
            headers=exc.headers,
        )
