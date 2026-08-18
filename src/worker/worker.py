import asyncio
import logging
from pathlib import Path
from typing import Dict, Set
from src.playwright.playwright import download_hsmt
from src.notebooklm.notebook import generate_report_docx
from telegram import InputMediaDocument

logger = logging.getLogger("APP.HSMT_QUEUE")

DOWNLOAD_QUEUE: asyncio.Queue = asyncio.Queue()
PENDING_TASKS: Dict[str | int, Set[int]] = {}

STORAGE_DIR = Path("storage")
STORAGE_DIR.mkdir(parents=True, exist_ok=True)


def get_existing_hsmt_file(ma_tbmt: str) -> Path | None:
    file_path = STORAGE_DIR / str(ma_tbmt).strip() / "HSMT.pdf"
    return file_path if (file_path.is_file() and file_path.stat().st_size > 0) else None


def get_existing_baocao_file(ma_tbmt: str) -> Path | None:
    file_path = STORAGE_DIR / str(ma_tbmt).strip() / "BaoCao.docx"
    return file_path if (file_path.is_file() and file_path.stat().st_size > 0) else None


def is_task_pending(task_id: str | int) -> bool:
    return task_id in PENDING_TASKS


async def add_download_task(id: str | int, ma_tbmt: str, chat_id: int) -> bool:
    if is_task_pending(id):
        PENDING_TASKS[id].add(chat_id)
        return True

    PENDING_TASKS[id] = {chat_id}
    await DOWNLOAD_QUEUE.put({"id": id, "ma_tbmt": ma_tbmt})
    return True


async def hsmt_download_worker(bot):
    """Worker chạy nền xử lý tải và tạo báo cáo rồi gửi gom nhóm 2 file."""
    logger.info("Worker HSMT & AI Report đã sẵn sàng.")
    while True:
        task = await DOWNLOAD_QUEUE.get()
        item_id = task["id"]
        ma_tbmt = str(task["ma_tbmt"]).strip()
        waiting_chat_ids = PENDING_TASKS.get(item_id, set())

        target_dir = STORAGE_DIR / ma_tbmt
        pdf_path = target_dir / "HSMT.pdf"
        docx_path = target_dir / "BaoCao.docx"
        temp_pdf_path = target_dir / "HSMT.pdf.tmp"

        try:
            # 1. TẢI FILE PDF (NẾU CHƯA CÓ HOẶC FILE CŨ BỊ LỖI)
            target_dir.mkdir(parents=True, exist_ok=True)
            
            if not pdf_path.is_file() or pdf_path.stat().st_size == 0:
                pdf_bytes = await download_hsmt(item_id)
                if not pdf_bytes or len(pdf_bytes) == 0:
                    raise ValueError("Không nhận được dữ liệu PDF hoặc dữ liệu rỗng.")

                # Ghi vào file tạm để chống lỗi file rác/0 KB
                with open(temp_pdf_path, "wb") as f:
                    f.write(pdf_bytes)
                temp_pdf_path.replace(pdf_path)

            # 2. TẠO FILE DOCX BÁO CÁO AI (NẾU CHƯA CÓ)
            docx_success = docx_path.is_file() and docx_path.stat().st_size > 0
            if not docx_success:
                docx_success = await generate_report_docx(str(pdf_path), str(docx_path), ma_tbmt)

            # 3. TRẢ FILE CHO CÁC NGƯỜI DÙNG ĐANG CHỜ
            for chat_id in waiting_chat_ids:
                try:
                    if docx_success:
                        # GỬI CÙNG LÚC 2 FILE TRONG 1 TIN NHẮN (MEDIA GROUP)
                        with open(pdf_path, "rb") as f_pdf, open(docx_path, "rb") as f_docx:
                            media_group = [
                                InputMediaDocument(
                                    media=f_pdf,
                                    filename=f"HSMT_{ma_tbmt}.pdf",
                                ),
                                InputMediaDocument(
                                    media=f_docx,
                                    filename=f"BaoCao_{ma_tbmt}.docx",
                                    caption=f"✅HSMT & Tóm tắt cho gói <b>{ma_tbmt}</b>!",
                                    parse_mode="HTML",
                                ),
                            ]
                            await bot.send_media_group(chat_id=chat_id, media=media_group)
                    else:
                        # Nếu tạo DOCX lỗi, gửi 1 file PDF kèm lời nhắn
                        with open(pdf_path, "rb") as f_pdf:
                            await bot.send_document(
                                chat_id=chat_id,
                                document=f_pdf,
                                filename=f"HSMT_{ma_tbmt}.pdf",
                                caption=f"✅HSMT cho gói <b>{ma_tbmt}</b>!",
                                parse_mode="HTML"
                            )
                except Exception as send_err:
                    logger.error(f"Lỗi gửi file Telegram tới chat {chat_id}: {send_err}")

        except Exception as e:
            # XỬ LÝ LỖI TOÀN BỘ VÀ DỌN RÁC
            logger.error(f"Thất bại toàn bộ: Lỗi tải PDF gói {ma_tbmt} | Chi tiết: {e}")
            if temp_pdf_path.is_file():
                temp_pdf_path.unlink(missing_ok=True)
            if pdf_path.is_file() and pdf_path.stat().st_size == 0:
                pdf_path.unlink(missing_ok=True)

            for chat_id in waiting_chat_ids:
                try:
                    await bot.send_message(
                        chat_id=chat_id,
                        text=f"❌ Không thể tải HSMT cho gói <b>{ma_tbmt}</b> do hệ thống bên mời thầu bận hoặc lỗi mạng.",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass

        finally:
            PENDING_TASKS.pop(item_id, None)
            DOWNLOAD_QUEUE.task_done()