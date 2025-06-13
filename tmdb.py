import re
import aiohttp
import asyncio
from config import TMDB_API_KEY, logger

POSTER_BASE_URL = 'https://image.tmdb.org/t/p/original'
PROFILE_BASE_URL = 'https://image.tmdb.org/t/p/w500'

def profile_url(path):
    return f"{PROFILE_BASE_URL}{path}" if path else None

GENRE_EMOJI_MAP = {
    "Action": "🥊", "Adventure": "🌋", "Animation": "🎬", "Comedy": "😂",
    "Crime": "🕵️", "Documentary": "🎥", "Drama": "🎭", "Family": "👨‍👩‍👧‍👦",
    "Fantasy": "🧙", "History": "📜", "Horror": "👻", "Music": "🎵",
    "Mystery": "🕵️‍♂️", "Romance": "❤️", "ScienceFiction": "🤖",
    "Sci-Fi": "🤖", "SciFi": "🤖", "TV Movie": "📺", "Thriller": "🔪",
    "War": "⚔️", "Western": "🤠", "Sport": "🏆", "Biography": "📖"
}

def clean_genre_name(genre):
    return re.sub(r'[^A-Za-z0-9]', '', genre)

def genre_tag_with_emoji(genre):
    clean_name = clean_genre_name(genre)
    emoji = GENRE_EMOJI_MAP.get(clean_name, "")
    return f"#{clean_name}{' ' + emoji if emoji else ''}"

def extract_language(data):
    spoken_languages = data.get('spoken_languages', [])
    if spoken_languages:
        # Join all available english_name fields, fallback to 'Unknown'
        return ", ".join(lang.get('english_name', 'Unknown') for lang in spoken_languages)
    return "Unknown"

def extract_genres(data):
    genres = []
    for genre in data.get('genres', []):
        # Split genre names containing '&' into separate genres
        if '&' in genre['name']:
            parts = [g.strip() for g in genre['name'].split('&')]
            genres.extend(parts)
        else:
            genres.append(genre['name'])
    return genres

def extract_release_date(data):
    # Safely get release_date or first_air_date, fallback to empty string
    return data.get('release_date') or data.get('first_air_date', "")

def extract_directors(tmdb_type, data, credits):
    directors = []
    if tmdb_type == 'movie':
        for member in credits.get('crew', []):
            if member.get('job') == 'Director':
                directors.append({
                    "name": member.get('name'),
                    "profile_path": profile_url(member.get('profile_path'))
                })
    elif tmdb_type == 'tv':
        for creator in data.get('created_by', []):
            directors.append({
                "name": creator.get('name'),
                "profile_path": profile_url(creator.get('profile_path'))
            })
    return directors

def extract_stars(credits, limit=5):
    cast_list = credits.get('cast', [])
    if not cast_list:
        return []
    return [
        {
            "name": member.get('name'),
            "profile_path": profile_url(member.get('profile_path'))
        }
        for member in cast_list[:limit]
    ]

def get_poster_url(data):
    poster_path = data.get('poster_path')
    return f"{POSTER_BASE_URL}{poster_path}" if poster_path else None

def get_backdrop_url(movie_images):
    for key in ['backdrops', 'posters']:
        if key in movie_images and movie_images[key]:
            # Return the first available backdrop/poster path
            path = movie_images[key][0].get('file_path')
            if path:
                return f"{POSTER_BASE_URL}{path}"
    return None

async def get_trailer_url(session, tmdb_type, tmdb_id):
    video_url = f'https://api.themoviedb.org/3/{tmdb_type}/{tmdb_id}/videos?api_key={TMDB_API_KEY}'
    async with session.get(video_url) as video_response:
        if video_response.status == 200:
            data = await video_response.json()
            results = data.get('results', [])
            if results:
                for video in results:
                    if video.get('site') == 'YouTube' and video.get('type') == 'Trailer':
                        return f"https://www.youtube.com/watch?v={video.get('key')}"
    return None

async def get_by_id(tmdb_type, tmdb_id):
    api_url = f"https://api.themoviedb.org/3/{tmdb_type}/{tmdb_id}?api_key={TMDB_API_KEY}&language=en-US"
    images_url = f'https://api.themoviedb.org/3/{tmdb_type}/{tmdb_id}/images?api_key={TMDB_API_KEY}&language=en-US&include_image_language=en,hi'
    credits_url = f"https://api.themoviedb.org/3/{tmdb_type}/{tmdb_id}/credits?api_key={TMDB_API_KEY}&language=en-US"
    try:
        async with aiohttp.ClientSession() as session:
            detail_resp, images_resp, credits_resp = await asyncio.gather(
                session.get(api_url), session.get(images_url), session.get(credits_url)
            )
            data = await detail_resp.json()
            movie_images = await images_resp.json()
            credits = await credits_resp.json()

            poster_url = get_poster_url(data)
            backdrop_url = get_backdrop_url(movie_images)
            trailer_url = await get_trailer_url(session, tmdb_type, tmdb_id)

            directors_list = extract_directors(tmdb_type, data, credits)
            stars_list = extract_stars(credits)

            directors_str = ", ".join([d["name"] for d in directors_list]) if directors_list else "Unknown"
            stars_str = ", ".join([s["name"] for s in stars_list]) if stars_list else "Unknown"
            language = extract_language(data)
            genres = extract_genres(data)
            release_date = extract_release_date(data)

            message = await format_tmdb_info(directors_str, stars_str, data)

            mongo_dict = {
                "tmdb_id": tmdb_id,
                "tmdb_type": tmdb_type,
                "title": data.get('title') or data.get('name'),
                "rating": round(float(data.get('vote_average', 0)), 1),
                "language": language,
                "genre": genres,
                "release_date": release_date,
                "story": data.get('overview'),
                "directors": directors_list,
                "stars": stars_list,
                "trailer_url": trailer_url,
                "poster_url": poster_url
            }

            return {
                "message": message,
                "poster_url": poster_url,
                "backdrop_url": backdrop_url,
                "trailer_url": trailer_url,
                "mongo_dict": mongo_dict
            }

    except aiohttp.ClientError as e:
        print(f"Error fetching TMDB data: {e}")
        return {"message": f"Error: {str(e)}", "poster_url": None}
    return {"message": "Unknown error occurred.", "poster_url": None}

async def format_tmdb_info(directors_str, stars_str, data):
    genres = extract_genres(data)
    genre_tags = " ".join([genre_tag_with_emoji(g) for g in genres])
    language = extract_language(data)
    title = data.get('title') or data.get('name')
    rating = round(float(data.get('vote_average', 0)), 1)
    tagline = data.get('tagline', "")
    runtime = data.get('runtime') or (data.get('episode_run_time', [None])[0])
    release_date = extract_release_date(data)
    release_date_fmt = ""
    if release_date and len(release_date) >= 10:
        from datetime import datetime
        try:
            release_date_fmt = datetime.strptime(release_date[:10], "%Y-%m-%d").strftime("%b %d, %Y")
        except Exception:
            release_date_fmt = release_date[:10]
    else:
        release_date_fmt = release_date

    seasons_str = ""
    if data.get('media_type') == "tv":
        num_seasons = data.get('number_of_seasons')
        num_episodes = data.get('number_of_episodes')
        if num_seasons is not None and num_episodes is not None:
            seasons_str = f"<b>📺Seasons:</b> {num_seasons}  <b>🎞️Episodes:</b> {num_episodes}\n"

    message = (
        f"<b>🏷️Title:</b> {title}\n"
        f"<b>🌟Rating:</b> {rating} / 10\n"
        f"<b>⏳️Runtime:</b> {format_duration(runtime)}\n" if runtime else ""
    )
    message += f"<b>🅰️Language:</b> {language}\n"
    message += f"<b>⚙️Genre:</b> {genre_tags}\n" if genre_tags else ""
    message += f"<b>📆Release:</b> {release_date_fmt}\n" if release_date_fmt else ""
    message += seasons_str
    message += f"<b>🎬Director:</b> {directors_str}\n"
    message += f"<b>⭐Stars:</b> {stars_str}\n"
    message += "\n"
    message += f"{tagline}\n" if tagline else ""

    return message.strip()

def truncate_overview(overview):
    MAX_OVERVIEW_LENGTH = 600
    return overview[:MAX_OVERVIEW_LENGTH] + "..." if len(overview) > MAX_OVERVIEW_LENGTH else overview

def format_duration(duration):
    try:
        mins = int(duration)
        hours = mins // 60
        mins = mins % 60
        return f"{hours}h {mins:02d}min" if hours else f"{mins}min"
    except Exception:
        return str(duration) if duration else ""

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
                        return {
                            "id": result['id'],
                            "media_type": result['media_type']
                        }
        return None
    except Exception as e:
        print(f"Error fetching TMDb ID: {e}")
        return