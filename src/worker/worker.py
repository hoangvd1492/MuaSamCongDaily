import asyncio
import logging
from pathlib import Path
from typing import Dict, Set
from src.playwright.playwright import download_hsmt
from src.notebooklm.notebook import notebooklm_analyse
from src.gemini.gemini import gemini_analyse
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
    """Worker chạy nền xử lý tải, tạo báo cáo và gửi gom nhóm 2 file."""
    logger.info("Worker HSMT & AI Report đã sẵn sàng.")
    
    while True:
        task = await DOWNLOAD_QUEUE.get()
        item_id = task.get("id")
        ma_tbmt = str(task.get("ma_tbmt", "")).strip()
        waiting_chat_ids = list(PENDING_TASKS.get(item_id, set()))

        logger.info(f"==> [START] Bắt đầu xử lý gói {ma_tbmt} (ID: {item_id}) cho {len(waiting_chat_ids)} người dùng đang chờ.")

        target_dir = STORAGE_DIR / ma_tbmt
        pdf_path = target_dir / "HSMT.pdf"
        docx_path = target_dir / "BaoCao.docx"
        temp_pdf_path = target_dir / "HSMT.pdf.tmp"

        try:
            target_dir.mkdir(parents=True, exist_ok=True)

            # -------------------------------------------------------------
            # 1. TẢI FILE PDF (NẾU CHƯA CÓ HOẶC FILE CŨ BỊ LỖI/RỖNG)
            # -------------------------------------------------------------
            if not pdf_path.is_file() or pdf_path.stat().st_size == 0:
                logger.info(f"[{ma_tbmt}] Đang tải dữ liệu PDF từ hệ thống...")
                pdf_bytes = await download_hsmt(item_id)
                if not pdf_bytes or len(pdf_bytes) == 0:
                    raise ValueError("Không nhận được dữ liệu PDF hoặc dữ liệu rỗng.")

                # Ghi file tạm rồi rename nguyên tử để tránh file rác 0 KB
                temp_pdf_path.write_bytes(pdf_bytes)
                temp_pdf_path.replace(pdf_path)

            # -------------------------------------------------------------
            # 2. TẠO BÁO CÁO DOCX (FALLBACK: NOTEBOOKLM -> GEMINI)
            # -------------------------------------------------------------
            docx_success = docx_path.is_file() and docx_path.stat().st_size > 0
            
            if not docx_success:
                # Thử lần 1: NotebookLM (Chạy trong thread riêng tránh block Event Loop)
                try:
                    logger.info(f"[{ma_tbmt}] Thử phân tích qua NotebookLM...")
                    docx_success = await asyncio.to_thread(
                        notebooklm_analyse, str(pdf_path), str(docx_path), ma_tbmt
                    )
                except Exception as e_nb:
                    logger.error(f"Lỗi khi chạy NotebookLM gói {ma_tbmt}: {e_nb}")
                    docx_success = False

                # Fallback lần 2: Gemini
                if not docx_success:
                    logger.warning(f"NotebookLM thất bại, chuyển sang Gemini cho gói {ma_tbmt}...")
                    try:
                        docx_success = await asyncio.to_thread(
                            gemini_analyse, str(pdf_path), str(docx_path), ma_tbmt
                        )
                    except Exception as e_gemini:
                        logger.error(f"Lỗi khi chạy Gemini fallback cho gói {ma_tbmt}: {e_gemini}")
                        docx_success = False

            # -------------------------------------------------------------
            # 3. GỬI FILE CHO TOÀN BỘ NGƯỜI DÙNG ĐANG CHỜ
            # -------------------------------------------------------------
            pdf_data = pdf_path.read_bytes()
            docx_data = docx_path.read_bytes() if docx_success and docx_path.is_file() else None

            for chat_id in waiting_chat_ids:
                try:
                    if docx_success and docx_data:
                        # Gửi Media Group gồm cả 2 file
                        media_group = [
                            InputMediaDocument(
                                media=pdf_data,
                                filename=f"HSMT_{ma_tbmt}.pdf"
                            ),
                            InputMediaDocument(
                                media=docx_data,
                                filename=f"BaoCao_{ma_tbmt}.docx",
                                caption=f"✅ HSMT & Báo cáo phân tích cho gói <b>{ma_tbmt}</b>!",
                                parse_mode="HTML"
                            )
                        ]
                        await bot.send_media_group(chat_id=chat_id, media=media_group)
                    else:
                        # Chỉ gửi file PDF nếu tạo báo cáo thất bại
                        await bot.send_document(
                            chat_id=chat_id,
                            document=pdf_data,
                            filename=f"HSMT_{ma_tbmt}.pdf",
                            caption=f"✅ HSMT cho gói <b>{ma_tbmt}</b> (Báo cáo tóm tắt tạm thời chưa khả dụng).",
                            parse_mode="HTML"
                        )
                except Exception as send_err:
                    logger.error(f"Lỗi gửi file Telegram tới chat {chat_id}: {send_err}")

        except Exception as e:
            # Dọn dẹp file lỗi nếu quá trình tải hỏng
            logger.error(f"Thất bại toàn bộ khi xử lý gói {ma_tbmt}: {e}")
            if temp_pdf_path.is_file():
                temp_pdf_path.unlink(missing_ok=True)
            if pdf_path.is_file() and pdf_path.stat().st_size == 0:
                pdf_path.unlink(missing_ok=True)

            # Báo lỗi về cho tất cả user đang đợi
            for chat_id in waiting_chat_ids:
                try:
                    await bot.send_message(
                        chat_id=chat_id,
                        text=f"❌ Không thể tải HSMT cho gói <b>{ma_tbmt}</b> do hệ thống bên mời thầu bận hoặc lỗi kết nối.",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass

        finally:
            PENDING_TASKS.pop(item_id, None)
            DOWNLOAD_QUEUE.task_done()