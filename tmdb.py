import aiohttp
import imdb
from config import TMDB_API_KEY


POSTER_BASE_URL = 'https://image.tmdb.org/t/p/original'

# =========================
# Utility Functions
# =========================

def genre_to_tags(genre):
    """
    Convert genre string to emoji-rich hashtag tags.
    """
    emoji_map = {
        "Action": "🥊", "SciFi": "🤖", "Sci-Fi": "🤖", "Science Fiction": "🤖",
        "Adventure": "🌋", "Drama": "🎭", "Comedy": "😂", "Horror": "👻",
        "Thriller": "🔪", "Romance": "❤️", "Animation": "🎬", "Crime": "🕵️",
        "Fantasy": "🧙", "Mystery": "🕵️‍♂️", "Family": "👨‍👩‍👧‍👦", "Biography": "📖",
        "History": "📜", "War": "⚔️", "Music": "🎵", "Western": "🤠",
        "Sport": "🏆", "Documentary": "🎥"
    }
    tags = []
    if genre:
        for g in genre.split(","):
            g = g.strip()
            # Normalize Sci-Fi and Science Fiction to SciFi
            if g in ["Sci-Fi", "Science Fiction"]:
                tag = "#SciFi 🤖"
            else:
                emoji = emoji_map.get(g, "")
                tag = f"#{g.replace(' ', '')} {emoji}".strip()
            tags.append(tag)
    return "  ".join(tags)

def format_duration(duration):
    """
    Format duration in minutes to 'Xh YYmin' format.
    """
    try:
        mins = int(duration)
        hours = mins // 60
        mins = mins % 60
        return f"{hours}h {mins:02d}min" if hours else f"{mins}min"
    except Exception:
        return duration or ""

def truncate_overview(overview):
    """
    Truncate the overview if it exceeds the specified limit.
    """
    MAX_OVERVIEW_LENGTH = 600
    if overview and len(overview) > MAX_OVERVIEW_LENGTH:
        return overview[:MAX_OVERVIEW_LENGTH] + "..."
    return overview

def profile_url(path):
    """
    Return the full TMDB profile image URL or None.
    """
    return f"https://image.tmdb.org/t/p/w500{path}" if path else None

# =========================
# IMDb/ API
# =========================

def get_imdb_details(imdb_id):
    ia = imdb.IMDb()
    movie = ia.get_movie(imdb_id.replace('tt', ''))
    if not movie:
        return {}

    return {
        "title": movie.get('title'),
        "rating": movie.get('rating'),
        "duration": movie.get('runtime', [None])[0],
        "language": ", ".join(movie.get('languages', [])),
        "genre": ", ".join(movie.get('genres', [])),
        "release_date": movie.get('original air date') or movie.get('year'),
        "story": movie.get('plot', [None])[0],
        "director": ", ".join([d['name'] for d in movie.get('director', [])]),
        "stars": ", ".join([a['name'] for a in movie.get('cast', [])[:5]])
    }
# =========================
# TMDB API
# =========================

async def get_tv_imdb_id(tv_id):
    """
    Fetch IMDb ID for a TV show from TMDB.
    """
    url = f"https://api.themoviedb.org/3/tv/{tv_id}/external_ids?api_key={TMDB_API_KEY}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            data = await resp.json()
            return data.get("imdb_id")

async def get_tmdb_info_dict(tmdb_type, tmdb_id, season=None, episode=None):
    """
    Fetch TMDB info, use IMDb only for rating and storyline, and return a dict for MongoDB storage and messaging.
    Includes trailer, poster, directors, stars, and a formatted message.
    """
    # --- Build URLs ---
    api_url = f"https://api.themoviedb.org/3/{tmdb_type}/{tmdb_id}?api_key={TMDB_API_KEY}&language=en-US"
    credits_url = f"https://api.themoviedb.org/3/{tmdb_type}/{tmdb_id}/credits?api_key={TMDB_API_KEY}&language=en-US"
    video_url = f"https://api.themoviedb.org/3/{tmdb_type}/{tmdb_id}/videos?api_key={TMDB_API_KEY}"
    # --- Fetch TMDB Data ---
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(api_url) as resp:
                data = await resp.json()
            async with session.get(credits_url) as resp:
                credits = await resp.json()
            async with session.get(video_url) as resp:
                video_data = await resp.json()
        except Exception as e:
            print(f"Error fetching TMDB data: {e}")
            return {}

    # --- Extract Trailer URL ---
    trailer_url = None
    for video in video_data.get('results', []):
        if video.get('site') == 'YouTube' and video.get('type') == 'Trailer':
            trailer_url = f"https://www.youtube.com/watch?v={video['key']}"
            break

    # --- Extract Poster URL ---
    poster_path = data.get('poster_path')
    poster_url = f"{POSTER_BASE_URL}{poster_path}" if poster_path else None

    # --- Extract Directors and Stars ---
    directors_list = []
    stars_list = []
    if tmdb_type == 'movie':
        for member in credits.get('crew', []):
            if member.get('job') == 'Director':
                directors_list.append({
                    "name": member.get('name'),
                    "profile_path": profile_url(member.get('profile_path'))
                })
    elif tmdb_type == 'tv':
        for creator in data.get('created_by', []):
            directors_list.append({
                "name": creator.get('name'),
                "profile_path": profile_url(creator.get('profile_path'))
            })
    for member in credits.get('cast', [])[:5]:
        stars_list.append({
            "name": member.get('name'),
            "profile_path": profile_url(member.get('profile_path'))
        })

    directors_names = ", ".join([d["name"] for d in directors_list])
    stars_names = ", ".join([s["name"] for s in stars_list])

    # --- IMDb Info (only for rating and story) ---
    imdb_rating = None
    imdb_story = None

    if tmdb_type == 'movie':
        imdb_id = data.get('imdb_id')
        if imdb_id:
            imdb_info = get_imdb_details(imdb_id)
            imdb_rating = imdb_info.get('rating')
            imdb_story = imdb_info.get('story')
        title = data.get('title')
        rating = imdb_rating or (f"{data.get('vote_average', 0):.1f}" if data.get('vote_average') is not None else None)
        language = data.get('original_language')
        genres = data.get('genres', [])
        genre = ", ".join([g.get('name', '') for g in genres])
        genre_tags = genre_to_tags(genre)
        release_date = (data.get('release_date', '')[:10] if data.get('release_date') else "")
        story = imdb_story or data.get('overview')
        duration = data.get('runtime')

        # --- Format Release Date ---
        if release_date and len(release_date) == 10:
            from datetime import datetime
            try:
                release_date_fmt = datetime.strptime(release_date, "%Y-%m-%d").strftime("%b %d, %Y")
            except Exception:
                release_date_fmt = release_date
        else:
            release_date_fmt = release_date

        # Format duration
        duration_fmt = format_duration(duration) if duration else ""

        # Build the message
        message = f"<b>🏷️Title:</b> {title}\n"
        if rating:
            message += f"<b>🌟Rating:</b> {rating} / 10\n"
        if duration_fmt:
            message += f"<b>⏳️Duration:</b> {duration_fmt}\n"
        if language:
            message += f"<b>🅰️Language:</b> {language}\n"
        if genre_tags:
            message += f"<b>⚙️Genre:</b> {genre_tags}\n"
        if release_date_fmt:
            message += f"<b>📆Release:</b> {release_date_fmt}\n"
        message += "\n"
        if story:
            message += f"<b>📝Story:</b> {story}\n"
        if directors_names:
            message += f"<b>Directors:</b>  {directors_names}\n"
        if stars_names:
            message += f"<b>Stars:</b>  {stars_names}\n"

    elif tmdb_type == 'tv':
        imdb_id = await get_tv_imdb_id(tmdb_id)
        if imdb_id:
            imdb_info = get_imdb_details(imdb_id)
            imdb_rating = imdb_info.get('rating')
            imdb_story = imdb_info.get('story')
        title = data.get('name')
        rating = imdb_rating or (f"{data.get('vote_average', 0):.1f}" if data.get('vote_average') is not None else None)
        language = data.get('original_language')
        genres = data.get('genres', [])
        genre = ", ".join([g.get('name', '') for g in genres])
        genre_tags = genre_to_tags(genre)
        release_date = (data.get('first_air_date', '')[:10] if data.get('first_air_date') else "")
        story = imdb_story or data.get('overview')
        duration = None

        # --- Format Release Date ---
        if release_date and len(release_date) == 10:
            from datetime import datetime
            try:
                release_date_fmt = datetime.strptime(release_date, "%Y-%m-%d").strftime("%b %d, %Y")
            except Exception:
                release_date_fmt = release_date
        else:
            release_date_fmt = release_date

        message = f"<b>🏷️Title:</b> {title}\n"
        if season:
            message += f"<b>📺Season:</b> {season}\n"
        if episode:
            message += f"<b>📺Episode:</b> {episode}\n"
        if rating:
            message += f"<b>🌟Rating:</b> {rating} / 10\n"
        if language:
            message += f"<b>🅰️Language:</b> {language}\n"
        if genre_tags:
            message += f"<b>⚙️Genre:</b> {genre_tags}\n"
        if release_date_fmt:
            message += f"<b>📆Release:</b> {release_date_fmt}\n"
        message += "\n"
        if story:
            message += f"<b>📝Story:</b> {story}\n"
        if directors_names:
            message += f"<b>Creators:</b>  {directors_names}\n"
        if stars_names:
            message += f"<b>Stars:</b>  {stars_names}\n"

    else:
        return {}

    # --- Return All Info ---
    return {
        "tmdb_id": tmdb_id,
        "tmdb_type": tmdb_type,
        "title": title,
        "rating": rating,
        "language": language,
        "genre": genre,
        "release_date": release_date,
        "story": story,
        "directors": directors_list,
        "stars": stars_list,
        "trailer_url": trailer_url,
        "poster_url": poster_url,
        "message": message.strip()
    }

async def get_by_name(movie_name, release_year):
    tmdb_search_url = f'https://api.themoviedb.org/3/search/multi?api_key={TMDB_API_KEY}&query={movie_name}'

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(tmdb_search_url) as search_response:
                search_data = await search_response.json()

                if search_data['results']:
                    matching_results = [
                        result for result in search_data['results']
                        if ('release_date' in result and result['release_date'][:4] == str(release_year)) or
                        ('first_air_date' in result and result['first_air_date'][:4] == str(release_year))
                    ]

                    if matching_results:
                        result = matching_results[0]
                        media_type = result['media_type']
                        tmdb_id = result['id']
                        
                        return {
                            "id": tmdb_id,
                            "media_type": media_type,
                        }


        return None  # No matching results found
    except Exception as e:
        print(f"Error fetching TMDb ID: {e}")
        return None