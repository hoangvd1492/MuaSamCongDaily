import os
from pathlib import Path
from typing import Any, Dict, Optional, Union
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from src.logger import get_logger



logger = get_logger(__name__)

# Gán cứng trực tiếp tên file JSON ở thư mục gốc (không dùng env)
SERVICE_ACCOUNT_FILE = "mscidsc-c0a0739eb6e1.json"

GOOGLE_DRIVE_FILE_ID = os.getenv("GOOGLE_DRIVE_FILE_ID", "")
SCOPES = ["https://www.googleapis.com/auth/drive"]


def get_drive_service():
    """Khởi tạo và trả về đối tượng Google Drive Service."""
    if not os.path.exists(SERVICE_ACCOUNT_FILE):
        logger.error(f"❌ Không tìm thấy file chứng thực Service Account tại: {SERVICE_ACCOUNT_FILE}")
        return None

    try:
        credentials = service_account.Credentials.from_service_account_file(
            SERVICE_ACCOUNT_FILE, scopes=SCOPES
        )
        return build("drive", "v3", credentials=credentials)
    except Exception as e:
        logger.error(f"❌ Lỗi khởi tạo Drive Service: {e}")
        return None


def replace_file(local_file_path: Union[str, Path]) -> Optional[Dict[str, Any]]:
    """Tải lên và ghi đè file local lên Google Drive.
    
    :param local_file_path: Đường dẫn file (dạng str hoặc pathlib.Path)
    :return: Dict thông tin file (id, name, modifiedTime, webViewLink) hoặc None nếu lỗi
    """
    # 1. Kiểm tra File ID trong .env
    if not GOOGLE_DRIVE_FILE_ID or not GOOGLE_DRIVE_FILE_ID.strip():
        logger.warning("⚠️ Bỏ qua ghi đè Drive: Chưa cấu hình GOOGLE_DRIVE_FILE_ID trong file .env!")
        return None

    # 2. Chuẩn hóa đường dẫn về string và kiểm tra tồn tại
    local_path_str = str(local_file_path)
    if not os.path.exists(local_path_str):
        logger.error(f"❌ Bỏ qua ghi đè Drive: Không tìm thấy file nguồn tại '{local_path_str}'.")
        return None

    try:
        drive_service = get_drive_service()
        if not drive_service:
            return None

        media = MediaFileUpload(
            local_path_str,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            resumable=True,
        )

        logger.info(f"🚀 Đang ghi đè file '{local_path_str}' lên Google Drive (ID: {GOOGLE_DRIVE_FILE_ID})...")

        updated_file = drive_service.files().update(
            fileId=GOOGLE_DRIVE_FILE_ID,
            media_body=media,
            fields="id, name, modifiedTime, webViewLink",
        ).execute()

        logger.info(f"✅ Ghi đè file thành công: {updated_file.get('name')} (ID: {updated_file.get('id')})")
        return updated_file

    except Exception as e:
        logger.error(f"❌ Ghi đè Google Drive thất bại: {e}", exc_info=True)
        return None

