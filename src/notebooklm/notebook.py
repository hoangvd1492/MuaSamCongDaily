import asyncio
import os
from pathlib import Path
from typing import Optional

from notebooklm import NotebookLMClient

from src.helpers import PROMPT_TEMPLATE, save_markdown_to_docx
from src.logger import get_logger

logger = get_logger(__name__)


async def _async_worker(pdf_path: str, docx_path: str, ma_tbmt: str) -> bool:
    """Core logic chạy ngầm bằng async."""
    notebook_id: Optional[str] = None

    # Khởi tạo client qua context manager
    async with await NotebookLMClient.from_storage() as client:
        try:
            # 1. Tạo Notebook tạm
            logger.info(f"[{ma_tbmt}] Tạo Notebook tạm...")
            nb = await client.notebooks.create(title=f"Temp_{ma_tbmt}")
            notebook_id = nb.id

            # 2. Upload file & chờ index
            logger.info(f"[{ma_tbmt}] Tải file lên NotebookLM: {pdf_path}")
            source = await client.sources.add_file(notebook_id, pdf_path)
            await client.sources.wait_for_sources(notebook_id, source_ids=[source.id])

            # 3. Phân tích nội dung
            logger.info(f"[{ma_tbmt}] Đang phân tích hồ sơ qua NotebookLM...")
            result = await client.chat.ask(notebook_id, PROMPT_TEMPLATE)

            if not result or not getattr(result, "answer", None):
                logger.error(f"[{ma_tbmt}] Không nhận được phản hồi từ NotebookLM.")
                return False

            # 4. Lưu DOCX
            save_markdown_to_docx(
                markdown_text=result.answer,
                output_path=docx_path,
                title=f"BÁO CÁO PHÂN TÍCH HỒ SƠ: {ma_tbmt}",
            )
            return True

        finally:
            # Dọn dẹp notebook tạm
            if notebook_id:
                try:
                    await client.notebooks.delete(notebook_id)
                except Exception as del_err:
                    logger.debug(f"[{ma_tbmt}] Không thể xóa Notebook tạm: {del_err}")


def notebooklm_analyse(pdf_path: str | Path, docx_path: str | Path, ma_tbmt: str) -> bool:
    """Hàm synchronous hoàn toàn - gọi trực tiếp không cần await."""
    
   
    
    pdf_path_str = str(pdf_path)
    docx_path_str = str(docx_path)

    if not os.path.exists(pdf_path_str):
        logger.error(f"[{ma_tbmt}] File không tồn tại: {pdf_path_str}")
        return False

    try:
        # Chạy task async đồng bộ
        return asyncio.run(_async_worker(pdf_path_str, docx_path_str, ma_tbmt))
    except Exception as e:
        logger.error(f"Lỗi tạo DOCX qua NotebookLM cho gói {ma_tbmt}: {e}", exc_info=True)
        return False