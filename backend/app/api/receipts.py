"""小票 / 支付截图识别接口。"""

from fastapi import APIRouter, File, UploadFile, status

from app import store
from app.core.errors import ApiError
from app.schemas.transaction import Source, TransactionDraft

router = APIRouter(prefix="/receipts", tags=["小票识别"])

# deepseek-flash 支持的格式（它按文件内容判断，不看扩展名，但我们这里先按 MIME 挡一道）
_ALLOWED_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
_MAX_BYTES = 32 * 1024 * 1024


@router.post(
    "/parse",
    response_model=TransactionDraft,
    summary="上传小票/截图，识别消费信息",
    description=(
        "接收 `multipart/form-data`，字段名 `file`。\n\n"
        "识别成功返回金额、商户、时间、类别，`confidence` 表示把握；\n"
        "识别不出来的字段返回 null 并列进 `missing_fields`，前端让用户补。"
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
                        "occurred_at": "2026-11-11T12:30:00+08:00",
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
async def parse_receipt(file: UploadFile = File(..., description="小票或支付截图")) -> TransactionDraft:
    if file.content_type not in _ALLOWED_TYPES:
        raise ApiError(
            code="UNSUPPORTED_IMAGE_TYPE",
            message=f"不支持的图片格式：{file.content_type}，请用 JPEG / PNG / GIF / WebP",
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
        )

    content = await file.read()
    if len(content) > _MAX_BYTES:
        raise ApiError(
            code="IMAGE_TOO_LARGE",
            message="图片超过 32MB，请压缩后重试",
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail={"size_bytes": len(content), "limit_bytes": _MAX_BYTES},
        )
    if not content:
        raise ApiError(code="EMPTY_FILE", message="上传的文件是空的")

    # TODO(模型)：把 content 转成 base64 传给 deepseek-flash，让它返回 JSON。
    # 参考 api-test/vision_test.py，那边是完整可运行的版本。
    draft = TransactionDraft(
        draft_id=store.new_draft_id(),
        source=Source.RECEIPT,
        amount_cents=None,
        merchant=None,
        occurred_at=None,
        category=None,
        confidence=0.0,
        missing_fields=["amount_cents", "merchant", "occurred_at", "category"],
    )
    return store.save_draft(draft)
