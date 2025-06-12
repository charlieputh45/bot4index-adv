import aiohttp
import imdb
from config import TMDB_API_KEY

POSTER_BASE_URL = 'https://image.tmdb.org/t/p/original'

def profile_url(path):
    """
    Return the full TMDB profile image URL or None.
    """
    return f"https://image.tmdb.org/t/p/w500{path}" if path else None

async def get_by_id(tmdb_type, tmdb_id):
    api_url = f"https://api.themoviedb.org/3/{tmdb_type}/{tmdb_id}?api_key={TMDB_API_KEY}&language=en-US"
    tmdb_movie_image_url = f'https://api.themoviedb.org/3/{tmdb_type}/{tmdb_id}/images?api_key={TMDB_API_KEY}&language=en-US&include_image_language=en,hi'
    credits_url = f"https://api.themoviedb.org/3/{tmdb_type}/{tmdb_id}/credits?api_key={TMDB_API_KEY}&language=en-US"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(api_url) as detail_response:
                data = await detail_response.json()
                async with session.get(tmdb_movie_image_url) as movie_response:
                    movie_images = await movie_response.json()
                async with session.get(credits_url) as resp:
                    credits = await resp.json()

                message, imdb_dict = await format_tmdb_info(tmdb_type, tmdb_id, data)

                # --- Extract Poster URL ---
                poster_path = data.get('poster_path')
                poster_url = f"{POSTER_BASE_URL}{poster_path}" if poster_path else None

                backdrop_path = data.get('backdrop_path', None)
                if 'backdrops' in movie_images and movie_images['backdrops']:
                    backdrop_path = movie_images['backdrops'][0]['file_path']
                elif 'posters' in movie_images and movie_images['posters']:
                    backdrop_path = movie_images['posters'][0]['file_path']
                if backdrop_path:
                    backdrop_url = f"https://image.tmdb.org/t/p/original{backdrop_path}" if backdrop_path else None

                # Fetch trailer URL if available
                video_url = f'https://api.themoviedb.org/3/{tmdb_type}/{tmdb_id}/videos?api_key={TMDB_API_KEY}'
                async with session.get(video_url) as video_response:
                    video_data = await video_response.json()
                    trailer_url = None
                    for video in video_data.get('results', []):
                        if video['site'] == 'YouTube' and video['type'] == 'Trailer':
                            trailer_url = f"https://www.youtube.com/watch?v={video['key']}"
                            break

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

                mongo_dict = {
                    "tmdb_id": tmdb_id,
                    "tmdb_type": tmdb_type,
                    "title": imdb_dict.get('title'),
                    "rating": imdb_dict.get('rating'),
                    "language": imdb_dict.get('language'),
                    "genre": imdb_dict.get('genre'),
                    "release_date": imdb_dict.get('release_date'),
                    "story": imdb_dict.get('summary'),
                    "directors": directors_list,
                    "stars": stars_list,
                    "trailer_url": trailer_url,
                    "poster_url": poster_url
                }
                return {
                    "message": message,
                    "poster_url": poster_url,
                    "backdrop_url": backdrop_url,
                    "mongo_dict": mongo_dict,
                    "trailer_url": trailer_url,
                }
                
    except aiohttp.ClientError as e:
        print(f"Error fetching TMDB data: {e}")
        return {"message": f"Error: {str(e)}", "poster_url": None}
    # Default return if no exception and no data
    return {"message": "Unknown error occurred.", "poster_url": None}

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

async def format_tmdb_info(tmdb_type, movie_id, data):
    cast_crew = await get_cast_and_crew(tmdb_type, movie_id)
    genres = " ".join([f"#{genre['name'].replace(' ', '').replace('-', '').replace('&', '')}" for genre in data.get('genres', [])])

    if tmdb_type == 'movie':
        imdb_id = data.get('imdb_id')
        imdb_info = get_imdb_details(imdb_id) if imdb_id else {}

        title = imdb_info.get('title') or data.get('title')
        rating = imdb_info.get('rating')
        duration = format_duration(imdb_info.get('duration'))
        language = imdb_info.get('language')
        genre = imdb_info.get('genre')
        genre_tags = genre_to_tags(genre)
        release_date = imdb_info.get('release_date') or (data.get('release_date', '')[:10] if data.get('release_date') else "")
        release_date = str(release_date) if release_date is not None else ""
        summary = imdb_info.get('story') or truncate_overview(data.get('overview'))
        director = imdb_info.get('director') or (cast_crew.get('director') if cast_crew.get('director') else None)
        starring = imdb_info.get('stars') or (", ".join(cast_crew.get('starring', [])) if cast_crew.get('starring') else None)

        # Format release date
        if release_date and len(release_date) == 10:
            from datetime import datetime
            try:
                release_date_fmt = datetime.strptime(release_date, "%Y-%m-%d").strftime("%b %d, %Y")
            except Exception:
                release_date_fmt = release_date
        else:
            release_date_fmt = release_date

        # Build the message
        message = (
            f"<b>🏷️Title:</b> {title}\n"
            f"<b>🌟Rating:</b> {rating} / 10\n" if rating else ""
        )
        message += f"<b>⏳️Duration:</b> {duration}\n" if duration else ""
        message += f"<b>🅰️Language:</b> {language}\n" if language else ""
        message += f"<b>⚙️Genre:</b> {genre_tags}\n" if genre_tags else ""
        message += f"<b>📆Release:</b> {release_date_fmt}\n" if release_date_fmt else ""
        message += "\n"
        message += f"<b>📝Story:</b> {summary}\n" if summary else ""
        message += f"<b>Directors:</b>  {director}\n" if director else ""
        message += f"<b>Stars:</b>  {starring}\n" if starring else ""

        imdb_dict = {
            "title": title,
            "rating": rating,
            "language": language,
            "genre": genre,
            "release_date": release_date_fmt,
            "summary": summary,
        }

        return message.strip(), imdb_dict

    elif tmdb_type == 'tv':
        imdb_id = await get_tv_imdb_id(movie_id)
        imdb_info = get_imdb_details(imdb_id) if imdb_id else {}

        title = imdb_info.get('title') or data.get('name')
        rating = imdb_info.get('rating') or data.get('vote_average')
        # duration removed for TV
        language = imdb_info.get('language') or (", ".join(data.get('languages', [])) if data.get('languages') else "")
        genre = imdb_info.get('genre') or ", ".join([g['name'] for g in data.get('genres', [])])
        genre_tags = genre_to_tags(genre)
        release_date = imdb_info.get('release_date') or (data.get('first_air_date', '')[:10] if data.get('first_air_date') else "")
        release_date = str(release_date) if release_date is not None else ""
        summary = imdb_info.get('story') or truncate_overview(data.get('overview'))
        director = imdb_info.get('director') or (
            ", ".join([creator['name'] for creator in data.get('created_by', [])]) if data.get('created_by') else None
        )
        starring = imdb_info.get('stars') or (
            ", ".join(cast_crew.get('starring', [])) if cast_crew.get('starring') else None
        )

        # Format release date
        if release_date and len(release_date) == 10:
            from datetime import datetime
            try:
                release_date_fmt = datetime.strptime(release_date, "%Y-%m-%d").strftime("%b %d, %Y")
            except Exception:
                release_date_fmt = release_date
        else:
            release_date_fmt = release_date

        message = (
            f"<b>🏷️Title:</b> {title}\n"
            f"<b>🌟Rating:</b> {rating} / 10\n" if rating else ""
        )
        # No duration for TV
        message += f"<b>🅰️Language:</b> {language}\n" if language else ""
        message += f"<b>⚙️Genre:</b> {genre_tags}\n" if genre_tags else ""
        message += f"<b>📆Release:</b> {release_date_fmt}\n" if release_date_fmt else ""
        message += "\n"
        message += f"<b>📝Story:</b> {summary}\n" if summary else ""
        message += f"<b>Directors:</b>  {director}\n" if director else ""
        message += f"<b>Stars:</b>  {starring}\n" if starring else ""

        imdb_dict = {
            "title": title,
            "rating": rating,
            "language": language,
            "genre": genre,
            "release_date": release_date_fmt,
            "summary": summary,
        }

        return message.strip(), imdb_dict

    else:
        return "Unknown type. Unable to format information."

async def get_cast_and_crew(tmdb_type, movie_id):
    """
    Fetches the cast and crew details (starring actors and director) for a movie or TV show.
    
    Args:
    - tmdb_type (str): The type of TMDb entity ('movie', 'tv').
    - movie_id (int): The TMDb movie or TV show ID.

    Returns:
    - dict: A dictionary containing the starring actors and director.
    """
    cast_crew_url = f'https://api.themoviedb.org/3/{tmdb_type}/{movie_id}/credits?api_key={TMDB_API_KEY}&language=en-US'
    
    async with aiohttp.ClientSession() as session:
        async with session.get(cast_crew_url) as response:
            cast_crew_data = await response.json()

    # Get starring actors (first 3 cast members) and director
    starring = [member['name'] for member in cast_crew_data.get('cast', [])[:5]]
    director = next((member['name'] for member in cast_crew_data.get('crew', []) if member['job'] == 'Director'), 'N/A')

    return {"starring": starring, "director": director}


def truncate_overview(overview):
    """
    Truncate the overview if it exceeds the specified limit.

    Args:
    - overview (str): The overview text from the API.

    Returns:
    - str: Truncated overview with an ellipsis if it exceeds the limit.
    """
    MAX_OVERVIEW_LENGTH = 600  # Define your maximum character length for the summary
    if len(overview) > MAX_OVERVIEW_LENGTH:
        return overview[:MAX_OVERVIEW_LENGTH] + "..."
    return overview

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
                            "media_type": media_type
                        }


        return None  # No matching results found
    except Exception as e:
        print(f"Error fetching TMDb ID: {e}")
        return None

def genre_to_tags(genre):
    """
    Convert genre string to emoji-rich hashtag tags.
    """
    emoji_map = {
        "Action": "🥊",
        "SciFi": "🤖",
        "Sci-Fi": "🤖",
        "Science Fiction": "🤖",
        "Adventure": "🌋",
        "Drama": "🎭",
        "Comedy": "😂",
        "Horror": "👻",
        "Thriller": "🔪",
        "Romance": "❤️",
        "Animation": "🎬",
        "Crime": "🕵️",
        "Fantasy": "🧙",
        "Mystery": "🕵️‍♂️",
        "Family": "👨‍👩‍👧‍👦",
        "Biography": "📖",
        "History": "📜",
        "War": "⚔️",
        "Music": "🎵",
        "Western": "🤠",
        "Sport": "🏆",
        "Documentary": "🎥"
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

async def get_tv_imdb_id(tv_id):
    url = f"https://api.themoviedb.org/3/tv/{tv_id}/external_ids?api_key={TMDB_API_KEY}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            data = await resp.json()
            return data.get("imdb_id")