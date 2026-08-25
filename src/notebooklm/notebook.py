import os
from pathlib import Path
from typing import Optional
from notebooklm import NotebookLMClient

from src.helpers import  save_markdown_to_docx
from src.logger import get_logger
from src.helpers import sanitize_name,get_file_mime_type


logger = get_logger(__name__)

SUFFIXES = ("HSMT", "TCDG", "YCKT")

STORAGE_DIR = Path("storage")
STORAGE_DIR.mkdir(parents=True, exist_ok=True)

def build_ask_prompt(context: str = "") -> str:
    """Tạo prompt gửi NotebookLM, kèm dữ liệu context từ DB (nếu có)."""
    context_block = f"\n{context.strip()}\n" if context and context.strip() else ""

    return (
        "Đóng vai chuyên gia đấu thầu. Hãy đọc tất cả tài liệu nguồn HSMT (*_HSMT, *_TCDG, *_YCKT) "
        "kết hợp dữ liệu hệ thống bên dưới để điền đầy đủ toàn bộ nội dung vào khung mẫu của file 'TEMPLATE_Bao_Cao.md'.\n"
        f"{context_block}\n"
        "YÊU CẦU BẮT BUỘC:\n"
        "1. In TRỰC TIẾP TOÀN BỘ nội dung báo cáo Markdown ngay trong câu trả lời này từ đầu đến cuối.\n"
        "2. TUYỆT ĐỐI KHÔNG lưu vào Studio, KHÔNG tạo ghi chú/artifact riêng, KHÔNG viết lời tóm tắt hay giới thiệu ngoài lề.\n"
        "3. Xuất phát ngay bằng tiêu đề: # 📋 BÁO CÁO PHÂN TÍCH HỒ SƠ MỜI THẦU TỔNG HỢP"
    )
CURRENT_DIR = Path(__file__).resolve().parent
TEMPLATE_FILE_PATH = CURRENT_DIR / "TEMPLATE_Bao_Cao.md"


async def notebooklm_analyse(ma_tbmt: str,context: str = "") -> bool:
    
    """Quét các file {ma_tbmt}_HSMT, {ma_tbmt}_TCDG, {ma_tbmt}_YCKT nạp vào NotebookLM cùng file template và xuất báo cáo."""
    safe_tbmt = sanitize_name(str(ma_tbmt))
    target_dir = STORAGE_DIR / safe_tbmt

    if not target_dir.is_dir():
        logger.error(f"[{safe_tbmt}] Thư mục không tồn tại: {target_dir}")
        return False

    # 1. Tạo tập hợp các tên file mục tiêu (không kèm đuôi mở rộng)
    target_stems = {f"{safe_tbmt}_{suffix}".upper() for suffix in SUFFIXES}

    # 2. Tìm tất cả các file có stem khớp mẫu (bỏ qua đuôi mở rộng)
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

    # 3. Đưa file template vào danh sách nạp nguồn (nếu tồn tại)
    sources_to_upload: list[Path] = list(valid_source_files)
    if TEMPLATE_FILE_PATH.is_file() and TEMPLATE_FILE_PATH.stat().st_size > 0:
        sources_to_upload.append(TEMPLATE_FILE_PATH)
    else:
        logger.warning(f"[{safe_tbmt}] Không tìm thấy file template tại: {TEMPLATE_FILE_PATH}")

    output_docx_path = target_dir / f"{safe_tbmt}_BaoCao.docx"
    notebook_id: Optional[str] = None

    try:
        async with await NotebookLMClient.from_storage() as client:
            try:
                # 4. Tạo Notebook tạm
                logger.info(f"[{safe_tbmt}] Tạo Notebook tạm...")
                nb = await client.notebooks.create(title=f"Temp_{safe_tbmt}")
                notebook_id = nb.id

                # 5. Upload các file nguồn (HSMT + Template)
                source_ids = []
                for src_file in sources_to_upload:
                    mime_type = get_file_mime_type(str(src_file))
                    logger.info(f"[{safe_tbmt}] Tải file lên NotebookLM: {src_file.name}/{mime_type}")
                    source = await client.sources.add_file(notebook_id, str(src_file),mime_type=mime_type)
                    source_ids.append(source.id)

                # Chờ xử lý index toàn bộ nguồn
                if source_ids:
                    await client.sources.wait_for_sources(notebook_id, source_ids=source_ids)

                # 6. Phân tích nội dung bằng prompt ngắn
                logger.info(f"[{safe_tbmt}] Đang phân tích tổng hợp hồ sơ qua NotebookLM...")
                final_prompt = build_ask_prompt(context=context)
                result = await client.chat.ask(notebook_id, final_prompt)

                if not result or not getattr(result, "answer", None):
                    logger.error(f"[{safe_tbmt}] Không nhận được phản hồi từ NotebookLM.")
                    return False

                # 7. Ghi file báo cáo DOCX
                save_markdown_to_docx(
                    markdown_text=result.answer,
                    output_path=str(output_docx_path),
                    title=f"BÁO CÁO PHÂN TÍCH HỒ SƠ: {safe_tbmt}",
                )
                logger.info(f"[{safe_tbmt}] Đã tạo xong báo cáo: {output_docx_path.name}")
                return True

            finally:
                # 8. Dọn dẹp Notebook tạm
                if notebook_id:
                    try:
                        await client.notebooks.delete(notebook_id)
                        logger.info(f"[{safe_tbmt}] Đã dọn dẹp Notebook tạm thành công.")
                    except Exception as del_err:
                        logger.debug(f"[{safe_tbmt}] Lỗi khi xóa Notebook tạm: {del_err}")

    except Exception as e:
        logger.error(f"Lỗi tạo DOCX qua NotebookLM cho gói {safe_tbmt}: {e}", exc_info=True)
        return False