import copy
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from db import files_col, n_files_col
from config import BOT_USERNAME, MY_DOMAIN
from datetime import datetime, timedelta, timezone
from utility import generate_telegram_link
from typing import Any, Dict
from threading import Lock

# =========================
# Cache System
# =========================
CACHE_TTL_SECONDS = 300  # 5 minutes

class ExpiringCache:
    def __init__(self, ttl_seconds: int):
        self.ttl = ttl_seconds
        self._cache: Dict[str, Any] = {}
        self._lock = Lock()

    def get(self, key: str):
        with self._lock:
            entry = self._cache.get(key)
            if not entry:
                return None
            value, expires_at = entry
            if datetime.now(timezone.utc) > expires_at:
                del self._cache[key]
                return None
            # Return a deepcopy to avoid mutation issues
            return copy.deepcopy(value)

    def set(self, key: str, value: Any):
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=self.ttl)
        # Store a deepcopy to avoid mutation issues
        with self._lock:
            self._cache[key] = (copy.deepcopy(value), expires_at)

    def clear(self):
        with self._lock:
            self._cache.clear()

all_tmdb_files_cache = ExpiringCache(CACHE_TTL_SECONDS)
all_n_files_cache = ExpiringCache(CACHE_TTL_SECONDS)  


api = FastAPI()
api.add_middleware(
    CORSMiddleware,
    allow_origins=[f"{MY_DOMAIN}"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def build_query(params: dict, search_fields: dict) -> dict:
    """
    Build a MongoDB query dict from params and search_fields mapping.
    search_fields: {param_name: (db_field, regex_search: bool)}
    """
    query = {}
    for param, (db_field, use_regex) in search_fields.items():
        value = params.get(param, "")
        if value:
            if use_regex:
                regex = ".*".join(map(str, value.strip().split()))
                query[db_field] = {"$regex": regex, "$options": "i"}
            else:
                query[db_field] = value
    return query

# --- Serialization Helpers ---

def serialize_file(file: dict) -> dict:
    date_val = file.get("date")
    return {
        "file_name": file.get("file_name"),
        "file_size": file.get("file_size"),
        "file_format": file.get("file_format"),
        "date": date_val.strftime('%Y-%m-%d %H:%M:%S') if isinstance(date_val, datetime) else date_val or "",
        "telegram_link": file.get("telegram_link"),
        "channel_id": file.get("channel_id"),
        "message_id": file.get("message_id"),
    }

def serialize_tmdb_entry(entry: dict) -> dict:
    files = entry.get("files", [])
    for file in files:
        if not file.get("telegram_link"):
            file["telegram_link"] = generate_telegram_link(
                BOT_USERNAME, file.get("channel_id"), file.get("message_id")
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

def serialize_n_file(file: dict) -> dict:
    date_val = file.get("date")
    return {
        "file_name": file.get("file_name"),
        "file_size": file.get("file_size"),
        "file_format": file.get("file_format"),
        "date": date_val.strftime('%Y-%m-%d %H:%M:%S') if isinstance(date_val, datetime) else date_val or "",
        "telegram_link": generate_telegram_link(
            BOT_USERNAME, file.get("channel_id"), file.get("message_id")
        ),
        "channel_id": file.get("channel_id"),
        "message_id": file.get("message_id"),
        "ss_url": file.get("ss_url", ""),
        "thumb_url": file.get("thumb_url", "")
    }

def make_cache_key(*args, **kwargs) -> str:
    """Create a cache key from args and kwargs."""
    key = ":".join(map(str, args))
    if kwargs:
        key += ":" + ":".join(f"{k}={v}" for k, v in sorted(kwargs.items()))
    return key

# --- API Endpoints ---

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
    cache_key = make_cache_key(q, cast, director, genre, tmdb_type, offset, limit, sort, order)
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

    sort_field = sort if sort in ["rating", "_id", "release_date"] else "release_date"
    sort_order = -1 if order == "desc" else 1

    # Compound sort: always add _id as secondary
    sort_list = [(sort_field, sort_order)]
    if sort_field != "_id":
        sort_list.append(("_id", -1))  # Always descending for _id for stability

    cursor = files_col.find(query, {"_id": 0}).sort(sort_list).skip(offset).limit(limit)
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
    cache_key = make_cache_key("nfiles", q, offset, limit)
    cached = all_n_files_cache.get(cache_key)  # Use the new cache here
    if cached:
        return JSONResponse(cached)

    search_fields = {
        "q": ("file_name", True),
    }
    params = dict(q=q)
    query = build_query(params, search_fields)

    cursor = n_files_col.find(query, {"_id": 0}).sort("_id", -1).skip(offset).limit(limit)
    n_files = await cursor.to_list(length=limit)

    total = await n_files_col.count_documents(query)
    has_more = offset + limit < total

    response_data = {
        "results": [serialize_n_file(f) for f in n_files],
        "has_more": has_more,
        "total": total
    }
    all_n_files_cache.set(cache_key, response_data)  # Use the new cache here
    return JSONResponse(response_data)