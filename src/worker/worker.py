import asyncio
import itertools  # 1. Thêm import itertools
import logging
from pathlib import Path
from typing import Any, Dict, Optional, Set, Union
from src.database.db import get_tbmt_by_id
from src.gemini.gemini import gemini_analyse
from src.helpers import build_tbmt_context, sanitize_name
from src.notebooklm.notebook import notebooklm_analyse
from src.playwright.playwright import download_hsmt
from telegram import InputMediaDocument

logger = logging.getLogger("APP.HSMT_QUEUE")

# Bộ đếm tự tăng để tránh lỗi so sánh dict trong PriorityQueue
_counter = itertools.count()

DOWNLOAD_QUEUE: asyncio.PriorityQueue = asyncio.PriorityQueue()
PENDING_TASKS: Dict[Union[str, int], Set[Union[int, str]]] = {}
TASK_META: Dict[Union[str, int], Dict[str, Any]] = {}

STORAGE_DIR = Path("storage")
STORAGE_DIR.mkdir(parents=True, exist_ok=True)


def get_existing_report_folder(ma_tbmt: str) -> Optional[Path]:
    safe_tbmt = sanitize_name(str(ma_tbmt))
    target_dir = STORAGE_DIR / safe_tbmt
    report_path = target_dir / f"{safe_tbmt}_BaoCao.docx"

    if report_path.is_file() and report_path.stat().st_size > 0:
        return target_dir
    return None


def is_task_pending(task_id: Union[str, int]) -> bool:
    return task_id in PENDING_TASKS


async def add_download_task(
    id: Union[str, int],
    ma_tbmt: str,
    chat_id: Optional[Union[int, str]] = None,
) -> bool:
    if chat_id is not None:
        if id in PENDING_TASKS:
            PENDING_TASKS[id].add(chat_id)
        else:
            PENDING_TASKS[id] = {chat_id}
    else:
        if id not in PENDING_TASKS:
            PENDING_TASKS[id] = set()


    # Cập nhật priority & version
    if id in TASK_META:
        TASK_META[id]["priority"] += 1
        TASK_META[id]["version"] += 1
        new_priority = TASK_META[id]["priority"]
        current_version = TASK_META[id]["version"]
        logger.info(
            f"⬆️ Tăng priority gói {ma_tbmt} (ID: {id}) lên {new_priority} (Version {current_version})"
        )
    else:
        new_priority = 1
        current_version = 1
        TASK_META[id] = {"priority": new_priority, "version": current_version}
        logger.info(
            f"📥 Thêm task mới gói {ma_tbmt} (ID: {id}) với priority = {new_priority}"
        )

    task_payload = {
        "id": id,
        "ma_tbmt": ma_tbmt,
        "version": current_version,
    }

    # Đưa next(_counter) vào tuple: (-priority, current_version, counter, task_payload)
    count = next(_counter)
    await DOWNLOAD_QUEUE.put(
        (-new_priority, current_version, count, task_payload)
    )
    return True


async def hsmt_download_worker(bot):
    logger.info("Worker HSMT & AI Report (Priority Mode) đã sẵn sàng nhận tác vụ.")

    while True:
        # Unpack nhận thêm biến count (hoặc dùng _)
        priority_weight, version, _, task = await DOWNLOAD_QUEUE.get()
        item_id = task.get("id")
        ma_tbmt = str(task.get("ma_tbmt", "")).strip()

        # Kiểm tra tính hợp lệ của task (Bỏ qua phiên bản cũ nếu task đã được nâng priority)
        meta = TASK_META.get(item_id)
        if not meta or meta.get("version") != version:
            logger.debug(
                f"Bỏ qua task trùng lặp / version cũ của gói {ma_tbmt} (ID: {item_id})"
            )
            DOWNLOAD_QUEUE.task_done()
            continue

        actual_priority = -priority_weight
        waiting_chat_ids = [
            cid for cid in PENDING_TASKS.get(item_id, set()) if cid is not None
        ]

        safe_tbmt = sanitize_name(ma_tbmt)
        target_dir = STORAGE_DIR / safe_tbmt

        logger.info(
            f"==> [START] Gói {safe_tbmt} (ID: {item_id}) | Priority: {actual_priority} | Đang chờ: {len(waiting_chat_ids)} user(s)"
        )

        try:
            # 1. Tải toàn bộ file HSMT
            logger.info(f"[{safe_tbmt}] Đang tải dữ liệu hồ sơ mời thầu...")
            success, count = await download_hsmt(item_id, ma_tbmt)
            if not success or count == 0:
                raise ValueError(
                    f"Tải thất bại hoặc không có file nào được lưu cho gói {safe_tbmt}."
                )

            logger.info(f"[{safe_tbmt}] Tải file hoàn tất ({count} files).")

            tbmt_detail = get_tbmt_by_id(item_id) if item_id else None
            db_context = build_tbmt_context(tbmt_detail)

            # 2. Phân tích AI & sinh báo cáo
            docx_created = False
            try:
                logger.info(
                    f"[{safe_tbmt}] Bắt đầu phân tích qua NotebookLM..."
                )
                docx_created = await notebooklm_analyse(
                    ma_tbmt, context=db_context
                )
            except Exception as e_nb:
                logger.warning(f"[{safe_tbmt}] NotebookLM gặp lỗi: {e_nb}")
                docx_created = False

            if not docx_created:
                logger.warning(
                    f"[{safe_tbmt}] NotebookLM thất bại, chuyển sang Fallback Gemini..."
                )
                try:
                    docx_created = await asyncio.to_thread(
                        gemini_analyse, ma_tbmt, context=db_context
                    )
                except Exception as e_gem:
                    logger.error(
                        f"[{safe_tbmt}] Gemini Fallback cũng gặp lỗi: {e_gem}"
                    )
                    docx_created = False

            if docx_created:
                logger.info(f"[{safe_tbmt}] Tạo báo cáo DOCX thành công.")
            else:
                logger.warning(
                    f"[{safe_tbmt}] Không thể tạo báo cáo AI, sẽ chỉ gửi các file HSMT gốc."
                )

            # 3. Nếu không có user nào chờ nhận -> kết thúc (đã lưu sẵn file vào storage)
            if not waiting_chat_ids:
                logger.info(
                    f"[{safe_tbmt}] Không có chat_id nào đăng ký nhận file. Đã lưu sẵn tại {target_dir}."
                )
                continue

            # 4. Gửi file qua Telegram
            downloaded_files = [
                (f.name, f.read_bytes())
                for f in target_dir.iterdir()
                if f.is_file() and f.stat().st_size > 0
            ]

            if not downloaded_files:
                raise ValueError(
                    f"Thư mục {safe_tbmt} rỗng sau khi hoàn tất tải và xử lý."
                )

            CHUNK_SIZE = 10
            file_chunks = [
                downloaded_files[i : i + CHUNK_SIZE]
                for i in range(0, len(downloaded_files), CHUNK_SIZE)
            ]

            logger.info(
                f"[{safe_tbmt}] Bắt đầu gửi {len(downloaded_files)} files cho {len(waiting_chat_ids)} user(s)..."
            )

            for chat_id in waiting_chat_ids:
                try:
                    for chunk_idx, chunk in enumerate(file_chunks):
                        is_last_chunk = chunk_idx == len(file_chunks) - 1
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
                        await bot.send_media_group(
                            chat_id=chat_id, media=media_group
                        )
                        await asyncio.sleep(0.5)

                    logger.info(
                        f"[{safe_tbmt}] Đã gửi trọn bộ file thành công cho chat_id: {chat_id}"
                    )
                except Exception as send_err:
                    logger.error(
                        f"[{safe_tbmt}] Lỗi gửi Media Group tới chat_id {chat_id}: {send_err}"
                    )

            logger.info(f"==> [DONE] Hoàn tất xử lý gói {safe_tbmt}.")

        except Exception as e:
            logger.error(
                f"❌ [FAILED] Xảy ra lỗi khi xử lý gói {safe_tbmt}: {e}",
                exc_info=True,
            )
            for chat_id in waiting_chat_ids:
                try:
                    await bot.send_message(
                        chat_id=chat_id,
                        text=f"❌ Không thể tải hồ sơ cho gói <b>{ma_tbmt}</b> do hệ thống bên mời thầu bận hoặc lỗi kết nối.",
                        parse_mode="HTML",
                    )
                except Exception as notify_err:
                    logger.error(
                        f"[{safe_tbmt}] Không thể gửi thông báo lỗi tới {chat_id}: {notify_err}"
                    )

        finally:
            PENDING_TASKS.pop(item_id, None)
            TASK_META.pop(item_id, None)
            DOWNLOAD_QUEUE.task_done()