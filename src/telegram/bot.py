import functools
import json
import os
from pathlib import Path
import asyncio
from curl_cffi.requests import AsyncSession
from curl_cffi.curl import CurlMime

from zoneinfo import ZoneInfo  # Python 3.9+
from croniter import croniter
from datetime import datetime
import traceback

import io
from cachetools import TTLCache
from telegram.request import HTTPXRequest
import time

from telegram import InputMediaDocument, Update
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)



from src.worker.worker import (
    is_task_pending,
    add_download_task,
    get_existing_report_folder
)

from src.database.db import (
    add_keyword,
    get_all_keywords,
    get_all_subscribers,
    get_subscriber_role,
    remove_keyword,
    remove_subscriber,
    upsert_subscriber,
    is_tbmt_valid,
    get_tbmt_by_time_range,
    get_tbmt_by_maTBMT
)
from src.logger import get_logger

from src.playwright.playwright import get_server_time

from src.helpers import build_detail_message


logger = get_logger("APP.BOT")

download_cooldown_cache = TTLCache(maxsize=5000, ttl=7)


# ==========================================================
# ADMIN
# ==========================================================


def get_super_admin_ids() -> list[str]:
    raw_admin_ids = os.getenv("SUPER_ADMIN_IDS", "")

    return [
        cid.strip()
        for cid in raw_admin_ids.split(";")
        if cid.strip()
    ]


def is_admin(chat_id: str) -> bool:
    """
    Kiểm tra quyền:
    - Super Admin
    - Hoặc user có role='admin' trong database
    """

    if chat_id in get_super_admin_ids():
        return True

    return get_subscriber_role(chat_id) == "admin"


# ==========================================================
# ADMIN DECORATOR
# ==========================================================


def admin_required(func):
    """
    Decorator chặn người dùng không phải Admin.
    Kiểm tra dựa trên ID người gửi (effective_user.id).
    """

    @functools.wraps(func)
    async def wrapper(
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        *args,
        **kwargs,
    ):
        user = update.effective_user
        message = update.effective_message

        # Kiểm tra nếu update không chứa thông tin người gửi (ví dụ: kênh ẩn danh, service updates)
        if user is None:
            logger.warning(
                f"Telegram update không có effective_user. Update ID={update.update_id}"
            )
            return

        user_id = str(user.id)

        # Kiểm tra quyền Admin dựa trên User ID
        if not is_admin(user_id):
            logger.warning(
                f"Người dùng {user_id} (@{user.username}) cố ý truy cập lệnh admin trái phép."
            )

            if message:
                await message.reply_text(
                    "⛔ Bạn không có quyền Admin để thực hiện lệnh này."
                )
            return

        return await func(
            update,
            context,
            *args,
            **kwargs,
        )

    return wrapper
# ==========================================================
# PUBLIC COMMANDS
# ==========================================================


async def start_cmd(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if message is None or chat is None or user is None:
        logger.warning(
            f"/start không có đủ thông tin update. "
            f"Update ID={update.update_id}"
        )
        return

    chat_id = str(chat.id)

    username = (
        f"@{user.username}"
        if user.username
        else user.full_name
    )

    logger.info(
        f"User gọi /start: {username} "
        f"(ID: {chat_id})"
    )

    msg = (
        f"👋 Xin chào **{user.first_name}**!\n\n"
        f"🆔 **Chat ID của bạn:** `{chat_id}`\n\n"
        f"📌 Hãy sao chép Chat ID ở trên và gửi cho "
        f"**Admin** để được cấp quyền."
    )

    await message.reply_text(
        msg,
        parse_mode="Markdown",
    )


async def help_cmd(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message
    user = update.effective_user  # Lấy trực tiếp thông tin người gửi

    if message is None or user is None:
        logger.warning(
            f"/help không có message/user. "
            f"Update ID={update.update_id}"
        )
        return

    # Lấy ID của chính người gửi (dạng chuỗi)
    user_id = str(user.id)

    # Kiểm tra xem người gửi có phải là Admin hay không
    user_is_admin = is_admin(user_id)

    help_text = (
        "📖 **DANH SÁCH LỆNH**\n\n"
        "👤 **Lệnh chung:**\n"
        "• `/start` : Lấy Chat ID của bạn\n"
        "• `/help` : Hiển thị hướng dẫn\n"
        "• `/moinhat` : Lấy lại thông tin gói thầu lần quét dữ liệu gần nhất\n"
        "• `/goithau maHSMT` : Tìm lại thông tin theo mã HSMT\n"
    )

    if user_is_admin:
        help_text += (
            "\n🛡 **Lệnh Quản trị (Admin):**\n"
            "• `/crawl` : Chạy cào dữ liệu ngay lập tức\n"
            "• `/adduser <chat_id> <username> [role]` : Thêm user (role: `admin` hoặc `user`)\n"
            "• `/removeuser <chat_id>` : Xóa user\n"
            "• `/listuser` : Danh sách user\n"
            "• `/addkw <từ_khóa>` : Thêm từ khóa theo dõi\n"
            "• `/removekw <từ_khóa>` : Xóa từ khóa\n"
            "• `/listkw` : Danh sách từ khóa\n"
        )

    await message.reply_text(
        help_text,
        parse_mode="Markdown",
    )
    
    
async def get_news(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Lấy chat_id của người/nhóm vừa gửi lệnh
    to = update.effective_chat.id if update and update.effective_chat else None

    try:
        # 1. Tính toán mốc thời gian crawl gần nhất
        schedule_cron = os.getenv("CRON_SCHEDULE", "1 12 * * *")
        tz = ZoneInfo("Asia/Ho_Chi_Minh")
        now = datetime.now(tz)

        iter_cron = croniter(schedule_cron, now)

        # Lần 1: Lấy mốc cào gần nhất trong quá khứ (to_time)
        last_run: datetime = iter_cron.get_prev(datetime)
        to_time = last_run.strftime("%Y-%m-%dT%H:%M:%S")

        # Lần 2: Lùi tiếp 1 chu kỳ để lấy mốc cào thứ 2 trước đó (from_time)
        prev_run: datetime = iter_cron.get_prev(datetime)
        from_time = prev_run.strftime("%Y-%m-%dT%H:%M:%S")

        # 2. Lấy dữ liệu bài viết/gói thầu mới
        news = get_tbmt_by_time_range(from_time, to_time)
        total_today = len(news) if news else 0
        logger.info(f"Số lượng thông báo mời thầu mới: {total_today}")

        # Tin đầu tiên: Thông báo tổng số lượng mời thầu mới
        first_message = f"Hôm nay có {total_today} thông báo mời thầu mới!"
        await send_telegram(text=first_message, to=to)

        # 3. Gửi chi tiết từng gói thầu nếu có dữ liệu
        if total_today > 0:
            for item in news:
                try:
                    message, detail_url = build_detail_message(item)

                    item_id = item.get("id")
                    ma_tbmt = item.get("maTBMT") or item.get("ma_tbmt")

                    buttons = []

                    # Nút 1: Link xem chi tiết
                    if detail_url:
                        buttons.append(
                            {"text": " Xem chi tiết TBMT", "url": detail_url}
                        )

                    # Nút 2: Nút thủ công dự phòng để tải/gửi lại
                    if item_id and ma_tbmt:
                        buttons.append(
                            {
                                "text": " Phân tích HSMT",
                                "callback_data": f"download_hsmt:{item_id}:{ma_tbmt}",
                            }
                        )

                    reply_markup = None
                    if buttons:
                        reply_markup = {"inline_keyboard": [buttons]}

                    await send_telegram(
                        text=message,
                        reply_markup=reply_markup,
                        to=to,
                    )

                except Exception as item_err:
                    logger.error(
                        f"Lỗi khi xử lý/gửi gói thầu {item.get('maTBMT')}: {item_err}"
                    )
                    continue

            logger.info(" Gửi báo cáo Telegram thành công!")
        return True

    except Exception as e:
        error_msg = f"❌ Đã xảy ra lỗi trong quá trình lấy tin mới: {str(e)}"
        logger.error(f"{error_msg}\n{traceback.format_exc()}")
        try:
            await send_telegram(text=error_msg, to=to)
        except Exception:
            pass
        return False


async def get_one(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Lấy chat_id của người gửi
    to = update.effective_chat.id if update and update.effective_chat else None

    try:
        # 1. Kiểm tra tham số người dùng nhập vào (ví dụ: /get_one IB2400012345)
        if not context.args or len(context.args) == 0:
            await send_telegram(
                text="⚠️ Vui lòng nhập mã TBMT cần tra cứu.\n\n<i>Cú pháp:</i> <code>/get_one &lt;mã_TBMT&gt;</code>",
                to=to,
            )
            return

        ma_tbmt_input = context.args[0].strip()

        # 2. Truy vấn dữ liệu từ database bằng hàm get_tbmt_by_maTBMT
        item = get_tbmt_by_maTBMT(ma_tbmt_input)

        if not item:
            await send_telegram(
                text=f"❌ Không tìm thấy thông tin cho gói thầu <b>{ma_tbmt_input}</b> trong hệ thống!",
                to=to,
            )
            return

        # 3. Tạo nội dung tin nhắn và các nút bấm tương tự get_news
        message, detail_url = build_detail_message(item)

        item_id = item.get("id")
        ma_tbmt = item.get("maTBMT") or item.get("ma_tbmt")

        buttons = []

        # Nút 1: Link xem chi tiết
        if detail_url:
            buttons.append(
                {"text": " Xem chi tiết TBMT", "url": detail_url}
            )

        # Nút 2: Nút thủ công tải/phân tích HSMT
        if item_id and ma_tbmt:
            buttons.append(
                {
                    "text": " Phân tích HSMT",
                    "callback_data": f"download_hsmt:{item_id}:{ma_tbmt}",
                }
            )

        reply_markup = None
        if buttons:
            reply_markup = {"inline_keyboard": [buttons]}

        # 4. Gửi kết quả về Telegram
        await send_telegram(
            text=message,
            reply_markup=reply_markup,
            to=to,
        )
        return True

    except Exception as e:
        error_msg = f"❌ Đã xảy ra lỗi khi tra cứu gói thầu: {str(e)}"
        logger.error(f"{error_msg}\n{traceback.format_exc()}")
        try:
            await send_telegram(text=error_msg, to=to)
        except Exception:
            pass
        return False

# ==========================================================
# USER MANAGEMENT
# ==========================================================


@admin_required
async def add_user_cmd(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message

    if message is None:
        return

    if len(context.args) < 2:
        await message.reply_text(
            "⚠️ **Cú pháp:** "
            "`/adduser <chat_id> <username> [role]`\n\n"
            "**Ví dụ:**\n"
            "• `/adduser 123456789 van_a`\n"
            "• `/adduser 987654321 @van_b admin`",
            parse_mode="Markdown",
        )
        return

    # Giữ nguyên username người dùng nhập
    # Không thêm / xóa ký tự @
    target_id = context.args[0].strip()
    target_username = context.args[1].strip()

    role = "user"

    if (
        len(context.args) >= 3
        and context.args[2].lower() in ["admin", "user"]
    ):
        role = context.args[2].lower()

    upsert_subscriber(
        chat_id=target_id,
        username=target_username,
        role=role,
    )

    logger.info(
        f"Admin {update.effective_chat.id} "
        f"đã thêm user: "
        f"ID={target_id}, "
        f"Username={target_username}, "
        f"Role={role}"
    )

    await message.reply_text(
        f"✅ **Đã thêm người dùng thành công!**\n\n"
        f"• **Chat ID:** `{target_id}`\n"
        f"• **Username:** `{target_username}`\n"
        f"• **Vai trò:** **{role}**",
        parse_mode="Markdown",
    )


@admin_required
async def remove_user_cmd(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message

    if message is None:
        return

    if not context.args:
        await message.reply_text(
            "⚠️ Cú pháp: `/removeuser <chat_id>`"
        )
        return

    target_id = context.args[0].strip()

    if target_id in get_super_admin_ids():
        await message.reply_text(
            "⛔ Không thể xóa Super Admin."
        )
        return

    if remove_subscriber(target_id):
        logger.info(
            f"Admin đã xóa user: {target_id}"
        )

        await message.reply_text(
            f"✅ Đã xóa user `{target_id}` "
            f"khỏi hệ thống.",
            parse_mode="Markdown",
        )
    else:
        await message.reply_text(
            f"❌ Không tìm thấy user `{target_id}` "
            f"trong database."
        )


@admin_required
async def list_user_cmd(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message

    if message is None:
        return

    users = get_all_subscribers()

    if not users:
        await message.reply_text(
            "Chưa có user nào trong hệ thống."
        )
        return

    lines = [
        "👥 **DANH SÁCH NGƯỜI DÙNG:**\n"
    ]

    super_ids = get_super_admin_ids()

    for idx, user in enumerate(users, 1):
        is_super = (
            " *(SuperAdmin)*"
            if user["chat_id"] in super_ids
            else ""
        )

        # Giữ nguyên username trong DB
        username = (
            user["username"]
            if user["username"]
            else "Chưa có"
        )

        lines.append(
            f"{idx}. `{user['chat_id']}` | "
            f"{username} | "
            f"Role: **{user['role']}**"
            f"{is_super}"
        )

    await message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
    )


# ==========================================================
# KEYWORD MANAGEMENT
# ==========================================================


@admin_required
async def add_kw_cmd(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message

    if message is None:
        return

    if not context.args:
        await message.reply_text(
            "⚠️ Cú pháp: `/addkw <từ khóa>`"
        )
        return

    kw = " ".join(context.args).strip()

    if not kw:
        await message.reply_text(
            "⚠️ Từ khóa không được để trống."
        )
        return

    if add_keyword(kw):
        logger.info(
            f"Admin thêm từ khóa: {kw}"
        )

        await message.reply_text(
            f"✅ Đã thêm từ khóa theo dõi: `{kw}`",
            parse_mode="Markdown",
        )
    else:
        await message.reply_text(
            f"⚠️ Từ khóa `{kw}` "
            f"đã tồn tại trong danh sách.",
            parse_mode="Markdown",
        )


@admin_required
async def remove_kw_cmd(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message

    if message is None:
        return

    if not context.args:
        await message.reply_text(
            "⚠️ Cú pháp: `/removekw <từ khóa>`"
        )
        return

    kw = " ".join(context.args).strip()

    if not kw:
        await message.reply_text(
            "⚠️ Từ khóa không được để trống."
        )
        return

    if remove_keyword(kw):
        logger.info(
            f"Admin xóa từ khóa: {kw}"
        )

        await message.reply_text(
            f"✅ Đã xóa từ khóa: `{kw}`",
            parse_mode="Markdown",
        )
    else:
        await message.reply_text(
            f"❌ Không tìm thấy từ khóa "
            f"`{kw}` để xóa."
        )


@admin_required
async def list_kw_cmd(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message

    if message is None:
        return

    keywords = get_all_keywords()

    if not keywords:
        await message.reply_text(
            "Chưa có từ khóa nào được thiết lập."
        )
        return

    lines = [
        "🔑 **DANH SÁCH TỪ KHÓA THEO DÕI:**\n"
    ]

    for idx, kw in enumerate(keywords, 1):
        lines.append(
            f"{idx}. `{kw}`"
        )

    await message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
    )
    
@admin_required
async def crawl_cmd(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message
    if message is None:
        return

    from src.crawler import crawler_lock, run_crawler

    if crawler_lock.locked():
        await message.reply_text("⏳ Tiến trình đang bận chạy, vui lòng đợi!")
        return

    await message.reply_text(
        "🚀 Đã kích hoạt cào dữ liệu ngầm!"
    )

    # Đẩy tác vụ chạy ngầm -> Giải phóng handler ngay lập tức
    asyncio.create_task(run_crawler(trigger_source="Admin Command"))
# ==========================================================
# ERROR HANDLER
# ==========================================================


async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):
    error = context.error

    logger.exception(
        f"Telegram Bot Exception: {error}"
    )


async def send_message_to_admin(text: str):
    """Gửi tin nhắn cảnh báo/lỗi đến các Super Admin bằng curl_cffi Async."""
    bot_token = os.getenv("BOT_TOKEN")
    if not bot_token:
        raise ValueError("Chưa thiết lập BOT_TOKEN trong file .env!")

    admin_ids = get_super_admin_ids()
    if not admin_ids:
        logger.warning("⚠️ Không tìm thấy SUPER_ADMIN_IDS nào để gửi thông báo!")
        return

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

    async with AsyncSession(impersonate="chrome") as session:
        for chat_id in admin_ids:
            try:
                payload = {
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                }
                response = await session.post(
                    url,
                    json=payload,
                    timeout=10,
                )
                result = response.json()
                if not result.get("ok"):
                    logger.error(
                        f"❌ Gửi tin nhắn thất bại tới Super Admin {chat_id}: {result.get('description')}"
                    )
            except Exception as e:
                logger.error(f"❌ Lỗi khi gửi tin nhắn tới Super Admin {chat_id}: {e}")


async def send_telegram(
    text: str = "",
    file_path: str | Path | None = None,
    reply_markup: dict | None = None,
    to: str | int | list[str | int] | None = None,
):
    """Gửi tin nhắn văn bản hoặc file kèm inline button tới 'to' hoặc tất cả subscribers."""
    bot_token = os.getenv("BOT_TOKEN")
    if not bot_token:
        raise ValueError("Chưa thiết lập BOT_TOKEN trong file .env!")

    # Xác định danh sách chat_id nhận tin
    if to is not None:
        if isinstance(to, list):
            target_ids = [str(chat_id) for chat_id in to]
        else:
            target_ids = [str(to)]
    else:
        subscribers = get_all_subscribers()
        if not subscribers:
            logger.warning("⚠️ Không có subscriber nào trong hệ thống!")
            return
        target_ids = [str(sub["chat_id"]) for sub in subscribers]

    base_url = f"https://api.telegram.org/bot{bot_token}"

    async with AsyncSession(impersonate="chrome") as session:
        for chat_id in target_ids:
            try:
                if file_path and os.path.exists(file_path):
                    url = f"{base_url}/sendDocument"
                    path_obj = Path(file_path)

                    mp = CurlMime()
                    mp.addpart(name="chat_id", data=chat_id)
                    if text:
                        mp.addpart(name="caption", data=text)
                        mp.addpart(name="parse_mode", data="HTML")
                    if reply_markup:
                        mp.addpart(
                            name="reply_markup",
                            data=json.dumps(reply_markup),
                        )
                    mp.addpart(
                        name="document",
                        filename=path_obj.name,
                        local_path=str(path_obj),
                    )

                    response = await session.post(url, multipart=mp, timeout=30)
                else:
                    url = f"{base_url}/sendMessage"
                    payload = {
                        "chat_id": chat_id,
                        "text": text,
                        "parse_mode": "HTML",
                    }
                    if reply_markup:
                        payload["reply_markup"] = reply_markup

                    response = await session.post(
                        url, json=payload, timeout=10
                    )

                result = response.json()
                if not result.get("ok"):
                    logger.error(
                        f"❌ Gửi Telegram thất bại {chat_id}: {result.get('description')}"
                    )

            except Exception as error:
                logger.error(f"❌ Gửi Telegram thất bại {chat_id}: {error}")

async def handle_download_hsmt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    callback_data = query.data
    if not callback_data or not callback_data.startswith("download_hsmt:"):
        return

    # Tách dữ liệu: "download_hsmt:id:ma_tbmt"
    parts = callback_data.split(":")
    if len(parts) != 3:
        return
    
    

    _, item_id, ma_tbmt = parts
    chat_id = query.message.chat_id
    user_id = query.from_user.id

    # 1. KIỂM TRA CHỐNG SPAM BẰNG CACHETOOLS
    lock_key = f"{user_id}:{ma_tbmt}"
    if lock_key in download_cooldown_cache:
        # Nếu đang trong thời gian cooldown (7s), cảnh báo toast nhẹ và dừng xử lý
        try:
            await query.answer(
                text="⚠️ Bạn thao tác quá nhanh, vui lòng đợi vài giây!",
                show_alert=False,
            )
        except TelegramError:
            pass
        return

    # Lưu key vào cache để kích hoạt cooldown
    download_cooldown_cache[lock_key] = time.time()

    # Tắt hiệu ứng quay tròn trên nút bấm
    try:
        await query.answer()
    except TelegramError:
        pass

    # 2. KIỂM TRA TÍNH HỢP LỆ
    if not is_tbmt_valid(item_id=item_id, ma_tbmt=ma_tbmt):
        await context.bot.send_message(
            chat_id=chat_id,
            text="❌ Thông tin gói thầu không hợp lệ hoặc không tồn tại trong hệ thống!",
            parse_mode="HTML",
        )
        return

    # 3. KIỂM TRA THƯ MỤC VÀ GỬI FILE NẾU ĐÃ CÓ SẴN
    folder_path = get_existing_report_folder(ma_tbmt)


    if folder_path and folder_path.is_dir():
        files = [
            p for p in folder_path.iterdir() if p.is_file() and p.stat().st_size > 0
        ]

        if files:
            chunk_size = 10
            for i in range(0, len(files), chunk_size):
                chunk = files[i : i + chunk_size]
                media_group = []
                file_handles = []

                for idx, file_p in enumerate(chunk):
                    f = open(file_p, "rb")
                    file_handles.append(f)

                    # Kiểm tra nếu là file cuối cùng của toàn bộ danh sách
                    is_last_file = (i + idx == len(files) - 1)

                    caption = (
                        f"✅ Hồ sơ gói thầu <b>{ma_tbmt}</b> ({len(files)} files)"
                        if is_last_file
                        else None
                    )

                    media_group.append(
                        InputMediaDocument(
                            media=f,
                            filename=file_p.name,
                            caption=caption,
                            parse_mode="HTML",
                        )
                    )

                try:
                    await context.bot.send_media_group(
                        chat_id=chat_id, media=media_group
                    )
                finally:
                    for f in file_handles:
                        f.close()

            return

    # 4. TRƯỜNG HỢP CHƯA CÓ FILE -> ĐƯA VÀO HÀNG ĐỢI
    already_pending = is_task_pending(item_id)


    if already_pending:
        await add_download_task(id=item_id, ma_tbmt=ma_tbmt, chat_id=chat_id)
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"⏳ Gói <b>{ma_tbmt}</b> đang trong hàng đợi. Bot sẽ gửi file cho bạn ngay khi hoàn tất!",
            parse_mode="HTML",
        )
    else:
        # Chỉ tạo task mới khi gói này chưa có trong hàng đợi
        await add_download_task(id=item_id, ma_tbmt=ma_tbmt, chat_id=chat_id)
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"📥 Đã thêm yêu cầu tải HSMT <b>{ma_tbmt}</b> vào hàng đợi xử lý.",
            parse_mode="HTML",
        )
# ==========================================================
# SETUP BOT
# ==========================================================


def setup_bot() -> Application:

    bot_token = os.getenv("BOT_TOKEN")
    
    request = HTTPXRequest(
    connect_timeout=30.0,
    read_timeout=120.0,
    write_timeout=120.0,
    pool_timeout=30.0,
    )

    if not bot_token:
        raise ValueError(
            "Chưa thiết lập BOT_TOKEN trong file .env!"
        )

    app = (
        ApplicationBuilder()
        .token(bot_token)
        .request(request)
        .build()
    )

    # --------------------------------------------------
    # Public
    # --------------------------------------------------

    app.add_handler(
        CommandHandler("start", start_cmd)
    )

    app.add_handler(
        CommandHandler("help", help_cmd)
    )
    
    app.add_handler(
            CommandHandler("moinhat", get_news)
        )
    
    app.add_handler(
                CommandHandler("goithau", get_one)
            )

    # --------------------------------------------------
    # User Management
    # --------------------------------------------------

    app.add_handler(
        CommandHandler("adduser", add_user_cmd)
    )

    app.add_handler(
        CommandHandler("removeuser", remove_user_cmd)
    )

    app.add_handler(
        CommandHandler("listuser", list_user_cmd)
    )
    
   

    # --------------------------------------------------
    # Keyword Management
    # --------------------------------------------------

    app.add_handler(
        CommandHandler("addkw", add_kw_cmd)
    )

    app.add_handler(
        CommandHandler("removekw", remove_kw_cmd)
    )

    app.add_handler(
        CommandHandler("listkw", list_kw_cmd)
    )

    # --------------------------------------------------
    # Error Handler
    # --------------------------------------------------


    app.add_handler(CallbackQueryHandler(handle_download_hsmt))
    
    app.add_handler(
                CommandHandler("crawl", crawl_cmd)
            )

    app.add_error_handler(error_handler)

    logger.info(
        "Telegram Bot đã được setup thành công."
    )

    return app