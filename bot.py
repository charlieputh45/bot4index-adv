# =========================
# Imports
# =========================
import asyncio
import base64
import os
import re
import sys
from datetime import datetime, timezone
from collections import defaultdict

from pyrogram import Client, enums, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import ListenerTimeout
import uvicorn

from config import *
from utility import (
    add_user, is_token_valid, authorize_user, is_user_authorized,
    generate_token, shorten_url, get_token_link, extract_channel_and_msg_id,
    safe_api_call, get_allowed_channels, extract_file_info,
    delete_after_delay, queue_file_for_processing, file_queue_worker,
    file_queue, extract_tmdb_link
)
from db import db, users_col, tokens_col, files_col, allowed_channels_col, auth_users_col
from fast_api import api
from utility import upsert_file_with_tmdb_info

# =========================
# Constants & Globals
# ========================= 

TOKEN_VALIDITY_SECONDS = 24 * 60 * 60  # 24 hours token validity
MAX_FILES_PER_SESSION = 10             # Max files a user can access per session

# Initialize Pyrogram bot client
bot = Client(
    "bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workers=1000,
    parse_mode=enums.ParseMode.HTML
)

# Track how many files each user has accessed in the current session
user_file_count = defaultdict(int)

# =========================
# Bot Command Handlers
# =========================

@bot.on_message(filters.command("start") & filters.private)
async def start_handler(client, message):
    """
    Handles the /start command.
    - Registers the user.
    - Handles token-based authorization.
    - Handles file access via deep link.
    - Sends a greeting if no special argument is provided.
    """
    user_id = message.from_user.id
    await add_user(user_id)
    bot_username = BOT_USERNAME

    # --- Token-based authorization ---
    if len(message.command) == 2 and message.command[1].startswith("token_"):
        if await is_token_valid(message.command[1][6:], user_id):
            await authorize_user(user_id)
            await safe_api_call(message.reply_text("✅ You are now authorized to access files for 24 hours."))
        else:
            await safe_api_call(message.reply_text("❌ Invalid or expired token. Please get a new link."))
        return

    # --- File access via deep link ---
    if len(message.command) == 2 and message.command[1].startswith("file_"):
        # Check if user is authorized
        if not await is_user_authorized(user_id):
            now = datetime.now(timezone.utc)
            token_doc = await tokens_col.find_one({
                "user_id": user_id,
                "expiry": {"$gt": now}
            })
            token_id = token_doc["token_id"] if token_doc else await generate_token(user_id)
            short_link = shorten_url(get_token_link(token_id, bot_username))
            await safe_api_call(message.reply_text(
                "🔒<b>You Are Not Authorized</b>",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🔑 Get Access Link", url=short_link)]]
                )
            ))
            return

        # Limit file access per session
        if user_file_count[user_id] >= MAX_FILES_PER_SESSION:
            await safe_api_call(message.reply_text("❌ You have reached the maximum of 10 files per session."))
            return

        # Decode file link and send file
        try:
            b64 = message.command[1][5:]
            padding = '=' * (-len(b64) % 4)
            decoded = base64.urlsafe_b64decode(b64 + padding).decode()
            channel_id_str, msg_id_str = decoded.split("_")
            channel_id = int(channel_id_str)
            msg_id = int(msg_id_str)
        except Exception:
            await safe_api_call(message.reply_text("Invalid file link."))
            return

        file_doc = await files_col.find_one({"channel_id": channel_id, "message_id": msg_id})
        if not file_doc:
            await safe_api_call(message.reply_text("File not found."))
            return

        try:
            sent = await safe_api_call(client.copy_message(
                chat_id=message.chat.id,
                from_chat_id=file_doc["channel_id"],
                message_id=file_doc["message_id"]
            ))
            user_file_count[user_id] += 1
            bot.loop.create_task(delete_after_delay(client, sent.chat.id, sent.id))
        except Exception as e:
            await safe_api_call(message.reply_text(f"Failed to send file: {e}"))
        return

    # --- Default greeting ---
    await safe_api_call(message.reply_text(
        "👋 <b>Welcome!</b>\n\n"
        "I'm your friendly file access bot 🤖.\n"
        "To get started, use a valid <b>access link</b> to unlock files 🔑.\n\n"
        "If you need help, contact the admin @tgflixcontactbot 🚀"
    ))

@bot.on_message(filters.document | filters.video | filters.audio | filters.photo)
async def channel_file_handler(client, message):
    allowed_channels = await get_allowed_channels()
    if message.chat.id not in allowed_channels:
        return
    await queue_file_for_processing(message, reply_func=message.reply_text)
    await file_queue.join()

@bot.on_message(filters.command("index") & filters.user(OWNER_ID))
async def index_channel_files(client, message: Message):
    """
    Handles the /index command for the owner.
    - Asks for start and end file links.
    - Indexes files in the specified range from allowed channels.
    - Only supports /c/ links.
    """
    prompt = await safe_api_call(message.reply_text("Please send the **start file link** (Telegram message link, only /c/ links supported):"))
    try:
        start_msg = await client.listen(message.chat.id, timeout=120)
    except ListenerTimeout:
        await safe_api_call(prompt.edit_text("⏰ Timeout! You took too long to reply. Please try again."))
        return
    start_link = start_msg.text.strip()

    prompt2 = await safe_api_call(message.reply_text("Now send the **end file link** (Telegram message link, only /c/ links supported):"))
    try:
        end_msg = await client.listen(message.chat.id, timeout=120)
    except ListenerTimeout:
        await safe_api_call(prompt2.edit_text("⏰ Timeout! You took too long to reply. Please try again."))
        return
    end_link = end_msg.text.strip()

    try:
        start_id, start_msg_id = extract_channel_and_msg_id(start_link)
        end_id, end_msg_id = extract_channel_and_msg_id(end_link)

        if start_id != end_id:
            await message.reply_text("Start and end links must be from the same channel.")
            return

        channel_id = start_id
        allowed_channels = await get_allowed_channels()
        if channel_id not in allowed_channels:
            await message.reply_text("❌ This channel is not allowed for indexing.")
            return

        if start_msg_id > end_msg_id:
            start_msg_id, end_msg_id = end_msg_id, start_msg_id

    except Exception as e:
        await message.reply_text(f"Invalid link: {e}")
        return

    await message.reply_text(f"Indexing files from {start_msg_id} to {end_msg_id} in channel {channel_id}...")

    batch_size = 50
    total_queued = 0
    for batch_start in range(start_msg_id, end_msg_id + 1, batch_size):
        batch_end = min(batch_start + batch_size - 1, end_msg_id)
        ids = list(range(batch_start, batch_end + 1))
        try:
            messages = []
            for msg_id in ids:
                msg = await safe_api_call(client.get_messages(channel_id, msg_id))
                messages.append(msg)
        except Exception as e:
            await message.reply_text(f"Failed to get messages {batch_start}-{batch_end}: {e}")
            continue
        for msg in messages:
            if not msg:
                continue
            if msg.document or msg.video or msg.audio or msg.photo:
                await queue_file_for_processing(
                    msg,
                    channel_id=channel_id,
                    reply_func=message.reply_text
                )
                total_queued += 1

    await message.reply_text(f"✅ Queued {total_queued} files from channel {channel_id} for processing.")

@bot.on_message(filters.command("delete") & filters.user(OWNER_ID))
async def delete_file_handler(client, message: Message):
    """
    Handles the /delete command for the owner.
    - /delete file <telegram_message_link> or <channel_id> <message_id>: deletes a file from the files array.
    - /delete tmdb <tmdb_type> <tmdb_id> or <tmdb_link>: deletes the entire TMDB document.
    """
    args = message.command
    if len(args) < 2:
        await message.reply_text(
            "Usage:\n"
            "/delete file <telegram_message_link> or channel_id message_id\n"
            "/delete tmdb tmdb_type tmdb_id or /delete tmdb tmdb_link"
        )
        return

    if args[1] == "file":
        # Delete a file from the files array
        if len(args) == 3 and args[2].startswith("https://t.me/"):
            link = args[2]
            try:
                match = re.search(r"t\.me/c/(-?\d+)/(\d+)", link)
                if match:
                    channel_id = int("-100" + match.group(1)) if not match.group(1).startswith("-100") else int(match.group(1))
                    message_id = int(match.group(2))
                else:
                    await message.reply_text("Invalid Telegram message link. Only /c/ links are supported.")
                    return
            except Exception:
                await message.reply_text("Invalid Telegram message link.")
                return
        elif len(args) == 4:
            try:
                channel_id = int(args[2])
                message_id = int(args[3])
            except Exception:
                await message.reply_text("Usage: /delete file <telegram_message_link> or /delete file <channel_id> <message_id>")
                return
        else:
            await message.reply_text("Usage: /delete file <telegram_message_link> or /delete file <channel_id> <message_id>")
            return

        try:
            result = await files_col.update_one(
                {"files.channel_id": channel_id, "files.message_id": message_id},
                {"$pull": {"files": {"channel_id": channel_id, "message_id": message_id}}}
            )
            if result.modified_count:
                await message.reply_text(f"✅ File ({channel_id}, {message_id}) deleted from database.")
            else:
                await message.reply_text("❌ File not found in database.")
        except Exception as e:
            await message.reply_text(f"Error: {e}")

    elif args[1] == "tmdb":
        # Support both /delete tmdb <tmdb_type> <tmdb_id> and /delete tmdb <tmdb_link>
        if len(args) == 3 and args[2].startswith("http"):
            # It's a TMDB link
            try:
                tmdb_type, tmdb_id = await extract_tmdb_link(args[2])
            except Exception:
                await message.reply_text("Invalid TMDB link.")
                return
        elif len(args) == 4:
            tmdb_type = args[2]
            tmdb_id = args[3]
        else:
            await message.reply_text("Usage: /delete tmdb tmdb_type tmdb_id or /delete tmdb tmdb_link")
            return
        try:
            result = await files_col.delete_one({"tmdb_type": tmdb_type, "tmdb_id": tmdb_id})
            if result.deleted_count:
                await message.reply_text(f"✅ TMDB document ({tmdb_type}, {tmdb_id}) deleted from database.")
            else:
                await message.reply_text("❌ TMDB document not found in database.")
        except Exception as e:
            await message.reply_text(f"Error: {e}")

    else:
        await message.reply_text(
            "Usage:\n"
            "/delete file <telegram_message_link> or channel_id message_id\n"
            "/delete tmdb tmdb_type tmdb_id or /delete tmdb tmdb_link"
        )

@bot.on_message(filters.command('restart') & filters.private & filters.user(OWNER_ID))
async def restart(client, message):
    """
    Handles the /restart command for the owner.
    - Deletes the log file, runs update.py, and restarts the bot.
    """
    log_file = "bot_log.txt"
    if os.path.exists(log_file):
        try:
            os.remove(log_file)
        except Exception as e:
            await safe_api_call(message.reply_text(f"Failed to delete log file: {e}"))
    os.system("python3 update.py")
    os.execl(sys.executable, sys.executable, "bot.py")

@bot.on_message(filters.command("addchannel") & filters.user(OWNER_ID))
async def add_channel_handler(client, message: Message):
    """
    Handles the /addchannel command for the owner.
    - Adds a channel to the allowed channels list in the database.
    """
    if len(message.command) < 3:
        await message.reply_text("Usage: /addchannel channel_id channel_name")
        return
    try:
        channel_id = int(message.command[1])
        channel_name = " ".join(message.command[2:])
        await allowed_channels_col.update_one(
            {"channel_id": channel_id},
            {"$set": {"channel_id": channel_id, "channel_name": channel_name}},
            upsert=True
        )
        await message.reply_text(f"✅ Channel {channel_id} ({channel_name}) added to allowed channels.")
    except Exception as e:
        await message.reply_text(f"Error: {e}")

@bot.on_message(filters.command("removechannel") & filters.user(OWNER_ID))
async def remove_channel_handler(client, message: Message):
    """
    Handles the /removechannel command for the owner.
    - Removes a channel from the allowed channels list in the database.
    """
    if len(message.command) != 2:
        await message.reply_text("Usage: /removechannel channel_id")
        return
    try:
        channel_id = int(message.command[1])
        result = await allowed_channels_col.delete_one({"channel_id": channel_id})
        if result.deleted_count:
            await message.reply_text(f"✅ Channel {channel_id} removed from allowed channels.")
        else:
            await message.reply_text("❌ Channel not found in allowed channels.")
    except Exception as e:
        await message.reply_text(f"Error: {e}")

@bot.on_message(filters.command("broadcast") & filters.user(OWNER_ID))
async def broadcast_handler(client, message: Message):
    """
    Handles the /broadcast command for the owner.
    - Broadcasts a message to all users in the database.
    - Removes users from DB if blocked or deactivated.
    """
    if len(message.command) < 2:
        await message.reply_text("Usage: /broadcast <your message>")
        return
    text = message.text.split(maxsplit=1, sep=" ",)[1]
    users = users_col.find({}, {"_id": 0, "user_id": 1})
    total = 0
    failed = 0
    removed = 0
    for user in users:
        try:
            await safe_api_call(client.send_message(user["user_id"], text))
            total += 1
        except Exception as e:
            failed += 1
            err_str = str(e)
            if "UserIsBlocked" in err_str or "InputUserDeactivated" in err_str:
                users_col.delete_one({"user_id": user["user_id"]})
                removed += 1
            continue
    await message.reply_text(f"✅ Broadcast sent to {total} users. Failed: {failed}. Removed: {removed}")

@bot.on_message(filters.command("log") & filters.user(OWNER_ID))
async def send_log_file(client, message: Message):
    """
    Handles the /log command for the owner.
    - Sends the bot.log file to the owner.
    """
    log_file = "bot_log.txt"
    if not os.path.exists(log_file):
        await safe_api_call(message.reply_text("Log file not found."))
        return
    try:
        await safe_api_call(client.send_document(message.chat.id, log_file, caption="Here is the log file."))
    except Exception as e:
        await safe_api_call(message.reply_text(f"Failed to send log file: {e}"))

@bot.on_message(filters.command("stats") & filters.private & filters.user(OWNER_ID))
async def stats_command(client, message: Message):
    """Show statistics (only for OWNER_ID)."""
    try:
        total_auth_users = await auth_users_col.count_documents({})
        total_users = await users_col.count_documents({})
        total_files = await files_col.count_documents({})
        stats = await db.command("dbstats")  # <-- await here
        db_storage = stats.get("storageSize", 0)

        await safe_api_call(
            message.reply_text(
            f"👤 Total auth users: <b>{total_auth_users}/{total_users}</b>\n"
            f"📁 Total files: <b>{total_files}</b>\n"
            f"📊 Database storage used: <b>{db_storage / (1024 * 1024):.2f} MB</b>",
            )
        )
    except Exception as e:
        await message.reply_text(f"⚠️ An error occurred while fetching stats:\n<code>{e}</code>")

@bot.on_message(filters.private & filters.command("tmdb") & filters.user(OWNER_ID))
async def tmdb_command(client, message):
    """
    Manually update a file's TMDB info in the database.
    Usage: /tmdb <telegram_file_link> <tmdb_link> [season] [episode]
    """
    if len(message.command) < 3:
        await safe_api_call(message.reply_text("Usage: /tmdb <telegram_file_link> <tmdb_link> [season] [episode]"))
        return

    telegram_file_link = message.command[1]
    tmdb_link = message.command[2]
    season = message.command[3] if len(message.command) > 3 else None
    episode = message.command[4] if len(message.command) > 4 else None

    try:
        # Extract channel_id and message_id from telegram link
        channel_id, message_id = extract_channel_and_msg_id(telegram_file_link)
    except Exception as e:
        await safe_api_call(message.reply_text(f"Invalid Telegram file link: {e}"))
        return

    try:
        tmdb_type, tmdb_id = await extract_tmdb_link(tmdb_link)
    except Exception as e:
        await safe_api_call(message.reply_text(f"Invalid TMDB link: {e}"))
        return

    # Fetch the message from Telegram
    try:
        msg = await safe_api_call(client.get_messages(channel_id, message_id))
        if not msg:
            await safe_api_call(message.reply_text("File not found in Telegram."))
            return
    except Exception as e:
        await safe_api_call(message.reply_text(f"Failed to fetch file: {e}"))
        return

    # Process the file (queue_file_for_processing will update or insert the file doc)
    try:
        file_info = await extract_file_info(msg, channel_id=channel_id)
    except Exception as e:
        await safe_api_call(message.reply_text(f"Failed to process file: {e}"))
        return

    # Now update the file doc with TMDB info
    try:
        await upsert_file_with_tmdb_info(file_info,
            tmdb_type,
            tmdb_id,
            season,
            episode,
            bot
        )
        await safe_api_call(message.reply_text("✅ File updated with TMDB info."))
    except Exception as e:
        await safe_api_call(message.reply_text(f"Failed to update TMDB info: {e}"))




# =========================
# Main Entrypoint
# =========================

async def main():
    """
    Starts the bot and FastAPI server.
    """
    await bot.start()
    bot.loop.create_task(start_fastapi())
    bot.loop.create_task(file_queue_worker(bot))  # Start the queue worker

    # Send startup message to log channel
    try:
        await bot.send_message(LOG_CHANNEL_ID, "✅ Bot started and FastAPI server running.")
    except Exception as e:
        print(f"Failed to send startup message to log channel: {e}")

async def start_fastapi():
    """
    Starts the FastAPI server using Uvicorn.
    """
    try:
        config = uvicorn.Config(api, host="0.0.0.0", port=8000, loop="asyncio", log_level="warning", reload=True)
        server = uvicorn.Server(config)
        await server.serve()
    except KeyboardInterrupt:
        pass
        logger.info("FastAPI server stopped.")

if __name__ == "__main__":
    """
    Main process entrypoint.
    - Runs the bot and FastAPI server.
    - Handles graceful shutdown on KeyboardInterrupt.
    """
    try:
        bot.loop.run_until_complete(main())
        bot.loop.run_forever()
    except KeyboardInterrupt:
        bot.stop()
        tasks = asyncio.all_tasks(loop=bot.loop)
        for task in tasks:
            task.cancel()
        bot.loop.stop()
        logger.info("Bot stopped.")
