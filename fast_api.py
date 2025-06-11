from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from db import allowed_channels_col, files_col
from config import BOT_USERNAME, MY_DOMAIN
from utility import generate_telegram_link, all_tmdb_files_cache
from datetime import datetime

api = FastAPI()
api.add_middleware(
    CORSMiddleware,
    allow_origins=[f"{MY_DOMAIN}"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@api.get("/")
async def root():
    """Greet users on root route."""
    return JSONResponse({"message": "👋 Hello! Welcome to the Sharing Bot"})

@api.get("/api/channels")
async def api_channels():
    """List all channels (JSON)."""
    cursor = allowed_channels_col.find({}, {"_id": 0, "channel_id": 1, "channel_name": 1})
    channels = await cursor.to_list(length=None)
    return JSONResponse({"channels": channels})

'''
@api.get("/api/channel/{channel_id}/files")
async def api_channel_files(
    channel_id: int,
    q: str = "",
    offset: int = 0,
    limit: int = 10
):
    """List files for a channel (JSON)."""
    bot_username = BOT_USERNAME
    query = {"channel_id": channel_id}
    if q:
        regex = ".*".join(map(lambda s: s, q.strip().split()))
        query["file_name"] = {"$regex": regex, "$options": "i"}

    cache_key = f"{channel_id}:{q}:{offset}:{limit}"
    if cache_key not in channel_files_cache:
        files = list(files_col.find(query, {"_id": 0}).sort("message_id", -1))
        for file in files:
            file["telegram_link"] = generate_telegram_link(bot_username, file["channel_id"], file["message_id"])
            if isinstance(file.get("date"), str):
                try:
                    file["date"] = datetime.fromisoformat(file["date"])
                except Exception:
                    file["date"] = None
        channel_files_cache[cache_key] = files
    else:
        files = channel_files_cache[cache_key]

    paginated_files = files[offset:offset+limit]
    has_more = offset + limit < len(files)

    def serialize_file(file):
        return {
            "file_name": file.get("file_name"),
            "file_size": file.get("file_size"),
            "file_format": file.get("file_format"),
            "date": file.get("date").strftime('%Y-%m-%d %H:%M:%S') if file.get("date") else "",
            "telegram_link": file.get("telegram_link")
        }

    return JSONResponse({
        "files": [serialize_file(f) for f in paginated_files],
        "has_more": has_more
    })
'''

@api.get("/api/all-tmdb-files")
async def api_all_tmdb_files(
    q: str = "",
    cast: str = "",
    director: str = "",
    genre: str = "",
    offset: int = 0,
    limit: int = 10
):
    """
    Return TMDB entries with their files, sorted by release_date descending, paginated, and filtered by search query or cast.
    """
    cache_key = f"{q}:{cast}:{director}:{genre}:{offset}:{limit}"
    if cache_key in all_tmdb_files_cache:
        cached = all_tmdb_files_cache[cache_key]
        return JSONResponse(cached)

    query = {}
    if q:
        regex = ".*".join(map(lambda s: s, q.strip().split()))
        query["title"] = {"$regex": regex, "$options": "i"}
    if cast:
        query["stars.name"] = {"$regex": cast, "$options": "i"}
    if director:
        query["directors.name"] = {"$regex": director, "$options": "i"}
    if genre:
        query["genre"] = {"$regex": genre, "$options": "i"}

    cursor = files_col.find(query, {"_id": 0}).sort("release_date", -1).skip(offset).limit(limit)
    tmdb_entries = await cursor.to_list(length=limit)

    def serialize_file(file):
        return {
            "file_name": file.get("file_name"),
            "file_size": file.get("file_size"),
            "file_format": file.get("file_format"),
            "date": file.get("date").strftime('%Y-%m-%d %H:%M:%S') if isinstance(file.get("date"), datetime) else file.get("date", ""),
            "telegram_link": file.get("telegram_link"),
            "channel_id": file.get("channel_id"),
            "message_id": file.get("message_id"),
        }

    def serialize_tmdb_entry(entry):
        bot_username = BOT_USERNAME
        files = entry.get("files", [])
        for file in files:
            if not file.get("telegram_link"):
                file["telegram_link"] = generate_telegram_link(
                    bot_username, file.get("channel_id"), file.get("message_id")
                )
        return {
            "tmdb_id": entry.get("tmdb_id"),
            "tmdb_type": entry.get("tmdb_type"),
            "title": entry.get("title"),
            "rating": entry.get("rating"),
            "language": entry.get("language"),
            "genre": entry.get("genre"),
            "release_date": entry.get("release_date"),
            "story": entry.get("story"),
            "directors": entry.get("directors"),
            "stars": entry.get("stars"),
            "trailer_url": entry.get("trailer_url"),
            "poster_url": entry.get("poster_url"),
            "files": [serialize_file(f) for f in files]
        }

    total = await files_col.count_documents(query)
    has_more = offset + limit < total

    response_data = {
        "results": [serialize_tmdb_entry(e) for e in tmdb_entries],
        "has_more": has_more,
        "total": total
    }
    all_tmdb_files_cache[cache_key] = response_data

    return JSONResponse(response_data)