import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

# Thư mục logs/ ở thư mục gốc của project
BASE_DIR = Path(__file__).resolve().parent.parent
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "app.log"

_is_configured = False


def setup_app_logging():
    """Khởi tạo logging cho toàn bộ ứng dụng: Console + File luân phiên 30 ngày."""
    global _is_configured
    if _is_configured:
        return

    log_formatter = logging.Formatter(
        "%(asctime)s - [%(levelname)s] - [%(name)s]: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 1. Ghi ra file app.log (mỗi ngày 1 file lúc 00:00, lưu 30 ngày)
    file_handler = TimedRotatingFileHandler(
        filename=str(LOG_FILE),
        when="midnight",
        interval=1,
        backupCount=30,
        encoding="utf-8",
    )
    file_handler.suffix = "%Y-%m-%d"
    file_handler.setFormatter(log_formatter)
    file_handler.setLevel(logging.INFO)

    # 2. In ra màn hình Terminal
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(log_formatter)
    console_handler.setLevel(logging.INFO)

    # 3. Cấu hình Root Logger (bắt log từ mọi module trong app)
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)

    # Giảm bớt log ồn từ các thư viện HTTP bên ngoài
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.INFO)

    _is_configured = True


def get_logger(name: str) -> logging.Logger:
    """Hàm tiện ích lấy logger theo tên module."""
    setup_app_logging()
    return logging.getLogger(name)