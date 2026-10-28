import asyncio
import logging
import os
import re
import shutil
import tempfile
import uuid
from pathlib import Path

import yt_dlp
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Paste your NEW token from @BotFather between the quotes below,
# or leave it as is and set the BOT_TOKEN environment variable instead.
BOT_TOKEN = os.environ.get("BOT_TOKEN") or "9250092712:AAHegfYZeGkyWM3tWKfUsBEcD6QZwyg2ENA"
MAX_FILE_SIZE = 50 * 1024 * 1024  # Telegram bot upload limit: 50 MB
URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)

HELP_TEXT = (
    "🤖 *What can this bot do?*\n\n"
    "Send me a link (YouTube, TikTok, Instagram, Twitter/X, Facebook, Reddit, "
    "and 1000+ other sites) and I will:\n"
    "1️⃣ Ask you if you want the *audio* or the *video*\n"
    "2️⃣ Download it and send it back to you\n\n"
    "🎵 *Audio* → sent as an MP3 file\n"
    "🎬 *Video* → sent as an MP4 video (up to 720p)\n\n"
    "⚠️ *Limits*\n"
    "• Telegram bots can only send files up to 50 MB\n"
    "• Private or age-restricted content may not work\n\n"
    "*Commands*\n"
    "/start - Welcome message\n"
    "/help - Show what this bot can do"
)


def username_line(context: ContextTypes.DEFAULT_TYPE) -> str:
    # Escape underscores so legacy Markdown doesn't break on usernames like my_bot
    return "\n\n🔗 @" + context.bot.username.replace("_", "\\_")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    name = update.effective_user.first_name if update.effective_user else "there"
    await update.message.reply_text(
        f"👋 Hi {name}!\n\nSend me a link and I'll send you its audio or video.\n\n"
        + HELP_TEXT
        + username_line(context),
        parse_mode="Markdown",
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        HELP_TEXT + username_line(context), parse_mode="Markdown"
    )


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text or ""
    match = URL_RE.search(text)
    if not match:
        await update.message.reply_text(
            "Please send me a valid link (starting with http:// or https://). "
            "Use /help to see what I can do."
        )
        return

    url = match.group(0)
    token = uuid.uuid4().hex[:10]
    # callback_data is limited to 64 bytes, so store the URL and pass a short token
    context.user_data.setdefault("links", {})[token] = url

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🎵 Audio", callback_data=f"dl:audio:{token}"),
                InlineKeyboardButton("🎬 Video", callback_data=f"dl:video:{token}"),
            ],
            [InlineKeyboardButton("❌ Cancel", callback_data=f"dl:cancel:{token}")],
        ]
    )
    await update.message.reply_text(
        "Do you want the audio or the video?",
        reply_markup=keyboard,
        reply_to_message_id=update.message.message_id,
    )


def download_media(url: str, kind: str, out_dir: str) -> Path:
    """Blocking download with yt-dlp. Returns the path of the downloaded file."""
    common = {
        "outtmpl": os.path.join(out_dir, "%(title).80s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "max_filesize": MAX_FILE_SIZE,
    }
    if kind == "audio":
        opts = {
            **common,
            "format": "bestaudio/best",
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }
            ],
        }
    else:
        opts = {
            **common,
            "format": (
                "bv*[height<=720][vcodec^=avc1]+ba[acodec^=mp4a]/"
                "b[height<=720][vcodec^=avc1]/"
                "bv*[height<=720][ext=mp4]+ba[ext=m4a]/"
                "b[height<=720]/b"
            ),
            "merge_output_format": "mp4",
        }

    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.extract_info(url, download=True)

    files = [p for p in Path(out_dir).iterdir() if p.is_file()]
    if not files:
        raise RuntimeError("Nothing was downloaded (the file may be larger than 50 MB).")
    return max(files, key=lambda p: p.stat().st_size)


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    try:
        _, action, token = query.data.split(":", 2)
    except ValueError:
        return

    links = context.user_data.get("links", {})
    url = links.pop(token, None)

    if action == "cancel":
        await query.edit_message_text("Cancelled ✅")
        return

    if not url:
        await query.edit_message_text(
            "This request has expired. Please send the link again."
        )
        return

    label = "audio 🎵" if action == "audio" else "video 🎬"
    await query.edit_message_text(f"⏳ Downloading {label}, please wait...")

    chat_id = query.message.chat_id
    tmp_dir = tempfile.mkdtemp(prefix="ptb_dl_")
    try:
        await context.bot.send_chat_action(
            chat_id,
            ChatAction.UPLOAD_VOICE if action == "audio" else ChatAction.UPLOAD_VIDEO,
        )
        file_path = await asyncio.to_thread(download_media, url, action, tmp_dir)

        if file_path.stat().st_size > MAX_FILE_SIZE:
            await query.edit_message_text(
                "❌ The file is bigger than 50 MB, which is Telegram's limit for bots."
            )
            return

        with open(file_path, "rb") as f:
            if action == "audio":
                await context.bot.send_audio(
                    chat_id,
                    audio=f,
                    title=file_path.stem,
                    caption=f"📥 @{context.bot.username}",
                    read_timeout=120,
                    write_timeout=120,
                )
            else:
                await context.bot.send_video(
                    chat_id,
                    video=f,
                    caption=f"📥 @{context.bot.username}",
                    supports_streaming=True,
                    read_timeout=120,
                    write_timeout=120,
                )
        await query.edit_message_text("✅ Done!")
    except Exception as e:  # noqa: BLE001
        logger.exception("Download failed")
        await query.edit_message_text(
            f"❌ Sorry, I couldn't download that.\n\n{str(e)[:300]}"
        )
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


async def post_init(app: Application) -> None:
    await app.bot.set_my_commands(
        [
            BotCommand("start", "Start the bot"),
            BotCommand("help", "What can this bot do?"),
        ]
    )


def main() -> None:
    if BOT_TOKEN == "PASTE_NEW_BOT_TOKEN":
        raise SystemExit("Put your bot token in bot.py (BOT_TOKEN = ...) first. Get it from @BotFather.")

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CallbackQueryHandler(button_handler, pattern=r"^dl:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))

    logger.info("Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
