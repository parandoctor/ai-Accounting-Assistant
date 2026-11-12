"""统一错误响应结构。

所有非 2xx 响应返回同一格式：

    {"code": "AMOUNT_MISSING", "message": "金额识别失败", "detail": {}}

code 供程序分支判断，message 供展示。
"""

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel


class ErrorResponse(BaseModel):
    """非 2xx 响应的响应体。"""

    code: str
    message: str
    detail: dict | None = None


class ApiError(Exception):
    """业务异常，由已注册的异常处理器转换为 ErrorResponse。"""

    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = status.HTTP_400_BAD_REQUEST,
        detail: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.detail = detail


def register_error_handlers(app: FastAPI) -> None:
    """注册异常处理器，将业务异常与参数校验失败统一转换为 ErrorResponse。"""

    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        body = ErrorResponse(code=exc.code, message=exc.message, detail=exc.detail)
        return JSONResponse(status_code=exc.status_code, content=body.model_dump())

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        # 参数校验失败同样返回统一结构。
        body = ErrorResponse(
            code="INVALID_REQUEST",
            message="请求参数不合法",
            detail={"errors": exc.errors()},
        )
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content=body.model_dump(),
        )
