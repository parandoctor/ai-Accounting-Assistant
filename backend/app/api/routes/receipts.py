"""小票识别接口：校验上传文件后交给视觉模型抽取。"""

from fastapi import APIRouter, File, UploadFile, status

from app import operations
from app.ai import tasks
from app.api.dependencies import ConnDep
from app.core.errors import VALIDATION_ERROR_RESPONSES, ApiError, ErrorResponse
from app.models import Source
from app.schemas import TransactionDraft
from app.utils import MAX_AMOUNT_CENTS

receipts_router = APIRouter(
    prefix="/receipts",
    tags=["小票识别"],
    # 400/413/415 对应 parse_receipt() 里的校验分支：
    # 空文件或金额超限、超过 32MB、MIME 不在 _ALLOWED_TYPES；422 复用统一校验错误结构。
    responses={
        **VALIDATION_ERROR_RESPONSES,
        400: {"model": ErrorResponse, "description": "文件为空或识别出的金额超出范围"},
        413: {"model": ErrorResponse, "description": "图片超过 32MB"},
        415: {"model": ErrorResponse, "description": "不支持的图片格式"},
    },
)

# 允许的图片 MIME 类型。这里只校验客户端声明的 content_type，
# 实际文件内容需要后续图像库或视觉模型进一步判断。
_ALLOWED_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
# 单张图片上限 32MB；parse_receipt() 会多读 1 字节判断是否超限。
_MAX_BYTES = 32 * 1024 * 1024


@receipts_router.post(
    "/parse",
    response_model=TransactionDraft,
    summary="上传小票/截图，识别消费信息",
    description=(
        "接收 `multipart/form-data`，文件字段名 `file`。\n\n"
        "识别出的字段直接返回；未能识别的字段为 null 并列入 `missing_fields`。"
    ),
    responses={
        200: {
            "content": {
                "application/json": {
                    "example": {
                        "draft_id": "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6",
                        "source": "receipt",
                        "amount_cents": 3250,
                        "merchant": "老乡鸡",
                        "occurred_at": "2026-11-11T04:30:00Z",
                        "category": "餐饮",
                        "confidence": 0.92,
                        "missing_fields": [],
                        "raw_text": None,
                    }
                }
            }
        }
    },
)
def parse_receipt(
    conn: ConnDep,
    file: UploadFile = File(..., description="小票或支付截图"),
) -> TransactionDraft:
    """校验上传文件，识别字段后写入 source=receipt 的草稿。

    参数:
        conn: 请求级连接，交给 operations.save_draft() 写入 transaction_drafts。
        file: multipart 字段名为 file；content_type 和文件大小在这里校验。

    返回:
        TransactionDraft：识别出的字段有值，识别不出的为 None 并列入 missing_fields；
        视觉模型不可用或调用失败时四个字段全为 None、confidence 为 0。

    错误:
        ApiError 由 core/errors.py 的异常处理器转成 ErrorResponse：
        - 415 UNSUPPORTED_IMAGE_TYPE：content_type 不在 _ALLOWED_TYPES；
        - 413 IMAGE_TOO_LARGE：读出的字节数超过 _MAX_BYTES；
        - 400 EMPTY_FILE：读出的字节数为 0。
    """
    if file.content_type not in _ALLOWED_TYPES:
        # 先校验 MIME，避免把非图片内容读进来；content_type 由客户端提供，不能完全信任。
        raise ApiError(
            code="UNSUPPORTED_IMAGE_TYPE",
            message=f"不支持的图片格式：{file.content_type}，请使用 JPEG / PNG / GIF / WebP",
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
        )

    # UploadFile.file 是底层临时文件；最多读 _MAX_BYTES + 1 字节，
    # 多出的 1 字节只用于判断是否超限，避免把超大文件整个读进内存。
    content = file.file.read(_MAX_BYTES + 1)
    if len(content) > _MAX_BYTES:
        raise ApiError(
            code="IMAGE_TOO_LARGE",
            message="图片超过 32MB",
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail={"limit_bytes": _MAX_BYTES},
        )
    if not content:
        raise ApiError(code="EMPTY_FILE", message="文件为空")

    # 视觉模型抽取；未配置视觉模型或调用失败时返回空结果，由前端提示用户手动补全。
    recognition = tasks.recognize(content, file.content_type or "image/png")

    # 与 /transactions/parse 一致：草稿金额也要落在 decimal(10,2) 范围内。
    if recognition.amount_cents is not None and recognition.amount_cents > MAX_AMOUNT_CENTS:
        raise ApiError(
            code="AMOUNT_TOO_LARGE",
            message="金额超出可记录范围（上限 99999999.99 元），请核对后重试",
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"max_cents": MAX_AMOUNT_CENTS},
        )

    draft = TransactionDraft(
        # draft_id 由 operations.new_draft_id() 生成，后续保存账目时回传。
        draft_id=operations.new_draft_id(),
        source=Source.RECEIPT,
        amount_cents=recognition.amount_cents,
        merchant=recognition.merchant,
        occurred_at=recognition.occurred_at,
        category=recognition.category,
        confidence=recognition.confidence,
        # 与草稿字段顺序一致，前端按这个数组标记待补项。
        missing_fields=[
            name
            for name, value in (
                ("amount_cents", recognition.amount_cents),
                ("merchant", recognition.merchant),
                ("occurred_at", recognition.occurred_at),
                ("category", recognition.category),
            )
            if value is None
        ],
        raw_text=recognition.raw_text,
        engine=recognition.engine,
    )
    # save_draft() 写入 transaction_drafts 并 commit，返回值直接作为响应。
    return operations.save_draft(conn, draft)
