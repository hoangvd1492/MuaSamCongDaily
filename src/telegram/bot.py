import functools
import json
import os
from pathlib import Path
import asyncio
from curl_cffi.requests import AsyncSession
from curl_cffi.curl import CurlMime

import io

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
    get_existing_hsmt_file,
    get_existing_baocao_file
)

from src.database.db import (
    add_keyword,
    get_all_keywords,
    get_all_subscribers,
    get_subscriber_role,
    remove_keyword,
    remove_subscriber,
    upsert_subscriber,
    is_tbmt_valid
)
from src.logger import get_logger


logger = get_logger("APP.BOT")


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
):
    """Gửi tin nhắn văn bản hoặc file kèm inline button tới tất cả subscribers."""
    bot_token = os.getenv("BOT_TOKEN")
    if not bot_token:
        raise ValueError("Chưa thiết lập BOT_TOKEN trong file .env!")

    subscribers = get_all_subscribers()
    if not subscribers:
        logger.warning("⚠️ Không có subscriber nào trong hệ thống!")
        return

    base_url = f"https://api.telegram.org/bot{bot_token}"

    async with AsyncSession(impersonate="chrome") as session:
        for sub in subscribers:
            chat_id = str(sub["chat_id"])
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
                        mp.addpart(name="reply_markup", data=json.dumps(reply_markup))
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

                    response = await session.post(url, json=payload, timeout=10)

                result = response.json()
                if not result.get("ok"):
                    logger.error(
                        f"❌ Gửi Telegram thất bại {chat_id}: {result.get('description')}"
                    )

            except Exception as error:
                logger.error(f"❌ Gửi Telegram thất bại {chat_id}: {error}")


async def handle_download_hsmt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    # 1. Tắt hiệu ứng quay tròn trên nút bấm
    try:
        await query.answer()
    except TelegramError:
        pass

    callback_data = query.data
    if not callback_data or not callback_data.startswith("download_hsmt:"):
        return

    # 2. Tách dữ liệu: "download_hsmt:id:ma_tbmt"
    parts = callback_data.split(":")
    if len(parts) != 3:
        return

    _, item_id, ma_tbmt = parts
    chat_id = query.message.chat_id

    if not is_tbmt_valid(item_id=item_id, ma_tbmt=ma_tbmt):
        await context.bot.send_message(
            chat_id=chat_id,
            text="❌ Thông tin gói thầu không hợp lệ hoặc không tồn tại trong hệ thống!",
            parse_mode="HTML",
        )
        return

    existing_pdf = get_existing_hsmt_file(ma_tbmt)
    existing_docx = get_existing_baocao_file(ma_tbmt)

    # TRƯỜNG HỢP 1: ĐÃ CÓ ĐỦ CẢ 2 FILE CACHE -> GỬI NGAY
    if existing_pdf and existing_docx:
        with open(existing_pdf, "rb") as f_pdf, open(existing_docx, "rb") as f_docx:
            media_group = [
                InputMediaDocument(
                    media=f_pdf,
                    filename=f"HSMT_{ma_tbmt}.pdf",
                ),
                InputMediaDocument(
                    media=f_docx,
                    filename=f"BaoCao_{ma_tbmt}.docx",
                    caption=f"✅ HSMT & Tóm tắt cho gói <b>{ma_tbmt}</b>!",
                    parse_mode="HTML",
                ),
            ]
            await context.bot.send_media_group(chat_id=chat_id, media=media_group)
        return

    already_pending = is_task_pending(item_id)
    await add_download_task(id=item_id, ma_tbmt=ma_tbmt, chat_id=chat_id)

    # 4. PHẢN HỒI THÔNG BÁO CHO NGƯỜI DÙNG
    if already_pending:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"⏳ Gói <b>{ma_tbmt}</b> đang trong hàng đợi. Bot sẽ gửi file cho bạn ngay khi hoàn tất!",
            parse_mode="HTML",
        )
    else:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"📥 Đã thêm yêu cầu tải HSMT & tạo báo cáo <b>{ma_tbmt}</b> vào hàng đợi xử lý.",
            parse_mode="HTML",
        )
# ==========================================================
# SETUP BOT
# ==========================================================


def setup_bot() -> Application:

    bot_token = os.getenv("BOT_TOKEN")

    if not bot_token:
        raise ValueError(
            "Chưa thiết lập BOT_TOKEN trong file .env!"
        )

    app = (
        ApplicationBuilder()
        .token(bot_token)
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