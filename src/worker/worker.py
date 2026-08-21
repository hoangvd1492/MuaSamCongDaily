import asyncio
import logging
from pathlib import Path
from typing import Dict, Set,Optional
from src.playwright.playwright import download_hsmt
from src.notebooklm.notebook import notebooklm_analyse
from src.gemini.gemini import gemini_analyse
from telegram import InputMediaDocument
from src.database.db import get_tbmt_by_id

from src.helpers import (sanitize_name,build_tbmt_context)

logger = logging.getLogger("APP.HSMT_QUEUE")

DOWNLOAD_QUEUE: asyncio.Queue = asyncio.Queue()
PENDING_TASKS: Dict[str | int, Set[int]] = {}

STORAGE_DIR = Path("storage")
STORAGE_DIR.mkdir(parents=True, exist_ok=True)

def get_existing_report_folder(ma_tbmt: str) -> Optional[Path]:
    """Kiểm tra file báo cáo DOCX hợp lệ; trả về Path thư mục nếu tồn tại, ngược lại trả về None."""
    safe_tbmt = sanitize_name(str(ma_tbmt))
    target_dir = STORAGE_DIR / safe_tbmt
    report_path = target_dir / f"{safe_tbmt}_BaoCao.docx"
    
    if report_path.is_file() and report_path.stat().st_size > 0:
        return target_dir
    return None


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
    """Worker chạy nền: tải file, tạo báo cáo (NotebookLM -> Gemini fallback) và gửi toàn bộ qua Telegram."""
    logger.info("Worker HSMT & AI Report đã sẵn sàng nhận tác vụ.")

    while True:
        task = await DOWNLOAD_QUEUE.get()
        item_id = task.get("id")
        ma_tbmt = str(task.get("ma_tbmt", "")).strip()
        waiting_chat_ids = list(PENDING_TASKS.get(item_id, set()))

        safe_tbmt = sanitize_name(ma_tbmt)
        target_dir = STORAGE_DIR / safe_tbmt

        logger.info(f"==> [START] Gói {safe_tbmt} (ID: {item_id}) | Đang chờ: {len(waiting_chat_ids)} user(s)")

        try:
            # 1. TẢI TOÀN BỘ FILE HSMT VỀ FOLDER
            logger.info(f"[{safe_tbmt}] Đang tải dữ liệu hồ sơ mời thầu...")
            success, count = await download_hsmt(item_id, ma_tbmt)
            if not success or count == 0:
                raise ValueError(f"Tải thất bại hoặc không có file nào được lưu cho gói {safe_tbmt}.")
            
            logger.info(f"[{safe_tbmt}] Tải file hoàn tất ({count} files).")
            
            tbmt_detail = get_tbmt_by_id(item_id) if item_id else None
            db_context = build_tbmt_context(tbmt_detail)

            # 2. PHÂN TÍCH VÀ TẠO BÁO CÁO (FALLBACK: NOTEBOOKLM -> GEMINI)
            docx_created = False
            
            # 2.1 Thử qua NotebookLM
            try:
                logger.info(f"[{safe_tbmt}] Bắt đầu phân tích qua NotebookLM...")
                docx_created = await notebooklm_analyse(ma_tbmt,context=db_context)
            except Exception as e_nb:
                logger.warning(f"[{safe_tbmt}] NotebookLM gặp lỗi: {e_nb}")
                docx_created = False

            # 2.2 Fallback sang Gemini nếu NotebookLM thất bại
            if not docx_created:
                logger.warning(f"[{safe_tbmt}] NotebookLM thất bại, chuyển sang Fallback Gemini...")
                try:
                    docx_created = await asyncio.to_thread(gemini_analyse, ma_tbmt,context=db_context)
                except Exception as e_gem:
                    logger.error(f"[{safe_tbmt}] Gemini Fallback cũng gặp lỗi: {e_gem}")
                    docx_created = False

            if docx_created:
                logger.info(f"[{safe_tbmt}] Tạo báo cáo DOCX thành công.")
            else:
                logger.warning(f"[{safe_tbmt}] Không thể tạo báo cáo AI, sẽ chỉ gửi các file HSMT gốc.")

            # 3. QUÉT TẤT CẢ FILE TRONG FOLDER
            downloaded_files = [
                (f.name, f.read_bytes())
                for f in target_dir.iterdir()
                if f.is_file() and f.stat().st_size > 0
            ]

            if not downloaded_files:
                raise ValueError(f"Thư mục {safe_tbmt} rỗng sau khi hoàn tất tải và xử lý.")

            # 4. CHIA NHÓM VÀ GỬI TẤT CẢ FILE (MAX 10 FILE/GROUP)
            CHUNK_SIZE = 10
            file_chunks = [
                downloaded_files[i : i + CHUNK_SIZE]
                for i in range(0, len(downloaded_files), CHUNK_SIZE)
            ]

            logger.info(f"[{safe_tbmt}] Bắt đầu gửi {len(downloaded_files)} files cho {len(waiting_chat_ids)} user(s)...")

            for chat_id in waiting_chat_ids:
                try:
                    for chunk_idx, chunk in enumerate(file_chunks):
                        is_last_chunk = (chunk_idx == len(file_chunks) - 1)
                        media_group = [
                            InputMediaDocument(
                                media=file_bytes,
                                filename=filename,
                                caption=(
                                    f"✅ Hồ sơ & Báo cáo gói thầu <b>{ma_tbmt}</b> ({len(downloaded_files)} file)"
                                    if is_last_chunk and idx == len(chunk) - 1
                                    else None
                                ),
                                parse_mode=(
                                    "HTML"
                                    if is_last_chunk and idx == len(chunk) - 1
                                    else None
                                ),
                            )
                            for idx, (filename, file_bytes) in enumerate(chunk)
                        ]
                        await bot.send_media_group(chat_id=chat_id, media=media_group)
                        await asyncio.sleep(0.5)
                    logger.info(f"[{safe_tbmt}] Đã gửi trọn bộ file thành công cho chat_id: {chat_id}")
                except Exception as send_err:
                    logger.error(f"[{safe_tbmt}] Lỗi gửi Media Group tới chat_id {chat_id}: {send_err}")

            logger.info(f"==> [DONE] Hoàn tất xử lý gói {safe_tbmt}.")

        except Exception as e:
            logger.error(f"❌ [FAILED] Xảy ra lỗi khi xử lý gói {safe_tbmt}: {e}", exc_info=True)
            for chat_id in waiting_chat_ids:
                try:
                    await bot.send_message(
                        chat_id=chat_id,
                        text=f"❌ Không thể tải hồ sơ cho gói <b>{ma_tbmt}</b> do hệ thống bên mời thầu bận hoặc lỗi kết nối.",
                        parse_mode="HTML",
                    )
                except Exception as notify_err:
                    logger.error(f"[{safe_tbmt}] Không thể gửi thông báo lỗi tới {chat_id}: {notify_err}")

        finally:
            PENDING_TASKS.pop(item_id, None)
            DOWNLOAD_QUEUE.task_done()