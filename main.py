import os
import asyncio
from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

# 1. Load biến môi trường
load_dotenv()

# 2. Khởi tạo logger
from src.logger import get_logger, setup_app_logging

setup_app_logging()
logger = get_logger("APP.MAIN")

from src.telegram.bot import setup_bot
from src.database.db import init_db, has_tbmt
from src.crawler import run_crawler  

from src.playwright.playwright import get_server_time


async def scheduled_crawler_job():
    """Hàm bọc gọi crawler cho scheduler"""
    logger.info("⏰ Bắt đầu tiến trình crawl theo lịch định kỳ...")
    await run_crawler()


async def init_scheduler_and_first_run():
    """Crawl lần đầu (chờ hoàn thành xong) rồi mới bật Cron Scheduler"""
    # 1. Nếu DB chưa có dữ liệu -> BẮT BUỘC ĐỢI CRAWL XONG (dùng await thay vì create_task)
    if not has_tbmt():
        logger.info("Database chưa có TBMT, tiến hành crawl dữ liệu lần đầu (đang chờ hoàn thành)...")
        await run_crawler()
        logger.info("✅ Đã crawl xong dữ liệu lần đầu!")
    else:
        logger.info("Database đã có dữ liệu TBMT, bỏ qua bước crawl ban đầu.")

    # 2. Khởi động Cron Scheduler sau khi đã có dữ liệu
    schedule_cron = os.getenv("CRON_SCHEDULE", "1 12 * * *")
    tz = pytz.timezone("Asia/Ho_Chi_Minh")

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        scheduled_crawler_job,
        CronTrigger.from_crontab(schedule_cron, timezone=tz),
        id="tbmt_crawler_job",
        replace_existing=True,
    )
    scheduler.start()
    logger.info(f"✅ Cron scheduler đã khởi động với cấu hình: '{schedule_cron}' (Asia/Ho_Chi_Minh)")


def main():
    
    logger.info("=== BẮT ĐẦU KHỞI CHẠY ỨNG DỤNG ===")



    logger.info("Khởi tạo cấu trúc Database...")
    init_db()

    logger.info("Khởi tạo dịch vụ Telegram Bot...")
    app = setup_bot()

    # Chạy lần đầu & scheduler trong hook post_init
    async def post_init(application):
        await init_scheduler_and_first_run()

    app.post_init = post_init

    logger.info("Ứng dụng đang khởi chạy...")
    app.run_polling()


if __name__ == "__main__":
    main()