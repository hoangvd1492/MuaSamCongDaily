import asyncio
import logging
from pathlib import Path
from typing import Dict, Set
from src.playwright.playwright import download_hsmt

logger = logging.getLogger("APP.HSMT_QUEUE")

DOWNLOAD_QUEUE: asyncio.Queue = asyncio.Queue()

# Lưu cấu trúc: {item_id: {chat_id_1, chat_id_2, ...}}
PENDING_TASKS: Dict[str | int, Set[int]] = {}

STORAGE_DIR = Path("storage")
STORAGE_DIR.mkdir(parents=True, exist_ok=True)


def get_existing_hsmt_file(ma_tbmt: str) -> Path | None:
    """Kiểm tra xem file storage/<ma_tbmt>/HSMT.pdf có tồn tại hay không."""
    file_path = STORAGE_DIR / str(ma_tbmt).strip() / "HSMT.pdf"
    return file_path if file_path.is_file() else None


def is_task_pending(task_id: str | int) -> bool:
    return task_id in PENDING_TASKS


async def add_download_task(
    id: str | int,
    ma_tbmt: str,
    chat_id: int,
) -> bool:
    """
    Thêm task tải HSMT vào hàng đợi.
    Nếu đang có tiến trình tải, tự động gom chat_id vào danh sách chờ nhận file.
    """
    # 1. Nếu đã có trong hàng đợi / đang tải: chỉ cần thêm chat_id vào danh sách nhận
    if is_task_pending(id):
        PENDING_TASKS[id].add(chat_id)
        logger.info(f"Đã thêm chat {chat_id} vào danh sách chờ ID: {id} ({ma_tbmt}).")
        return True

    # 2. Nếu là người đầu tiên bấm: tạo mới và đẩy vào Queue
    PENDING_TASKS[id] = {chat_id}
    task_payload = {
        "id": id,
        "ma_tbmt": ma_tbmt,
    }
    await DOWNLOAD_QUEUE.put(task_payload)
    logger.info(f"Đã thêm gói thầu ID: {id} ({ma_tbmt}) vào hàng đợi tải.")
    return True


async def hsmt_download_worker(bot):
    """Worker chạy nền liên tục lấy task từ Queue để xử lý và phân phối file."""
    logger.info("Worker tải HSMT đã sẵn sàng.")
    while True:
        task = await DOWNLOAD_QUEUE.get()
        item_id = task["id"]
        ma_tbmt = str(task["ma_tbmt"]).strip()

        # Lấy toàn bộ danh sách chat_id đang chờ nhận file
        waiting_chat_ids = PENDING_TASKS.get(item_id, set())

        try:
            # 1. Tải bytes blob từ Playwright
            pdf_bytes = await download_hsmt(item_id)
            if not pdf_bytes:
                raise ValueError("Không nhận được dữ liệu PDF từ hệ thống.")

            # 2. Tạo thư mục storage/<ma_tbmt>/ nếu chưa có
            target_dir = STORAGE_DIR / ma_tbmt
            target_dir.mkdir(parents=True, exist_ok=True)

            # 3. Ghi dữ liệu vào file HSMT.pdf
            file_path = target_dir / "HSMT.pdf"
            with open(file_path, "wb") as f:
                f.write(pdf_bytes)

            logger.info(f"Đã lưu file thành công tại: {file_path}")

            # 4. Gửi file cho TẤT CẢ người dùng trong danh sách chờ
            for chat_id in waiting_chat_ids:
                try:
                    with open(file_path, "rb") as f:
                        await bot.send_document(
                            chat_id=chat_id,
                            document=f,
                            filename=f"HSMT_{ma_tbmt}.pdf",
                            caption=f"✅ Đã tải xong HSMT cho gói <b>{ma_tbmt}</b>!",
                            parse_mode="HTML",
                        )
                    logger.info(f"Gửi thành công HSMT ID: {item_id} ({ma_tbmt}) tới chat {chat_id}.")
                except Exception as send_err:
                    logger.error(f"Lỗi gửi file tới chat {chat_id}: {send_err}")

        except Exception as e:
            logger.error(f"Lỗi tải ID {item_id} ({ma_tbmt}): {e}", exc_info=True)
            # Báo lỗi cho tất cả người đang chờ
            for chat_id in waiting_chat_ids:
                try:
                    await bot.send_message(
                        chat_id=chat_id,
                        text=f"❌ Lỗi khi tải HSMT gói {ma_tbmt}: {e}",
                    )
                except Exception:
                    pass

        finally:
            # Xóa khỏi danh sách chờ sau khi đã xử lý xong
            PENDING_TASKS.pop(item_id, None)
            DOWNLOAD_QUEUE.task_done()