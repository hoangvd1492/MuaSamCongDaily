from functools import lru_cache
import os
from pathlib import Path
import time

from google import genai
from google.genai import types

from src.helpers import PROMPT_TEMPLATE, save_markdown_to_docx
from src.logger import get_logger

logger = get_logger(__name__)


# GIỮ NGUYÊN DANH SÁCH MODEL CỦA BẠN
MODELS_TO_TRY = [
    'gemini-3.7-flash',
    'gemini-3.6-flash',
    'gemini-3.5-flash',
    'gemini-3.5-flash-lite',
    'gemini-3.1-flash-lite'
]


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
            raise RuntimeError(f"[{ma_tbmt}] Google không thể xử lý file PDF (trạng thái FAILED).")

        logger.info(f"[{ma_tbmt}] Đang chờ Google xử lý file ({state_name})...")
        time.sleep(poll_interval)

    raise TimeoutError(f"[{ma_tbmt}] Quá thời gian chờ Google xử lý file PDF.")


def gemini_analyse(pdf_path: str | Path, docx_path: str | Path, ma_tbmt: str) -> bool:
    pdf_path_str = str(pdf_path)
    docx_path_str = str(docx_path)

    if not os.path.exists(pdf_path_str):
        logger.error(f"[{ma_tbmt}] File không tồn tại: {pdf_path_str}")
        return False

    uploaded_file = None
    ai_client = get_gemini_client()
    gen_config = types.GenerateContentConfig(temperature=0.2)

    try:
        # 1. Upload PDF
        logger.info(f"[{ma_tbmt}] Đang tải file lên Gemini: {pdf_path_str}")
        uploaded_file = ai_client.files.upload(file=pdf_path_str)

        # Chờ xử lý file
        uploaded_file = _wait_for_file_processing(
            ai_client=ai_client,
            file_name=uploaded_file.name,
            ma_tbmt=ma_tbmt
        )

        # 2. Fallback qua danh sách models
        markdown_text: str | None = None
        for model_name in MODELS_TO_TRY:
            try:
                logger.info(f"[{ma_tbmt}] Đang gọi model {model_name}...")
                response = ai_client.models.generate_content(
                    model=model_name,
                    contents=[uploaded_file, PROMPT_TEMPLATE],
                    config=gen_config,
                )
                if response and response.text:
                    markdown_text = response.text
                    break
            except Exception as e_model:
                logger.warning(f"[{ma_tbmt}] Model {model_name} gặp lỗi: {e_model}")
                continue

        if not markdown_text:
            logger.error(f"[{ma_tbmt}] Tất cả model trong danh sách đều không trả về kết quả.")
            return False

        # 3. Xuất file DOCX trực tiếp
        save_markdown_to_docx(
            markdown_text=markdown_text,
            output_path=docx_path_str,
            title=f"BÁO CÁO PHÂN TÍCH HỒ SƠ: {ma_tbmt}",
        )
        return True

    except Exception as e:
        logger.error(f"Lỗi phân tích Gemini cho gói {ma_tbmt}: {e}", exc_info=True)
        return False

    finally:
        if uploaded_file and hasattr(uploaded_file, "name"):
            try:
                ai_client.files.delete(name=uploaded_file.name)
            except Exception as del_err:
                logger.debug(f"[{ma_tbmt}] Không thể xóa file tạm trên Gemini ({uploaded_file.name}): {del_err}")