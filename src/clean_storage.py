import logging
from pathlib import Path
import shutil
import time

logger = logging.getLogger("APP.CLEANER")
STORAGE_DIR = Path("storage")


def clean_old_storage_folders(days_threshold: float = 30):
    """Quét và xóa các thư mục trong STORAGE_DIR có thời gian sửa đổi/tạo cũ hơn

    `days_threshold` ngày.
    """
    if not STORAGE_DIR.exists() or not STORAGE_DIR.is_dir():
        return

    # Quy đổi số ngày ra giây (1 ngày = 86400 giây)
    now = time.time()
    cutoff_time = now - (days_threshold * 86400)

    deleted_count = 0

    for item in STORAGE_DIR.iterdir():
        if item.is_dir():
            try:
                # Lấy thời gian chỉnh sửa/tạo gần nhất của thư mục
                stat = item.stat()
                folder_mtime = stat.st_mtime

                if folder_mtime < cutoff_time:
                    logger.info(
                        f"🗑️ Đang xóa thư mục cũ (> {days_threshold} ngày): {item.name}"
                    )
                    shutil.rmtree(item)
                    deleted_count += 1
            except Exception as e:
                logger.error(
                    f"❌ Lỗi khi xóa thư mục {item.name}: {e}", exc_info=True
                )

    if deleted_count > 0:
        logger.info(
            f"✅ Hoàn tất dọn dẹp: Đã xóa {deleted_count} thư mục cũ trong storage."
        )