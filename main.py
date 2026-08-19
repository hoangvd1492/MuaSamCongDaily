import asyncio
import os
import pytz
from dotenv import load_dotenv

# 1. Load biến môi trường
load_dotenv()

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from src.crawler import run_crawler
from src.database.db import has_tbmt, init_db
from src.logger import get_logger, setup_app_logging
from src.telegram.bot import setup_bot
from src.worker.worker import hsmt_download_worker

# 2. Thiết lập logging
setup_app_logging()
logger = get_logger("APP.MAIN")


async def scheduled_crawler_job():
    """Hàm bọc gọi crawler cho scheduler chạy định kỳ"""
    logger.info("⏰ Bắt đầu tiến trình crawl theo lịch định kỳ...")
    try:
        await run_crawler()
    except Exception as e:
        logger.error(f"Lỗi trong quá trình crawl định kỳ: {e}", exc_info=True)


async def init_scheduler_and_first_run(application=None):
    """Cào dữ liệu lần đầu chạy ngầm và kích hoạt Cron Scheduler."""
    try:
        # 1. Quét dữ liệu lần đầu nếu DB trống (chạy ngầm, không chặn bot)
        if not has_tbmt():
            logger.info("Database chưa có TBMT, tiến hành crawl dữ liệu lần đầu...")
            await run_crawler()
            logger.info("✅ Đã crawl xong dữ liệu lần đầu!")
        else:
            logger.info("Database đã có dữ liệu TBMT, bỏ qua bước crawl ban đầu.")

        # 2. Cấu hình và khởi động Scheduler
        schedule_cron = os.getenv("CRON_SCHEDULE", "1 12 * * *")
        tz = pytz.timezone("Asia/Ho_Chi_Minh")

        scheduler = AsyncIOScheduler()
        scheduler.add_job(
            scheduled_crawler_job,
            CronTrigger.from_crontab(schedule_cron, timezone=tz),
            misfire_grace_time=120,
            coalesce=True,
            id="tbmt_crawler_job",
            replace_existing=True,
        )
        scheduler.start()

        # Lưu tham chiếu để tránh bị Garbage Collection thu hồi
        if application:
            application.bot_data["scheduler"] = scheduler

        logger.info(
            f"✅ Cron scheduler đã khởi động với cấu hình: '{schedule_cron}' (Asia/Ho_Chi_Minh)"
        )
    except Exception as e:
        logger.error(f"Lỗi khởi tạo Crawler/Scheduler nền: {e}", exc_info=True)


def main():
    logger.info("=== BẮT ĐẦU KHỞI CHẠY ỨNG DỤNG ===")

    # 1. Khởi tạo database
    logger.info("Khởi tạo cấu trúc Database...")
    init_db()

    # 2. Khởi tạo ứng dụng Bot Telegram
    logger.info("Khởi tạo dịch vụ Telegram Bot...")
    app = setup_bot()

    # 3. Hook post_init: Nạp các tác vụ ngầm vào Event Loop
    async def post_init(application):
        # [Task 1] Khởi chạy Worker tải HSMT & Báo cáo AI trong nền
        asyncio.create_task(hsmt_download_worker(application.bot))
        logger.info("✅ Worker tải HSMT & AI đã được khởi chạy ngầm.")

        # [Task 2] Khởi chạy Crawler lần đầu & Scheduler trong nền
        asyncio.create_task(init_scheduler_and_first_run(application))
        logger.info("✅ Tiến trình Crawler & Scheduler đã được khởi chạy ngầm.")

    # 4. Hook post_shutdown: Dọn dẹp tài nguyên khi tắt ứng dụng
    async def post_shutdown(application):
        scheduler = application.bot_data.get("scheduler")
        if scheduler and scheduler.running:
            scheduler.shutdown(wait=False)
            logger.info("✅ Đã đóng Scheduler an toàn.")

    app.post_init = post_init
    app.post_shutdown = post_shutdown

    # 5. Khởi động vòng lặp nhận tin nhắn Telegram
    logger.info("Ứng dụng đang khởi chạy và lắng nghe tin nhắn...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()