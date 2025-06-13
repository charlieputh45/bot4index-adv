import re
import asyncio
import base64
import uuid
import requests
from datetime import datetime, timezone, timedelta
from pyrogram.errors import FloodWait
from pyrogram import enums
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from config import logger

from db import (
    allowed_channels_col,
    users_col,
    tokens_col,
    auth_users_col,
    files_col,
)
from config import (SHORTERNER_URL, URLSHORTX_API_TOKEN, 
                    UPDATE_CHANNEL_ID, EXCLUDE_CHANNEL_ID,
                    LOG_CHANNEL_ID)
from tmdb import get_by_name, get_by_id

# =========================
# Constants & Globals
# =========================

TOKEN_VALIDITY_SECONDS = 24 * 60 * 60  # 24 hours
AUTO_DELETE_SECONDS = 5 * 60
channel_files_cache = {}
all_tmdb_files_cache = {}


# =========================
# Channel & User Utilities
# =========================

async def file_handler(message):
    allowed_channels = await get_allowed_channels()
    if message.chat.id not in allowed_channels:
        return
    await queue_file_for_processing(message, reply_func=message.reply_text)
    await file_queue.join()


async def get_allowed_channels():
    cursor = allowed_channels_col.find({}, {"_id": 0, "channel_id": 1})
    return [doc["channel_id"] async for doc in cursor]

async def add_user(user_id):
    await users_col.update_one(
        {"user_id": user_id},
        {"$set": {"user_id": user_id}},
        upsert=True
    )

async def authorize_user(user_id):
    """Authorize a user for 24 hours."""
    expiry = datetime.now(timezone.utc) + timedelta(seconds=TOKEN_VALIDITY_SECONDS)
    await auth_users_col.update_one(
        {"user_id": user_id},
        {"$set": {"expiry": expiry}},
        upsert=True
    )

async def is_user_authorized(user_id):
    """Check if a user is authorized."""
    doc = await auth_users_col.find_one({"user_id": user_id})
    if not doc:
        return False
    expiry = doc["expiry"]
    if isinstance(expiry, str):
        try:
            expiry = datetime.fromisoformat(expiry)
        except Exception:
            return False
    if isinstance(expiry, datetime) and expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    if expiry < datetime.now(timezone.utc):
        return False
    return True

# =========================
# Token Utilities
# =========================

async def generate_token(user_id):
    """Generate a new access token for a user."""
    token_id = str(uuid.uuid4())
    expiry = datetime.now(timezone.utc) + timedelta(seconds=TOKEN_VALIDITY_SECONDS)
    await tokens_col.insert_one({
        "token_id": token_id,
        "user_id": user_id,
        "expiry": expiry,
        "created_at": datetime.now(timezone.utc)
    })
    return token_id

async def is_token_valid(token_id, user_id):
    """Check if a token is valid for a user."""
    token = await tokens_col.find_one({"token_id": token_id, "user_id": user_id})
    if not token:
        return False
    expiry = token["expiry"]
    if isinstance(expiry, str):
        try:
            expiry = datetime.fromisoformat(expiry)
        except Exception:
            return False
    if isinstance(expiry, datetime) and expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    if expiry < datetime.now(timezone.utc):
        await tokens_col.delete_one({"_id": token["_id"]})
        return False
    return True

def get_token_link(token_id, bot_username):
    """Generate a Telegram deep link for a token."""
    return f"https://telegram.dog/{bot_username}?start=token_{token_id}"

# =========================
# Link & URL Utilities
# =========================

def generate_telegram_link(bot_username, channel_id, message_id):
    """Generate a base64-encoded Telegram deep link for a file."""
    raw = f"{channel_id}_{message_id}".encode()
    b64 = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return f"https://telegram.dog/{bot_username}?start=file_{b64}"

def generate_c_link(channel_id, message_id):
    # channel_id must be like -1001234567890
    return f"https://t.me/c/{str(channel_id)[4:]}/{message_id}"

def extract_channel_and_msg_id(link):
    # Only support t.me/c/(-?\d+)/(\d+)
    match = re.search(r"t\.me/c/(-?\d+)/(\d+)", link)
    if match:
        channel_id = int("-100" + match.group(1)) if not match.group(1).startswith("-100") else int(match.group(1))
        msg_id = int(match.group(2))
        return channel_id, msg_id
    raise ValueError("Invalid Telegram message link format. Only /c/ links are supported.")

def shorten_url(long_url):
    """Shorten a URL using the configured shortener."""
    try:
        resp = requests.get(
            f"https://{SHORTERNER_URL}/api?api={URLSHORTX_API_TOKEN}&url={long_url}",
            timeout=5
        )
        if resp.status_code == 200:
            data = resp.json()
            if data.get("status") == "success" and data.get("shortenedUrl"):
                return data["shortenedUrl"]
        return long_url
    except Exception:
        return long_url

# =========================
# File Utilities
# =========================
'''
async def upsert_file_info(file_info):
    """Insert or update file info, avoiding duplicates (Motor async version)."""
    await files_col.update_one(
        {"channel_id": file_info["channel_id"], "message_id": file_info["message_id"]},
        {"$set": file_info},
        upsert=True
    )
'''

async def extract_file_info(message, channel_id=None):
    """Extract file info from a Pyrogram message and remove extension from file name."""
    caption_name = message.caption.strip() if message.caption else None
    file_info = {
        "channel_id": channel_id if channel_id is not None else message.chat.id,
        "message_id": message.id,
        "file_name": None,
        "file_size": None,
        "file_format": None,
        "date": message.date.replace(tzinfo=timezone.utc) if getattr(message, "date", None) else datetime.now(timezone.utc)
    }
    if message.document:
        file_info["file_name"] = caption_name or message.document.file_name
        file_info["file_size"] = message.document.file_size
        file_info["file_format"] = message.document.mime_type
    elif message.video:
        file_info["file_name"] = caption_name or (message.video.file_name or "video.mp4")
        file_info["file_size"] = message.video.file_size
        file_info["file_format"] = message.video.mime_type
    elif message.audio:
        file_info["file_name"] = caption_name or (message.audio.file_name or "audio.mp3")
        file_info["file_size"] = message.audio.file_size
        file_info["file_format"] = message.audio.mime_type
    elif message.photo:
        file_info["file_name"] = caption_name or "photo.jpg"
        file_info["file_size"] = getattr(message.photo, "file_size", None)
        file_info["file_format"] = "image/jpeg"
    # Remove extension from file_name if present
    if file_info["file_name"]:
        file_info["file_name"] = await remove_extension(file_info["file_name"])
    return file_info

async def human_readable_size(size):
    for unit in ['B','KB','MB','GB','TB']:
        if size < 1024:
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} PB"

async def invalidate_channel_cache(channel_id):
    keys_to_delete = [k for k in channel_files_cache if k.startswith(f"{channel_id}:")]
    for k in keys_to_delete:
        del channel_files_cache[k]


async def remove_unwanted(input_string):
    # Use regex to match .mkv or .mp4 and everything that follows
    result = re.split(r'(\.mkv|\.mp4)', input_string)
    # Join the first two parts to get the string up to the extension
    return ''.join(result[:2])

async def remove_extension(caption):
    try:
        # Remove .mkv and .mp4 extensions if present
        cleaned_caption = re.sub(r'\.mkv|\.mp4|\.webm', '', caption)
        return cleaned_caption
    except Exception as e:
        logger.error(e)
        return None

# =========================
# Async/Bot Utilities
# =========================

async def safe_api_call(coro):
    """Utility wrapper to add delay before every bot API call."""
    while True:
        try:
            await asyncio.sleep(3)
            return await coro
        except FloodWait as e:
            print(f"FloodWait: Sleeping for {e.value} seconds")
            await asyncio.sleep(e.value)
        except Exception:
            raise

async def delete_after_delay(client, chat_id, msg_id):
    await asyncio.sleep(AUTO_DELETE_SECONDS)
    try:
        await safe_api_call(client.delete_messages(chat_id, msg_id))
    except Exception:
        pass

async def extract_tmdb_link(tmdb_url):
    movie_pattern = r'themoviedb\.org\/movie\/(\d+)'
    tv_pattern = r'themoviedb\.org\/tv\/(\d+)'
    collection_pattern = r'themoviedb\.org\/collection\/(\d+)'
    
    if re.search(movie_pattern, tmdb_url):
        tmdb_type = 'movie'
        tmdb_id = int(re.search(movie_pattern, tmdb_url).group(1))
    elif re.search(tv_pattern, tmdb_url):
        tmdb_type = 'tv'
        tmdb_id = int(re.search(tv_pattern, tmdb_url).group(1)) 
    elif re.search(collection_pattern, tmdb_url):
        tmdb_type = 'collection'
        tmdb_id = int(re.search(collection_pattern, tmdb_url).group(1)) 
    return tmdb_type, tmdb_id

async def extract_movie_info(caption):
    try:
        current_year = datetime.now().year + 2  # Allow a couple of years ahead for upcoming movies
        # Exclude 4-digit numbers followed by 'p' (like 1080p, 2160p, 720p)
        years = [
            y for y in re.findall(r'(\d{4})', caption)
            if 1900 <= int(y) <= current_year and not re.search(rf'{y}p', caption, re.IGNORECASE)
        ]
        release_year = years[-1] if years else None

        # Get everything before the last year
        if release_year:
            movie_name = caption.rsplit(release_year, 1)[0]
            movie_name = movie_name.replace('.', ' ').replace('(', '').replace(')', '').strip()
            movie_name = re.split(r'\s*A\s*K\s*A\s*', movie_name, flags=re.IGNORECASE)[0].strip()
        else:
            movie_name = caption

        # Return only movie_name and release_year, season and episode as None
        return movie_name, release_year
    except Exception as e:
        logger.error(f"Extract Movie info Error : {e}")
    return None, None, 

# =========================
# Queue System for File Processing
# =========================

file_queue = asyncio.Queue()

async def file_queue_worker(bot):
    processing_count = 0  # Track how many files processed in this batch
    while True:
        item = await file_queue.get()
        file_info, reply_func = item
        processing_count += 1
        try:
            # Check for duplicate by file name in any TMDB document
            existing = await files_col.find_one({
                "files": {
                    "$elemMatch": {
                        "file_name": file_info["file_name"]
                    }
                }
            })
            if existing:
                telegram_link = generate_c_link(file_info["channel_id"], file_info["message_id"])
                if reply_func:
                    await safe_api_call(
                        bot.send_message(
                            LOG_CHANNEL_ID,
                            f"⚠️ Duplicate File (by name).\nLink: {telegram_link}",
                            parse_mode=enums.ParseMode.HTML
                        )
                    )
            else:
                try:
                    if str(file_info["channel_id"]) not in EXCLUDE_CHANNEL_ID:
                        title, release_year= await extract_movie_info(file_info["file_name"])
                        result = await get_by_name(title, release_year)
                        tmdb_id, tmdb_type = result['id'], result['media_type'] 
                        await upsert_file_with_tmdb_info(file_info, tmdb_type, tmdb_id, bot)
                except Exception as e:
                    logger.error(f"Error processing TMDB info:{e}")
                    if reply_func:
                        await safe_api_call(
                            bot.send_message(
                                LOG_CHANNEL_ID,
                                f'❌ Error processing TMDB info: {file_info["file_name"]}\n{title} - {release_year}\n\n{e}',
                                parse_mode=enums.ParseMode.HTML
                            )
                        )
        except Exception as e:
            if reply_func:
                await safe_api_call(reply_func(f"❌ Error saving file: {e}"))
        finally:
            file_queue.task_done()
            if file_queue.empty():
                if processing_count > 1:
                    # Notify only if more than one file was processed
                    try:
                        await safe_api_call(
                            bot.send_message(
                                LOG_CHANNEL_ID,
                                f"✅ Done processing {processing_count} file(s) in the queue.",
                                parse_mode=enums.ParseMode.HTML
                            )
                        )
                    except Exception:
                        pass
            processing_count = 0  # Reset for next batch

# =========================
# Unified File Queueing
# =========================

async def queue_file_for_processing(message, channel_id=None, reply_func=None):
    try:
        file_info = await extract_file_info(message, channel_id=channel_id)
        if file_info["file_name"]:
            await file_queue.put((file_info, reply_func))
    except Exception as e:
        if reply_func:
            await safe_api_call(reply_func(f"❌ Error queuing file: {e}"))

async def upsert_file_with_tmdb_info(file_info, tmdb_type, tmdb_id, bot):
    """
    Upserts a document by tmdb_id and tmdb_type, adding file_info to the files array.
    This is an asynchronous version using Motor.
    The 'message' field from tmdb_info is not saved to the database.
    Only sends a message if this tmdb_id and tmdb_type is not already in the database.
    """
    result = await get_by_id(tmdb_type, tmdb_id)
    tmdb_info = result['mongo_dict']
    if not tmdb_info:
        return None

    # Check if tmdb_id and tmdb_type already exist in the database
    existing = await files_col.find_one({"tmdb_id": tmdb_id, "tmdb_type": tmdb_type})

    await files_col.update_one(
        {"tmdb_id": tmdb_id, "tmdb_type": tmdb_type},
        {
            "$set": tmdb_info,
            "$addToSet": {"files": file_info}
        },
        upsert=True
    )
    
    # Only send message if this is a new tmdb_id/tmdb_type entry
    if not existing and tmdb_info:
        try:
            poster_url = result['backdrop_url']
            trailer = result['trailer_url']
            info = result['message']

            if poster_url: 
                # Create the inline keyboard only if a trailer is available
                keyboard = None
                if trailer:
                    keyboard = InlineKeyboardMarkup(
                        [
                            [InlineKeyboardButton("🎥 Trailer", url=trailer)]
                        ]
                    )

                # Send the photo with or without the button
                await bot.send_photo(
                    UPDATE_CHANNEL_ID,
                    photo=poster_url,
                    caption=info,
                    parse_mode=enums.ParseMode.HTML,
                    reply_markup=keyboard
                )
                await asyncio.sleep(3)
        except Exception as e:
            logger.error(f" info error {e}")


