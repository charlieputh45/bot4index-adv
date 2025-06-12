from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from db import allowed_channels_col, files_col, n_files_col
from config import BOT_USERNAME, MY_DOMAIN
from utility import generate_telegram_link
from datetime import datetime, timedelta

CACHE_TTL_SECONDS = 300  # 5 minutes

class ExpiringCache:
    def __init__(self, ttl_seconds):
        self.ttl = ttl_seconds
        self._cache = {}

    def get(self, key):
        entry = self._cache.get(key)
        if not entry:
            return None
        value, expires_at = entry
        if datetime.utcnow() > expires_at:
            del self._cache[key]
            return None
        return value

    def set(self, key, value):
        expires_at = datetime.utcnow() + timedelta(seconds=self.ttl)
        self._cache[key] = (value, expires_at)

    def clear(self):
        self._cache.clear()

all_tmdb_files_cache = ExpiringCache(CACHE_TTL_SECONDS)

api = FastAPI()
api.add_middleware(
    CORSMiddleware,
    allow_origins=[f"{MY_DOMAIN}"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def build_query(params: dict, search_fields: dict):
    """
    Build a MongoDB query dict from params and search_fields mapping.
    search_fields: {param_name: (db_field, regex_search: bool)}
    """
    query = {}
    for param, (db_field, use_regex) in search_fields.items():
        value = params.get(param, "")
        if value:
            if use_regex:
                regex = ".*".join(map(lambda s: s, value.strip().split()))
                query[db_field] = {"$regex": regex, "$options": "i"}
            else:
                query[db_field] = value
    return query

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

def serialize_n_file(file):
    bot_username = BOT_USERNAME
    telegram_link = generate_telegram_link(
        bot_username, file.get("channel_id"), file.get("message_id")
    )
    return {
        "file_name": file.get("file_name"),
        "file_size": file.get("file_size"),
        "file_format": file.get("file_format"),
        "date": file.get("date").strftime('%Y-%m-%d %H:%M:%S') if isinstance(file.get("date"), datetime) else file.get("date", ""),
        "telegram_link": telegram_link,
        "channel_id": file.get("channel_id"),
        "message_id": file.get("message_id"),
        "ss_url": file.get("ss_url", ""),
        "thumb_url": file.get("thumb_url", "")
    }

@api.get("/")
async def root():
    return JSONResponse({"message": "👋 Hello! Welcome to the Sharing Bot"})

@api.get("/api/all-tmdb-files")
async def api_all_tmdb_files(
    q: str = "",
    cast: str = "",
    director: str = "",
    genre: str = "",
    tmdb_type: str = "",
    offset: int = 0,
    limit: int = 10,
    sort: str = "date",
    order: str = "desc"
):
    """
    Return TMDB entries with their files, sorted and filtered.
    """
    cache_key = f"{q}:{cast}:{director}:{genre}:{tmdb_type}:{offset}:{limit}:{sort}:{order}"
    cached = all_tmdb_files_cache.get(cache_key)
    if cached:
        return JSONResponse(cached)

    search_fields = {
        "q": ("title", True),
        "cast": ("stars.name", True),
        "director": ("directors.name", True),
        "genre": ("genre", True),
        "tmdb_type": ("tmdb_type", False),
    }
    params = dict(q=q, cast=cast, director=director, genre=genre, tmdb_type=tmdb_type)
    query = build_query(params, search_fields)

    sort_field = "date" if sort not in ["rating", "date"] else sort
    sort_order = -1

    cursor = files_col.find(query, {"_id": 0}).sort(sort_field, sort_order).skip(offset).limit(limit)
    tmdb_entries = await cursor.to_list(length=limit)

    total = await files_col.count_documents(query)
    has_more = offset + limit < total

    response_data = {
        "results": [serialize_tmdb_entry(e) for e in tmdb_entries],
        "has_more": has_more,
        "total": total
    }
    all_tmdb_files_cache.set(cache_key, response_data)
    return JSONResponse(response_data)

@api.get("/api/all-n-files")
async def api_all_n_files(
    q: str = "",
    offset: int = 0,
    limit: int = 10
):
    """
    Return entries from n_files_col, paginated and filtered by search query.
    """
    cache_key = f"nfiles:{q}:{offset}:{limit}"
    cached = all_tmdb_files_cache.get(cache_key)
    if cached:
        return JSONResponse(cached)

    search_fields = {
        "q": ("file_name", True),
    }
    params = dict(q=q)
    query = build_query(params, search_fields)

    cursor = n_files_col.find(query, {"_id": 0}).sort("date", -1).skip(offset).limit(limit)
    n_files = await cursor.to_list(length=limit)

    total = await n_files_col.count_documents(query)
    has_more = offset + limit < total

    response_data = {
        "results": [serialize_n_file(f) for f in n_files],
        "has_more": has_more,
        "total": total
    }
    all_tmdb_files_cache.set(cache_key, response_data)
    return JSONResponse(response_data)