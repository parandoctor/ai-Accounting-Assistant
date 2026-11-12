"""统一错误响应。

前端和测试只需要认一个结构：

    {"code": "AMOUNT_MISSING", "message": "金额识别失败，请手动填写", "detail": {}}

code 是给程序判断的（前端可以按 code 做不同 UI），message 是给人看的。
"""

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel


class ErrorResponse(BaseModel):
    """所有非 2xx 响应的结构。"""

    code: str
    message: str
    detail: dict | None = None


class ApiError(Exception):
    """业务异常。在路由里 raise 它，会被下面的 handler 转成统一结构。"""

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
    """在 main.py 里调用一次，把三类异常都收敛成同一个 JSON 结构。"""

    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        body = ErrorResponse(code=exc.code, message=exc.message, detail=exc.detail)
        return JSONResponse(status_code=exc.status_code, content=body.model_dump())

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        # 参数校验失败（缺字段、类型不对）也走同一个结构，前端不用写两套逻辑
        body = ErrorResponse(
            code="INVALID_REQUEST",
            message="请求参数不合法",
            detail={"errors": exc.errors()},
        )
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content=body.model_dump(),
        )
