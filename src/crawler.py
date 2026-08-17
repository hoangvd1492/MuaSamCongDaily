from src.database.db import get_all_tbmt
from src.telegram.bot import send_telegram
from src.helpers import build_detail_message
from src.telegram.bot import send_message_to_admin
from src.excel.excel import export_tbmt_excel
from src.playwright.playwright import get_server_time
from src.playwright.playwright import get_detail_khlcnt
from src.playwright.playwright import get_detail_tbmt
from src.playwright.playwright import get_list_tbmt
from src.helpers import query_builder
from src.logger import get_logger
from src.playwright.playwright import get_recaptcha_token
import asyncio

from typing import Optional

from src.drive.drive import replace_file


# Import các hàm DB đã viết ở db.py
from src.database.db import (
    check_db_health,
    get_all_keywords,
    get_latest_thoi_gian_sua_tbmt,
    get_tbmt_by_time_range,
    upsert_tbmt,
)

from src.helpers import sleep



logger = get_logger(__name__)





async def set_up_crawler_by_keyword(keyword: str, from_time: Optional[str], to_time: str):
    logger.info("Đang lấy token reCAPTCHA...")
    token = await get_recaptcha_token()
    logger.info("Lấy token reCAPTCHA thành công.")

    current_page = 0
    total_pages = 1
    total_elements = 0
    all_notify_ids = []
    all_items = []

    while current_page < total_pages:
        try:
            logger.info(f"Đang crawl page {current_page + 1}/{total_pages}")

            query_param = query_builder(
                keyword,
                current_page,
                f"{from_time}Z" if from_time else None,
                f"{to_time}Z",
            )
            list_tbmt = await get_list_tbmt(token, query_param)

            if list_tbmt and "page" in list_tbmt:
                page_data = list_tbmt["page"]
                total_pages = page_data.get("totalPages", 1)
                total_elements = page_data.get("totalElements", 0)

                content = page_data.get("content") or []
                logger.info(
                    f"Page {current_page + 1}: {len(content)} bản ghi (Tổng {total_elements})"
                )

                if total_elements == 0:
                    return

                for item in content:
                    notify_id = item.get("notifyId")
                    if notify_id:
                        all_items.append(item)
                        all_notify_ids.append(notify_id)

        except Exception as err:
            logger.warning(f"Page {current_page + 1} failed: {err}")
        finally:
            current_page += 1
            sleep()

    # Loại bỏ ID trùng lặp giữ nguyên thứ tự xuất hiện
    unique_notify_ids = list(dict.fromkeys(all_notify_ids))
    logger.info(
        f"Thu thập {len(all_notify_ids)} notifyId, còn {len(unique_notify_ids)} sau khi loại trùng."
    )

    index = 1
    for notify_id in unique_notify_ids:
        try:
            logger.info(f"[{index}/{len(unique_notify_ids)}] Đang xử lý notifyId={notify_id}")

            tbmt = await get_detail_tbmt(token, notify_id)
            if not tbmt or not tbmt.get("bidoNotifyContractorM"):
                logger.warn(f"notifyId={notify_id}: API trả về null")
                continue

            bido_notify = tbmt.get("bidoNotifyContractorM") or {}
            bid_id = bido_notify.get("bidId")
            plan_id = (tbmt.get("bidpPlanDetail") or {}).get("planId")

            matched_project = None
            khlcnt = None

            if plan_id:
                khlcnt = await get_detail_khlcnt(token, plan_id)
                project_list = (khlcnt or {}).get("bidpPlanDetailToProjectList") or []
                matched_project = next((item for item in project_list if item.get("id") == bid_id), None)

            cur_item = next((x for x in all_items if x.get("notifyId") == notify_id), {})

            # Format thời gian thực hiện gói thầu
            contract_period = bido_notify.get("contractPeriod")
            contract_period_unit = bido_notify.get("contractPeriodUnit", "")
            thoi_gian_thuc_hien = f"{contract_period}{contract_period_unit}" if contract_period is not None else ""

            # Format hiệu lực HSDT
            bid_validity_period = bido_notify.get("bidValidityPeriod")
            bid_validity_unit = bido_notify.get("bidValidityPeriodUnit", "")
            hieu_luc_hsdt = f"{bid_validity_period}{bid_validity_unit}" if bid_validity_period is not None else ""

            contractor_names = cur_item.get("contractorName") or []
            bid_winning_prices = cur_item.get("bidWinningPrice") or []

            upsert_tbmt({
                "id": notify_id,
                "planId": plan_id,
                "maTBMT": bido_notify.get("notifyNo", ""),
                "ngayDangTaiGoc": bido_notify.get("originalPublicDate"),
                "thoiGianSuaTBMT": bido_notify.get("publicDate"),
                "maKHLCNT": bido_notify.get("planNo", ""),
                "tenDuToanMuaSam": bido_notify.get("projectName", ""),
                "quyTrinhApDung": bido_notify.get("processApply", ""),
                "tenGoiThau": bido_notify.get("bidName", ""),
                "chuDauTu": bido_notify.get("investorName", ""),
                "hinhThucLuaChonNhaThau": bido_notify.get("bidForm", ""),
                "linhVuc": bido_notify.get("investField", ""),
                "loaiHopDong": bido_notify.get("contractType", ""),
                "thoiGianThucHienGoiThau": thoi_gian_thuc_hien,
                "thoiDiemDongThau": bido_notify.get("bidCloseDate"),
                "thoiDiemMoThau": bido_notify.get("bidOpenDate"),
                "hieuLucHoSoDuThau": hieu_luc_hsdt,
                "soTienBaoDamDuThau": bido_notify.get("guaranteeValue", ""),
                "duToanMuaSam": (khlcnt or {}).get("bidPoBidpPlanProjectDetailView", {}).get("investTotal", "") if khlcnt else "",
                "giaGoiThau": (matched_project or {}).get("bidPrice", 0) if matched_project else 0,
                "trangThaiTBMT": cur_item.get("statusForNotify", ""),
                "nguoiTrungThau": contractor_names[0] if contractor_names else "",
                "giaTrungThau": bid_winning_prices[0] if bid_winning_prices else "",
                "phuongThucLuaChonNhaThau": bido_notify.get("bidMode", ""),
            })

            logger.info(f"✓ Hoàn thành notifyId={notify_id}")
        except Exception as err:
            logger.warning(f"notifyId={notify_id} failed: {err}")
        finally:
            index += 1
            sleep()

# Khởi tạo khóa dùng chung cho toàn bộ tiến trình cào
crawler_lock = asyncio.Lock()


async def run_crawler(trigger_source: str = "Scheduler") -> bool:
    """Hàm chạy cào dữ liệu được bảo vệ bởi asyncio.Lock().

    :param trigger_source: Nguồn kích hoạt ('Scheduler' hoặc 'Admin Command')
    :return: True nếu cào thành công/đã thực hiện, False nếu bị bỏ qua do đang bận.
    """
    # Kiểm tra xem có tiến trình nào đang chạy không
    if crawler_lock.locked():
        logger.info(f"⏳ [{trigger_source}] Tiến trình Crawler đang bận chạy, bỏ qua lần gọi này.")
        return False

    # Khóa tiến trình lại cho đến khi hoàn thành xong khối 'async with'
    async with crawler_lock:
        logger.info(f"===== START CRAWLER (Trigger: {trigger_source}) =====")
        try:
            is_healthy = check_db_health()
            if not is_healthy:
                logger.warning("❌ Lỗi database, bỏ qua lần crawl này")
                await send_message_to_admin("❌ Lỗi database, bỏ qua lần crawl này")
                return False

            db_keywords = get_all_keywords()
            keywords = list(dict.fromkeys(db_keywords))

            from_time = get_latest_thoi_gian_sua_tbmt()
            to_time = await get_server_time()

            for keyword in keywords:
                logger.info(f"===== START KEYWORD: {keyword} =====")
                try:
                    from_formatted = (
                        from_time.replace(" ", "T")
                        if isinstance(from_time, str)
                        else None
                    )
                    await set_up_crawler_by_keyword(
                        keyword=keyword,
                        from_time=from_formatted,
                        to_time=to_time,
                    )
                    logger.info(f"===== FINISH KEYWORD: {keyword} =====")
                except Exception as err:
                    logger.warning(f'Keyword "{keyword}" failed: {err}')

            logger.info("===== ALL KEYWORDS FINISHED =====")

            rows = get_all_tbmt()
            file_path = export_tbmt_excel(rows, from_time, to_time)
            logger.info(f"===== FINISH EXPORT TBMT EXCEL: {file_path} =====")

            news = get_tbmt_by_time_range(from_time, to_time)
            total_today = len(news)
            logger.info(f"Số lượng thông báo mời thầu mới: {total_today}")

            drive_link = None

            if total_today > 0:
                logger.info("Đang ghi đè file lên Google Drive...")
                drive_result = replace_file(file_path)
            
                # Lấy link xem trực tiếp nếu upload thành công, nếu lỗi/không có ID thì drive_link = None
                drive_link = drive_result.get("webViewLink") if drive_result else None

                logger.info("Đang gửi file báo cáo qua Telegram...")
                caption = f"Hôm nay có {total_today} thông báo mời thầu mới!"
                if drive_link:
                    caption += f"\n\n📁 Google Drive:\n{drive_link}"

                # 1. Gửi file Excel kèm caption (và link Drive nếu có)
                await send_telegram(text=caption, file_path=file_path)

                # 2. Gửi chi tiết từng gói thầu
                for item in news:
                    message, detail_url = build_detail_message(item)
                    reply_markup = None
                    if detail_url:
                        reply_markup = {
                            "inline_keyboard": [
                                [{"text": "🔗 Xem chi tiết TBMT", "url": detail_url}]
                            ]
                        }
                    await send_telegram(text=message, reply_markup=reply_markup)

                logger.info("✅ Gửi báo cáo Telegram thành công!")

            logger.info("===== HOÀN TẤT TIẾN TRÌNH CRAWLER =====")
            return True

        except Exception as err:
            logger.error(f"❌ Lỗi Crawler: {err}", exc_info=True)
            await send_message_to_admin("❌ Có lỗi xảy ra khi crawl dữ liệu!")
            return False