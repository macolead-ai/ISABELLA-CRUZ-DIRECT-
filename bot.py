import logging
import os
import io
import asyncio
from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)
from PIL import Image, ImageFilter, ImageEnhance

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get('BOT_TOKEN')

# Limits
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB input
MAX_OUTPUT_DIM = 6000              # Max width/height after upscaling

# Modes
MODE_WAIT_FILE = "wait_file"
MODE_WAIT_SCALE = "wait_scale"

# Scale options
SCALES = [
    ("2️⃣ 2x Upscale",  2),
    ("3️⃣ 3x Upscale",  3),
    ("4️⃣ 4x Upscale",  4),
    ("✨ Enhance only (no resize)", 1),
]


# ---------- Helpers ----------

def main_menu_markup() -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("✨ Enhance / Upscale Image", callback_data="menu_enhance")],
        [InlineKeyboardButton("ℹ️ Help", callback_data="menu_help")],
    ]
    return InlineKeyboardMarkup(keyboard)


def scale_markup() -> InlineKeyboardMarkup:
    rows = []
    for lbl, scale in SCALES:
        rows.append([InlineKeyboardButton(lbl, callback_data=f"sc_{scale}")])
    rows.append([InlineKeyboardButton("🏠 Main Menu", callback_data="menu_home")])
    return InlineKeyboardMarkup(rows)


def reset_user_state(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop('mode', None)
    context.user_data.pop('source_file', None)


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


# ---------- Commands ----------

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    logger.info(f"User {user.id} started the bot")
    reset_user_state(context)

    welcome = (
        "👋 *Welcome to Image Enhancer Bot!*\n\n"
        "I make your photos look sharper, brighter, and bigger 🚀\n\n"
        "✨ *What I do:*\n"
        "• 📈 Upscale 2x, 3x, or 4x\n"
        "• 🔪 Apply smart sharpening\n"
        "• 🎨 Boost color & contrast\n"
        "• 🧹 Reduce noise\n\n"
        f"_Max file size: 10 MB._\n\n"
        "Tap below to begin:"
    )
    await update.message.reply_text(welcome, reply_markup=main_menu_markup(), parse_mode='Markdown')


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "ℹ️ *How to use*\n\n"
        "1. Tap ✨ *Enhance / Upscale Image*\n"
        "2. Send me a photo or image file\n"
        "3. Pick a scale (2x / 3x / 4x) or enhance-only\n"
        "4. Get your enhanced image!\n\n"
        "💡 *Tips:*\n"
        "• Best results on photos with good lighting\n"
        "• Very small images (< 200px) may not improve much\n"
        "• Results capped at 6000×6000 max\n\n"
        "Use /cancel anytime to reset."
    )
    if update.message:
        await update.message.reply_text(text, parse_mode='Markdown', reply_markup=main_menu_markup())
    else:
        await update.callback_query.edit_message_text(
            text, parse_mode='Markdown', reply_markup=main_menu_markup()
        )


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reset_user_state(context)
    await update.message.reply_text(
        "❌ Cancelled. Use /start to begin again.",
        reply_markup=main_menu_markup(),
    )


# ---------- Menu callbacks ----------

async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "menu_home":
        reset_user_state(context)
        await query.edit_message_text(
            "🏠 *Main Menu*\nChoose an option below:",
            reply_markup=main_menu_markup(),
            parse_mode='Markdown',
        )

    elif data == "menu_help":
        await help_command(update, context)

    elif data == "menu_enhance":
        context.user_data['mode'] = MODE_WAIT_FILE
        await query.edit_message_text(
            "✨ *Enhance Mode*\n\nSend me an image (photo or file).\n_Max 10 MB._",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🏠 Main Menu", callback_data="menu_home")]]
            ),
        )

    elif data.startswith("sc_"):
        scale = int(data.split("_", 1)[1])
        await do_enhance(update, context, scale)


# ---------- File handlers ----------

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await handle_incoming(update, context, update.message.document)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        return
    largest = update.message.photo[-1]
    fake_doc = type("Obj", (), {
        "file_id": largest.file_id,
        "file_name": f"photo_{largest.file_unique_id}.jpg",
        "file_size": largest.file_size,
        "mime_type": "image/jpeg",
    })()
    await handle_incoming(update, context, fake_doc)


async def handle_incoming(update, context, doc):
    mode = context.user_data.get('mode')
    if not doc:
        return

    if mode != MODE_WAIT_FILE:
        await update.message.reply_text(
            "Please tap ✨ *Enhance / Upscale Image* first.",
            reply_markup=main_menu_markup(),
            parse_mode='Markdown',
        )
        return

    fname = (doc.file_name or "image.jpg").lower()
    allowed = (".jpg", ".jpeg", ".png", ".webp", ".bmp")
    if not fname.endswith(allowed):
        await update.message.reply_text("⚠️ Send a JPG, PNG, WEBP, or BMP image.")
        return

    if doc.file_size and doc.file_size > MAX_FILE_SIZE:
        await update.message.reply_text(
            f"⚠️ File too large ({human_size(doc.file_size)}). Max is 10 MB."
        )
        return

    context.user_data['source_file'] = {
        "file_id": doc.file_id,
        "name": doc.file_name or "image.jpg",
        "size": doc.file_size or 0,
    }
    context.user_data['mode'] = MODE_WAIT_SCALE

    await update.message.reply_text(
        f"📸 Got *{doc.file_name or 'image'}* ({human_size(doc.file_size or 0)}).\n\n"
        "Choose enhancement option:",
        reply_markup=scale_markup(),
        parse_mode='Markdown',
    )


# ---------- Enhancement logic ----------

def enhance_image(in_bytes: bytes, scale: int) -> tuple:
    """
    Smart upscale + enhance using Pillow:
    - LANCZOS upscaling (best quality resize algorithm)
    - Median filter to reduce noise
    - UnsharpMask for sharper details
    - Contrast + color + brightness enhancement
    Returns (out_bytes, (new_w, new_h), original_size)
    """
    img = Image.open(io.BytesIO(in_bytes)).convert("RGB")
    orig_w, orig_h = img.size

    # Apply scale
    new_w = orig_w * scale
    new_h = orig_h * scale

    # Cap max dimensions
    if max(new_w, new_h) > MAX_OUTPUT_DIM:
        ratio = MAX_OUTPUT_DIM / max(new_w, new_h)
        new_w = int(new_w * ratio)
        new_h = int(new_h * ratio)

    # Step 1: light noise reduction BEFORE upscale (preserves details)
    img = img.filter(ImageFilter.MedianFilter(size=3))

    # Step 2: Upscale with LANCZOS (best for photos)
    if scale > 1:
        img = img.resize((new_w, new_h), Image.LANCZOS)

    # Step 3: Sharpen — UnsharpMask gives "AI-like" detail enhancement
    img = img.filter(ImageFilter.UnsharpMask(radius=2, percent=150, threshold=3))

    # Step 4: Auto contrast & color boost
    img = ImageEnhance.Contrast(img).enhance(1.15)
    img = ImageEnhance.Color(img).enhance(1.1)
    img = ImageEnhance.Sharpness(img).enhance(1.3)
    img = ImageEnhance.Brightness(img).enhance(1.03)

    out = io.BytesIO()
    img.save(out, format="JPEG", quality=92, optimize=True)
    return out.getvalue(), (new_w, new_h), (orig_w, orig_h)


async def do_enhance(update: Update, context: ContextTypes.DEFAULT_TYPE, scale: int):
    query = update.callback_query
    src = context.user_data.get('source_file')

    if not src:
        await query.edit_message_text(
            "⚠️ No file found. Start again.", reply_markup=main_menu_markup()
        )
        return

    chat_id = query.message.chat_id
    label = f"{scale}x" if scale > 1 else "enhance only"
    await query.edit_message_text(
        f"⏳ Enhancing image ({label})… please wait.",
        parse_mode='Markdown',
    )

    try:
        tg_file = await context.bot.get_file(src["file_id"])
        in_buf = io.BytesIO()
        await tg_file.download_to_memory(out=in_buf)
        in_bytes = in_buf.getvalue()

        loop = asyncio.get_event_loop()
        out_bytes, (nw, nh), (ow, oh) = await loop.run_in_executor(
            None, enhance_image, in_bytes, scale
        )

        base = os.path.splitext(src["name"])[0]
        suffix = f"_{scale}x" if scale > 1 else "_enhanced"
        out_name = f"{base}{suffix}.jpg"

        caption = (
            f"✅ *Enhanced!*\n\n"
            f"📐 Size: {ow}×{oh} → *{nw}×{nh}*\n"
            f"📦 File: {human_size(len(out_bytes))}\n"
            f"✨ Applied: Smart upscale + sharpening + color boost"
        )

        await context.bot.send_document(
            chat_id=chat_id,
            document=InputFile(io.BytesIO(out_bytes), filename=out_name),
            caption=caption,
            parse_mode='Markdown',
            reply_markup=main_menu_markup(),
        )

    except Exception as e:
        logger.error(f"Enhance failed: {e}")
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"❌ Failed: {e}",
            reply_markup=main_menu_markup(),
        )
    finally:
        reset_user_state(context)


# ---------- Dummy web server (keeps Render Web Service alive) ----------

async def health(request):
    return web.Response(text="Bot is running")


async def run_web():
    port = int(os.environ.get("PORT", 10000))
    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Health server listening on port {port}")


# ---------- Runner ----------

async def run_bot():
    if not BOT_TOKEN:
        logger.critical("FATAL: BOT_TOKEN is missing!")
        return

    try:
        application = Application.builder().token(BOT_TOKEN).build()

        application.add_handler(CommandHandler("start", start_command))
        application.add_handler(CommandHandler("help", help_command))
        application.add_handler(CommandHandler("cancel", cancel_command))
        application.add_handler(CallbackQueryHandler(menu_callback))
        application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
        application.add_handler(MessageHandler(filters.PHOTO, handle_photo))

        await run_web()

        logger.info("Bot is now polling...")
        await application.initialize()
        await application.start()
        await application.updater.start_polling(drop_pending_updates=True)

        stop_event = asyncio.Event()
        await stop_event.wait()

    except Exception as e:
        logger.error(f"Failed to start bot: {e}")
    finally:
        if 'application' in locals():
            await application.stop()
            await application.shutdown()


def main():
    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user.")
    except Exception as e:
        logger.error(f"Main loop error: {e}")


if __name__ == '__main__':
    main()
