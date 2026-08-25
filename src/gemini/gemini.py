from functools import lru_cache
import os
from pathlib import Path
import re
import time
from typing import Optional

from google import genai
from google.genai import types


from src.helpers import sanitize_name

from src.helpers import GEMINI_PROMPT_TEMPLATE, save_markdown_to_docx,get_file_mime_type
from src.logger import get_logger

logger = get_logger(__name__)

SUFFIXES = ("HSMT", "TCDG", "YCKT")

STORAGE_DIR = Path("storage")
STORAGE_DIR.mkdir(parents=True, exist_ok=True)

MODELS_TO_TRY = [
    'gemini-3.7-flash',
    'gemini-3.6-flash',
    'gemini-3.5-flash',
    'gemini-3.5-flash-lite',
    'gemini-3.1-flash-lite'
]


def sanitize_name(name: str) -> str:
    """Loại bỏ các ký tự không hợp lệ trên hệ thống file."""
    return re.sub(r'[\\/*?:"<>|]', "", name).strip()


@lru_cache(maxsize=1)
def get_gemini_client() -> genai.Client:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY chưa được thiết lập trong biến môi trường.")
    return genai.Client(api_key=api_key)


def _wait_for_file_processing(
    ai_client: genai.Client,
    file_name: str,
    ma_tbmt: str,
    max_retries: int = 12,
    poll_interval: int = 3
) -> types.File:
    """Chờ Google Files API xử lý file với cơ chế timeout an toàn."""
    for _ in range(max_retries):
        file_obj = ai_client.files.get(name=file_name)
        state_name = getattr(file_obj.state, "name", str(file_obj.state))

        if state_name == "ACTIVE":
            return file_obj
        if state_name == "FAILED":
            raise RuntimeError(f"[{ma_tbmt}] Google không thể xử lý file (trạng thái FAILED).")

        logger.info(f"[{ma_tbmt}] Đang chờ Google xử lý file {file_name} ({state_name})...")
        time.sleep(poll_interval)

    raise TimeoutError(f"[{ma_tbmt}] Quá thời gian chờ Google xử lý file {file_name}.")


def gemini_analyse(ma_tbmt: str,context: str = "") -> bool:
    """Quét các file {ma_tbmt}_HSMT, {ma_tbmt}_TCDG, {ma_tbmt}_YCKT nạp vào Gemini và xuất báo cáo DOCX."""
    safe_tbmt = sanitize_name(str(ma_tbmt))
    target_dir = STORAGE_DIR / safe_tbmt

    if not target_dir.is_dir():
        logger.error(f"[{safe_tbmt}] Thư mục không tồn tại: {target_dir}")
        return False

    # 1. Tập hợp các tên file mục tiêu (không phân biệt hoa thường)
    target_stems = {f"{safe_tbmt}_{suffix}".upper() for suffix in SUFFIXES}

    # 2. Tìm tất cả các file hợp lệ trong thư mục
    valid_source_files: list[Path] = [
        file_path
        for file_path in target_dir.iterdir()
        if file_path.is_file()
        and file_path.stat().st_size > 0
        and file_path.stem.upper() in target_stems
    ]

    if not valid_source_files:
        logger.error(f"[{safe_tbmt}] Không tìm thấy file nguồn hợp lệ ({', '.join(target_stems)}) trong thư mục.")
        return False

    output_docx_path = target_dir / f"{safe_tbmt}_BaoCao.docx"
    uploaded_files: list[types.File] = []
    ai_client = get_gemini_client()
    gen_config = types.GenerateContentConfig(temperature=0.2)

    try:
        # 3. Tải tất cả file nguồn lên Gemini Files API và đợi index
        for src_file in valid_source_files:
            mime_type = get_file_mime_type(str(src_file))
            logger.info(f"[{safe_tbmt}] Đang tải file lên Gemini: {src_file.name}/{mime_type}")
            raw_upload = ai_client.files.upload(file=str(src_file),config=dict(mime_type=mime_type))
            
            ready_file = _wait_for_file_processing(
                ai_client=ai_client,
                file_name=raw_upload.name,
                ma_tbmt=safe_tbmt
            )
            uploaded_files.append(ready_file)

        # 4. Fallback qua danh sách models với toàn bộ danh sách file nguồn
        markdown_text: Optional[str] = None
        contents_payload: list = [*uploaded_files]
        if context and context.strip():
            contents_payload.append(context.strip())
        contents_payload.append(GEMINI_PROMPT_TEMPLATE)

        for model_name in MODELS_TO_TRY:
            try:
                logger.info(f"[{safe_tbmt}] Đang gọi model {model_name} với {len(uploaded_files)} file nguồn...")
                response = ai_client.models.generate_content(
                    model=model_name,
                    contents=contents_payload,
                    config=gen_config,
                )
                if response and response.text:
                    markdown_text = response.text
                    break
            except Exception as e_model:
                logger.warning(f"[{safe_tbmt}] Model {model_name} gặp lỗi: {e_model}")
                continue

        if not markdown_text:
            logger.error(f"[{safe_tbmt}] Tất cả model trong danh sách đều không trả về kết quả.")
            return False

        # 5. Xuất file DOCX trực tiếp vào thư mục gói thầu
        save_markdown_to_docx(
            markdown_text=markdown_text,
            output_path=str(output_docx_path),
            title=f"BÁO CÁO PHÂN TÍCH HỒ SƠ: {safe_tbmt}",
        )
        logger.info(f"[{safe_tbmt}] Đã tạo xong báo cáo: {output_docx_path.name}")
        return True

    except Exception as e:
        logger.error(f"Lỗi phân tích Gemini cho gói {safe_tbmt}: {e}", exc_info=True)
        return False

    finally:
        # 6. Dọn dẹp toàn bộ file tạm đã tải lên Google
        for f in uploaded_files:
            if hasattr(f, "name"):
                try:
                    ai_client.files.delete(name=f.name)
                except Exception as del_err:
                    logger.debug(f"[{safe_tbmt}] Không thể xóa file tạm trên Gemini ({f.name}): {del_err}")