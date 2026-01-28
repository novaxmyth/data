from asyncio import sleep
from aiohttp import ClientSession
from datetime import datetime
from pyrogram.handlers import MessageHandler, CallbackQueryHandler
from pyrogram.types import InputMediaPhoto
from pyrogram.filters import command
from pyrogram import filters

from bot import bot, LOGGER, config_dict
from bot.helper.ext_utils.bot_utils import new_task
from bot.helper.telegram_helper.message_utils import send_message, edit_message, delete_message
from bot.helper.telegram_helper.filters import CustomFilters
from bot.helper.telegram_helper.button_build import ButtonMaker

FAILED_PIC = "https://telegra.ph/file/09733b49f3a9d5b147d21.png"

ANIME_QUERY = """
query ($id: Int, $idMal:Int, $search: String) {
    Media (id: $id, idMal: $idMal, search: $search, type: ANIME) {
        id
        idMal
        title {
            romaji
            english
            native
        }
        format
        status
        episodes
        duration
        countryOfOrigin
        source (version: 2)
        trailer {
            id
            site
        }
        genres
        tags {
            name
        }
        averageScore
        relations {
            edges {
                node {
                    title {
                        romaji
                        english
                    }
                    id
                    type
                }
                relationType
            }
        }
        nextAiringEpisode {
            timeUntilAiring
            episode
        }
        isAdult
        siteUrl
    }
}
"""

CHARACTER_QUERY = """
query ($search: String, $page: Int) {
    Page (perPage: 1, page: $page) {
        pageInfo {
            total
            hasNextPage
        }
        characters (search: $search) {
            id
            name {
                full
                native
            }
            image {
                large
            }
            media (type: ANIME) {
                edges {
                    node {
                        title {
                            romaji
                        }
                        type
                    }
                    voiceActors (language: JAPANESE) {
                        name {
                            full
                        }
                        siteUrl
                    }
                }
            }
            siteUrl
        }
    }
}
"""

MANGA_QUERY = """
query ($search: String, $page: Int) {
    Page (perPage: 1, page: $page) {
        pageInfo {
            total
            hasNextPage
        }
        media (search: $search, type: MANGA) {
            id
            title {
                romaji
                english
                native
            }
            format
            countryOfOrigin
            source (version: 2)
            status
            description(asHtml: false)
            chapters
            volumes
            averageScore
            siteUrl
            isAdult
        }
    }
}
"""

AIR_QUERY = """
query ($search: String, $page: Int) {
    Page (perPage: 1, page: $page) {
        pageInfo {
            total
            hasNextPage
        } 
        media (search: $search, type: ANIME) {
            id
            title {
                romaji
                english
            }
            status
            countryOfOrigin
            nextAiringEpisode {
                timeUntilAiring
                episode
            }
            siteUrl
            isAdult
        }
    }
}
"""

TOP_QUERY = """
query ($gnr: String, $page: Int) {
    Page (perPage: 15, page: $page) {
        pageInfo {
            lastPage
            total
            hasNextPage
        }
        media (genre: $gnr, sort: SCORE_DESC, type: ANIME) {
            title {
                romaji
            }
        }
    }
}
"""

ALLTOP_QUERY = """
query ($page: Int) {
    Page (perPage: 15, page: $page) {
        pageInfo {
            lastPage
            total
            hasNextPage
        }
        media (sort: SCORE_DESC, type: ANIME) {
            title {
                romaji
            }
        }
    }
}
"""

GET_GENRES = """
query {
    GenreCollection
}
"""

GET_TAGS = """
query {
    MediaTagCollection {
        name
        isAdult
    }
}
"""

DES_INFO_QUERY = """
query ($id: Int) {
    Media (id: $id) {
        id
        description (asHtml: false)
    }
}
"""

CHA_INFO_QUERY = """
query ($id: Int, $page: Int) {
    Media (id: $id, type: ANIME) {
        id
        characters (page: $page, perPage: 25, sort: ROLE) {
            pageInfo {
                hasNextPage
                lastPage
                total
            }
            edges {
                node {
                    name {
                        full
                    }
                }
                role
            }
        }
    }
}
"""

BROWSE_QUERY = """
query ($s: MediaSeason, $y: Int, $sort: [MediaSort]) {
    Page {
        media (season: $s, seasonYear: $y, sort: $sort) {
            title {
                romaji
            }
            format
        }
    }
}
"""

ANIME_TEMPLATE = """{name}

**ID | MAL ID:** `{idm}` | `{idmal}`
➤ **Source:** `{source}`
➤ **Type:** `{formats}`{avscd}{dura}
{status_air}{gnrs_}{tags_}

🎬 {trailer_link}
📖 <a href="{url}">Official Site</a>

{additional}"""


async def return_json_senpai(query: str, vars_: dict) -> dict:
    url = "https://graphql.anilist.co"
    headers = {
        'Content-Type': 'application/json',
        'Accept': 'application/json',
    }
    
    async with ClientSession() as session:
        async with session.post(
            url,
            json={"query": query, "variables": vars_},
            headers=headers
        ) as response:
            return await response.json()


def cflag(country: str) -> str:
    flags = {
        "JP": "🇯🇵",
        "CN": "🇨🇳",
        "KR": "🇰🇷",
        "TW": "🇹🇼"
    }
    return flags.get(country, "🌍")
    

def pos_no(no: str) -> str:
    ep_ = list(str(no))
    x = ep_.pop()
    if ep_ and ep_.pop() == '1':
        return 'th'
    return "st" if x == "1" else "nd" if x == "2" else "rd" if x == "3" else "th"


def make_it_rw(time_stamp: int) -> str:
    seconds, milliseconds = divmod(int(time_stamp), 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    
    parts = []
    if days:
        parts.append(f"{days} Days")
    if hours:
        parts.append(f"{hours} Hours")
    if minutes:
        parts.append(f"{minutes} Minutes")
    if seconds:
        parts.append(f"{seconds} Seconds")
    
    return ", ".join(parts) if parts else "0 Seconds"


def season_(future: bool = False):
    now = datetime.now()
    m = now.month + (3 if future else 0)
    y = now.year + (1 if m > 12 else 0)
    
    m = m if m <= 12 else m - 12
    
    if m in [1, 2, 3]:
        return 'WINTER', y
    elif m in [4, 5, 6]:
        return 'SPRING', y
    elif m in [7, 8, 9]:
        return 'SUMMER', y
    else:
        return 'FALL', y


async def send_photo(message, photo_url, caption, buttons=None):
    try:
        return await message.reply_photo(
            photo=photo_url,
            caption=caption,
            quote=True,
            reply_markup=buttons,
            disable_notification=True
        )
    except Exception as e:
        LOGGER.warning(f"Failed to send photo {photo_url}: {e}")
        try:
            return await message.reply_photo(
                photo=FAILED_PIC,
                caption=caption,
                quote=True,
                reply_markup=buttons,
                disable_notification=True
            )
        except Exception as e2:
            LOGGER.error(f"Failed to send fallback photo: {e2}")
            return await send_message(message, caption, buttons)


async def get_anime(vars_: dict):
    try:
        result = await return_json_senpai(ANIME_QUERY, vars_)
        
        if "errors" in result:
            error_msg = result["errors"][0].get("message", "Unknown error")
            return (f"[{error_msg}]",)
        
        data = result["data"]["Media"]
        
        idm = data.get("id")
        idmal = data.get("idMal", "N/A")
        romaji = data["title"]["romaji"]
        english = data["title"]["english"]
        native = data["title"]["native"]
        formats = data.get("format", "N/A")
        status = data.get("status", "N/A")
        episodes = data.get("episodes")
        duration = data.get("duration")
        country = data.get("countryOfOrigin", "JP")
        c_flag = cflag(country)
        source = data.get("source", "N/A")
        url = data.get("siteUrl")
        score = data.get("averageScore")
        
        if english:
            name = f"[{c_flag}]**{romaji}**\n__{english}__\n{native}"
        else:
            name = f"[{c_flag}]**{romaji}**\n{native}"
        
        avscd = f"\n➤ **Score:** `{score}%` 🌟" if score else ""
        dura = f"\n➤ **Duration:** `{duration} min/ep`" if duration else ""
        genres = ", ".join(data.get("genres", []))
        gnrs_ = f"\n➤ **Genres:** `{genres}`" if genres else ""
        tags = [tag["name"] for tag in data.get("tags", [])[:5]]
        tags_ = f"\n➤ **Tags:** `{', '.join(tags)}`" if tags else ""
  
        air_on = None
        if data.get("nextAiringEpisode"):
            next_air = data["nextAiringEpisode"]["timeUntilAiring"]
            eps = data["nextAiringEpisode"]["episode"]
            th = pos_no(str(eps))
            air_on = f"{make_it_rw(next_air * 1000)} | {eps}{th} eps"
        
        if air_on:
            status_air = f"➤ **Status:** `{status}`\n➤ **Next Airing:** `{air_on}`"
        else:
            eps_ = f"` | `{episodes} eps" if episodes else ""
            status_air = f"➤ **Status:** `{status}{eps_}`"
        
        trailer_link = "N/A"
        if data.get("trailer") and data["trailer"].get("site") == "youtube":
            trailer_link = f"<a href='https://youtu.be/{data['trailer']['id']}'>Trailer</a>"
        
        prql_id, sql_id = None, None
        prql, sql = "", ""
        
        for relation in data.get("relations", {}).get("edges", []):
            if relation["relationType"] == "PREQUEL" and relation["node"]["type"] == "ANIME":
                pname = relation["node"]["title"]["english"] or relation["node"]["title"]["romaji"]
                prql = f"**Prequel:** `{pname}`\n"
                prql_id = relation["node"]["id"]
                break
        
        for relation in data.get("relations", {}).get("edges", []):
            if relation["relationType"] == "SEQUEL" and relation["node"]["type"] == "ANIME":
                sname = relation["node"]["title"]["english"] or relation["node"]["title"]["romaji"]
                sql = f"**Sequel:** `{sname}`\n"
                sql_id = relation["node"]["id"]
                break
        
        additional = f"{prql}{sql}"
        title_img = f"https://img.anili.st/media/{idm}"
        
        finals_ = ANIME_TEMPLATE.format(**locals())
        
        data_dict = {
            'id': idm,
            'search': vars_.get('search', ''),
            'has_next': False,
            'prequel': prql_id,
            'sequel': sql_id
        }
        
        return (title_img, finals_, data_dict)
        
    except Exception as e:
        LOGGER.error(f"Error in get_anime: {e}")
        return (f"Error: {str(e)}",)


async def get_character(query: str, page: int):
    try:
        vars_ = {"search": query, "page": page}
        result = await return_json_senpai(CHARACTER_QUERY, vars_)
        
        if not result['data']['Page']['characters']:
            return ("No results found",)
        
        data = result["data"]["Page"]["characters"][0]
        
        id_ = data["id"]
        name = data["name"]["full"]
        native = data["name"]["native"]
        img = data["image"]["large"]
        site_url = data["siteUrl"]
        
        va = []
        for edge in data.get('media', {}).get('edges', []):
            for actor in edge.get('voiceActors', []):
                actor_link = f"[{actor['name']['full']}]({actor['siteUrl']})"
                if actor_link not in va:
                    va.append(actor_link)
        
        va_text = ""
        if va:
            lva = va.pop() if len(va) > 1 else None
            va_text = f"\n**Voice Actors:** {', '.join(va)}"
            if lva:
                va_text += f" and {lva}"
            va_text += "\n"
        
        cap_text = f"""__{native}__
(`{name}`)
**ID:** {id_}
{va_text}
<a href='{site_url}'>Visit Website</a>"""
        
        has_next = result["data"]["Page"]["pageInfo"]["hasNextPage"]
        
        data_dict = {
            'id': id_,
            'search': query,
            'has_next': has_next
        }
        
        return (img, cap_text, data_dict)
        
    except Exception as e:
        LOGGER.error(f"Error in get_character: {e}")
        return (f"Error: {str(e)}",)


async def get_manga(query: str, page: int):
    try:
        vars_ = {"search": query, "page": page}
        result = await return_json_senpai(MANGA_QUERY, vars_)
        
        if not result['data']['Page']['media']:
            return ("No results found",)
        
        data = result["data"]["Page"]["media"][0]
        
        idm = data.get("id")
        romaji = data["title"]["romaji"]
        english = data["title"]["english"]
        native = data["title"]["native"]
        status = data.get("status", "N/A")
        chapters = data.get("chapters", "N/A")
        volumes = data.get("volumes", "N/A")
        score = data.get("averageScore", "N/A")
        url = data.get("siteUrl")
        format_ = data.get("format", "N/A")
        country = data.get("countryOfOrigin", "JP")
        source = data.get("source", "N/A")
        c_flag = cflag(country)
        
        description = data.get("description", "N/A")
        if len(description) > 500:
            description = description[:500] + "..."
        
        if english:
            name = f"[{c_flag}]**{romaji}**\n__{english}__\n{native}"
        else:
            name = f"[{c_flag}]**{romaji}**\n{native}"
        
        finals_ = f"""{name}

**ID:** `{idm}`
➤ **Status:** `{status}`
➤ **Volumes:** `{volumes}`
➤ **Chapters:** `{chapters}`
➤ **Score:** `{score}`
➤ **Type:** `{format_}`
➤ **Source:** `{source}`

**Description:** `{description}`

<a href="{url}">Read More</a>"""
        
        pic = f"https://img.anili.st/media/{idm}"
        has_next = result["data"]["Page"]["pageInfo"]["hasNextPage"]
        
        data_dict = {
            'id': idm,
            'search': query,
            'has_next': has_next
        }
        
        return (pic, finals_, data_dict)
        
    except Exception as e:
        LOGGER.error(f"Error in get_manga: {e}")
        return (f"Error: {str(e)}",)


async def get_airing(query: str, page: int):
    try:
        vars_ = {"search": query, "page": page}
        result = await return_json_senpai(AIR_QUERY, vars_)
        
        if not result['data']['Page']['media']:
            return ("No results found",)
        
        data = result["data"]["Page"]["media"][0]
        
        mid = data.get("id")
        romaji = data["title"]["romaji"]
        english = data["title"]["english"]
        status = data.get("status", "N/A")
        country = data.get("countryOfOrigin", "JP")
        c_flag = cflag(country)
        cover_img = f"https://img.anili.st/media/{mid}"
        site = data.get("siteUrl")
        
        air_on = None
        if data.get("nextAiringEpisode"):
            next_air = data["nextAiringEpisode"]["timeUntilAiring"]
            episode = data["nextAiringEpisode"]["episode"]
            th = pos_no(episode)
            air_on = make_it_rw(next_air * 1000)
            air_text = f"Airing Episode `{episode}{th}` in `{air_on}`"
        else:
            air_text = "No upcoming episode"
        
        title_ = english or romaji
        out = f"[{c_flag}] **{title_}**\n\n**ID:** `{mid}`\n**Status:** `{status}`\n\n{air_text}\n\n<a href='{site}'>More Info</a>"
        
        has_next = result["data"]["Page"]["pageInfo"]["hasNextPage"]
        
        data_dict = {
            'id': mid,
            'search': query,
            'has_next': has_next
        }
        
        return (cover_img, out, data_dict)
        
    except Exception as e:
        LOGGER.error(f"Error in get_airing: {e}")
        return (f"Error: {str(e)}",)


async def get_top_animes(genre: str, page: int):
    try:
        vars_ = {"page": page}
        query = ALLTOP_QUERY
        msg = "Top animes:\n\n"
        
        if genre != "None":
            vars_["gnr"] = genre.lower()
            query = TOP_QUERY
            msg = f"Top animes for genre `{genre.capitalize()}`:\n\n"
        
        result = await return_json_senpai(query, vars_)
        
        if not result['data']['Page']['media']:
            return "No results found"
        
        data = result["data"]["Page"]
        
        for anime in data['media']:
            msg += f"⬛ `{anime['title']['romaji']}`\n"
        
        msg += f"\nTotal available: `{data['pageInfo']['total']}`"
        
        return msg
        
    except Exception as e:
        LOGGER.error(f"Error in get_top_animes: {e}")
        return f"Error: {str(e)}"


async def get_all_genres():
    try:
        result = await return_json_senpai(GET_GENRES, {})
        genres = result['data']['GenreCollection']
        msg = "**Genres List:**\n\n" + "\n".join(f"`{g}`" for g in genres)
        return msg
    except Exception as e:
        LOGGER.error(f"Error in get_all_genres: {e}")
        return f"Error: {str(e)}"


async def get_all_tags():
    try:
        result = await return_json_senpai(GET_TAGS, {})
        tags = [tag['name'] for tag in result['data']['MediaTagCollection'] if not tag['isAdult']]
        msg = f"**Tags List:**\n\n`{', '.join(tags)}`"
        return msg
    except Exception as e:
        LOGGER.error(f"Error in get_all_tags: {e}")
        return f"Error: {str(e)}"


async def get_additional_info(idm: int, req: str = "desc", page: int = 1):
    try:
        vars_ = {"id": idm}
        
        if req == "desc":
            result = await return_json_senpai(DES_INFO_QUERY, vars_)
            data = result["data"]["Media"]
            pic = f"https://img.anili.st/media/{idm}"
            description = data.get("description", "No description available")
            return (pic, description)
        
        elif req == "char":
            vars_['page'] = page
            result = await return_json_senpai(CHA_INFO_QUERY, vars_)
            data = result["data"]["Media"]
            pic = f"https://img.anili.st/media/{idm}"
            
            char_list = []
            for char in data["characters"]['edges']:
                char_list.append(f"• `{char['node']['name']['full']}` ({char['role']})")
            
            char_text = "\n".join(char_list) if char_list else "No characters found"
            return (pic, char_text, data["characters"]['pageInfo'])
        
        return (None, "No data available")
        
    except Exception as e:
        LOGGER.error(f"Error in get_additional_info: {e}")
        return (None, f"Error: {str(e)}")


async def browse_(query_type: str):
    try:
        season, year = season_(query_type == 'upcoming')
        sort = "POPULARITY_DESC"
        
        if query_type == 'trending':
            sort = "TRENDING_DESC"
        
        vars_ = {"s": season, "y": year, "sort": sort}
        result = await return_json_senpai(BROWSE_QUERY, vars_)
        
        data = result["data"]["Page"]["media"]
        anime_list = []
        
        for anime in data:
            if anime['format'] in ['TV', 'MOVIE', 'ONA']:
                anime_list.append(f"• `{anime['title']['romaji']}`")
        
        out = f"{query_type.capitalize()} animes in {season} {year}:\n\n"
        return out + "\n".join(anime_list[:20])
        
    except Exception as e:
        LOGGER.error(f"Error in browse_: {e}")
        return f"Error: {str(e)}"


def get_buttons(media: str, data: dict, page: int = None):
    buttons = ButtonMaker()
    anime_id = data.get('id')
    search_query = data.get('search', '')
    has_next = data.get('has_next', False)
    
    if media == "ANIME":
        buttons.data_button("Characters", f"animechar_{anime_id}_1_{anime_id}")
        buttons.data_button("Description", f"animedesc_{anime_id}_{anime_id}")
        
        if data.get('prequel'):
            buttons.data_button("Prequel", f"animebtn_{data['prequel']}")
        if data.get('sequel'):
            buttons.data_button("Sequel", f"animebtn_{data['sequel']}")
    
    elif media == "CHARACTER":
        buttons.data_button("Description", f"chardesc_{anime_id}_{search_query}_{page}")
    
    if page and search_query:
        if page > 1:
            buttons.data_button("Prev", f"animepage_{media}_{search_query}_{page-1}")
        if has_next:
            buttons.data_button("Next", f"animepage_{media}_{search_query}_{page+1}")
    
    return buttons.build_menu(2)


@new_task
async def anime_cmd(client, message):
    args = message.text.split(maxsplit=1)
    
    if len(args) == 1:
        msg = "Please provide an anime name or ID\nExample: `/anime Demon Slayer`"
        await send_message(message, msg)
        return
    
    query = args[1]
    vars_ = {"search": query}
    if query.isdigit():
        vars_ = {"id": int(query)}
    
    try:
        result = await get_anime(vars_)
        
        if len(result) == 1:
            await send_message(message, result[0])
            return
        
        title_img, text, data = result
        buttons = get_buttons("ANIME", data)
        
        await send_photo(message, title_img, text, buttons)
        
    except Exception as e:
        LOGGER.error(f"Error in anime_cmd: {e}")
        await send_message(message, f"Error: {str(e)}")


@new_task
async def manga_cmd(client, message):
    args = message.text.split(maxsplit=1)
    
    if len(args) == 1:
        msg = "Please provide a manga name\nExample: `/manga One Piece`"
        await send_message(message, msg)
        return
    
    query = args[1]
    
    try:
        result = await get_manga(query, 1)
        
        if len(result) == 1:
            await send_message(message, result[0])
            return
        
        pic, text, data = result
        buttons = get_buttons("MANGA", data, page=1)
        
        await send_photo(message, pic, text, buttons)
        
    except Exception as e:
        LOGGER.error(f"Error in manga_cmd: {e}")
        await send_message(message, f"Error: {str(e)}")


@new_task
async def character_cmd(client, message):
    args = message.text.split(maxsplit=1)
    
    if len(args) == 1:
        msg = "Please provide a character name\nExample: `/character Nezuko`"
        await send_message(message, msg)
        return
    
    query = args[1]
    
    try:
        result = await get_character(query, 1)
        
        if len(result) == 1:
            await send_message(message, result[0])
            return
        
        img, text, data = result
        buttons = get_buttons("CHARACTER", data, page=1)
        
        await send_photo(message, img, text, buttons)
        
    except Exception as e:
        LOGGER.error(f"Error in character_cmd: {e}")
        await send_message(message, f"Error: {str(e)}")


@new_task
async def airing_cmd(client, message):
    args = message.text.split(maxsplit=1)
    
    if len(args) == 1:
        msg = "Please provide an anime name\nExample: `/airing Demon Slayer`"
        await send_message(message, msg)
        return
    
    query = args[1]
    
    try:
        result = await get_airing(query, 1)
        
        if len(result) == 1:
            await send_message(message, result[0])
            return
        
        cover_img, out, data = result
        buttons = get_buttons("AIRING", data, page=1)
        
        await send_photo(message, cover_img, out, buttons)
        
    except Exception as e:
        LOGGER.error(f"Error in airing_cmd: {e}")
        await send_message(message, f"Error: {str(e)}")


@new_task
async def top_cmd(client, message):
    args = message.text.split(maxsplit=1)
    genre = args[1] if len(args) > 1 else "None"
    
    try:
        result = await get_top_animes(genre, 1)
        await send_message(message, result)
        
    except Exception as e:
        LOGGER.error(f"Error in top_cmd: {e}")
        await send_message(message, f"Error: {str(e)}")


@new_task
async def genres_cmd(client, message):
    try:
        result = await get_all_genres()
        await send_message(message, result)
        
    except Exception as e:
        LOGGER.error(f"Error in genres_cmd: {e}")
        await send_message(message, f"Error: {str(e)}")


@new_task
async def tags_cmd(client, message):
    try:
        result = await get_all_tags()
        await send_message(message, result)
        
    except Exception as e:
        LOGGER.error(f"Error in tags_cmd: {e}")
        await send_message(message, f"Error: {str(e)}")


@new_task
async def browse_cmd(client, message):
    try:
        buttons = ButtonMaker()
        buttons.data_button("Trending", "animebrowse_trending")
        buttons.data_button("Popular", "animebrowse_popular")
        buttons.data_button("Upcoming", "animebrowse_upcoming")
        
        msg = await browse_('trending')
        await send_message(message, msg, buttons.build_menu(3))
        
    except Exception as e:
        LOGGER.error(f"Error in browse_cmd: {e}")
        await send_message(message, f"Error: {str(e)}")


@new_task
async def page_callback(client, query):
    data = query.data.split("_")
    media = data[1]
    search_query = data[2]
    page = int(data[3])
    
    try:
        if media == "ANIME":
            result = await get_anime({"search": search_query})
            
        elif media == "MANGA":
            result = await get_manga(search_query, page)
            
        elif media == "CHARACTER":
            result = await get_character(search_query, page)
            
        elif media == "AIRING":
            result = await get_airing(search_query, page)
        
        else:
            await query.answer("Invalid media type!", show_alert=True)
            return
        
        if len(result) == 1:
            await query.answer("No more results!", show_alert=True)
            return
        
        pic = result[0]
        text = result[1]
        buttons = get_buttons(media, result[2], page=page)
        
        try:
            await query.message.edit_media(
                media=InputMediaPhoto(media=pic, caption=text),
                reply_markup=buttons
            )
        except Exception as e:
            LOGGER.warning(f"Failed to edit media: {e}")
            await query.message.delete()
            await send_photo(query.message, pic, text, buttons)
        
        await query.answer()
        
    except Exception as e:
        LOGGER.error(f"Error in page_callback: {e}")
        await query.answer(f"Error: {str(e)}", show_alert=True)


@new_task
async def btn_callback(client, query):
    data = query.data.split("_")
    anime_id = int(data[1])
    
    try:
        result = await get_anime({"id": anime_id})
        
        if len(result) == 1:
            await query.answer(result[0], show_alert=True)
            return
        
        pic, text, data_list = result
        buttons = get_buttons("ANIME", data_list)
        
        try:
            await query.message.edit_media(
                media=InputMediaPhoto(media=pic, caption=text),
                reply_markup=buttons
            )
        except Exception as e:
            LOGGER.warning(f"Failed to edit media: {e}")
            await query.message.delete()
            await send_photo(query.message, pic, text, buttons)
        
        await query.answer()
        
    except Exception as e:
        LOGGER.error(f"Error in btn_callback: {e}")
        await query.answer(f"Error: {str(e)}", show_alert=True)


@new_task
async def desc_callback(client, query):
    data = query.data.split("_")
    idm = int(data[1])
    back_data = data[2]
    
    try:
        pic, description = await get_additional_info(idm, "desc")
        
        if not description:
            await query.answer("No description available!", show_alert=True)
            return
        
        buttons = ButtonMaker()
        buttons.data_button("Back", f"animebtn_{back_data}")
        
        msg = f"**Description:**\n\n{description[:4000]}"
        
        try:
            await query.message.edit_media(
                media=InputMediaPhoto(media=pic, caption=msg),
                reply_markup=buttons.build_menu(1)
            )
        except Exception as e:
            LOGGER.warning(f"Failed to edit media: {e}")
            await query.message.delete()
            await send_photo(query.message, pic, msg, buttons.build_menu(1))
        
        await query.answer()
        
    except Exception as e:
        LOGGER.error(f"Error in desc_callback: {e}")
        await query.answer(f"Error: {str(e)}", show_alert=True)


@new_task
async def char_callback(client, query):
    data = query.data.split("_")
    idm = int(data[1])
    page = int(data[2])
    back_data = data[3]
    
    try:
        pic, char_text, page_info = await get_additional_info(idm, "char", page)
        
        if not char_text:
            await query.answer("No characters found!", show_alert=True)
            return
        
        buttons = ButtonMaker()
        
        if page > 1:
            buttons.data_button("Prev", f"animechar_{idm}_{page-1}_{back_data}")
        if page_info['hasNextPage']:
            buttons.data_button("Next", f"animechar_{idm}_{page+1}_{back_data}")
        
        buttons.data_button("Back", f"animebtn_{back_data}")
        
        msg = f"**Characters** (Page {page}):\n\n{char_text}\n\nTotal: {page_info['total']}"
        
        try:
            await query.message.edit_media(
                media=InputMediaPhoto(media=pic, caption=msg[:4000]),
                reply_markup=buttons.build_menu(2)
            )
        except Exception as e:
            LOGGER.warning(f"Failed to edit media: {e}")
            await query.message.delete()
            await send_photo(query.message, pic, msg[:4000], buttons.build_menu(2))
        
        await query.answer()
        
    except Exception as e:
        LOGGER.error(f"Error in char_callback: {e}")
        await query.answer(f"Error: {str(e)}", show_alert=True)


@new_task
async def browse_callback(client, query):
    data = query.data.split("_")
    browse_type = data[1]
    
    try:
        msg = await browse_(browse_type)
        
        buttons = ButtonMaker()
        buttons.data_button("Trending", "animebrowse_trending")
        buttons.data_button("Popular", "animebrowse_popular")
        buttons.data_button("Upcoming", "animebrowse_upcoming")
        
        await edit_message(query.message, msg, buttons.build_menu(3))
        await query.answer()
        
    except Exception as e:
        LOGGER.error(f"Error in browse_callback: {e}")
        await query.answer(f"Error: {str(e)}", show_alert=True)


bot.add_handler(
    MessageHandler(
        anime_cmd,
        filters=command('anime') & CustomFilters.authorized
    )
)

bot.add_handler(
    MessageHandler(
        manga_cmd,
        filters=command('manga') & CustomFilters.authorized
    )
)

bot.add_handler(
    MessageHandler(
        character_cmd,
        filters=command('character') & CustomFilters.authorized
    )
)

bot.add_handler(
    MessageHandler(
        airing_cmd,
        filters=command('airing') & CustomFilters.authorized
    )
)

bot.add_handler(
    MessageHandler(
        top_cmd,
        filters=command('top') & CustomFilters.authorized
    )
)

bot.add_handler(
    MessageHandler(
        genres_cmd,
        filters=command('genres') & CustomFilters.authorized
    )
)

bot.add_handler(
    MessageHandler(
        tags_cmd,
        filters=command('tags') & CustomFilters.authorized
    )
)

bot.add_handler(
    MessageHandler(
        browse_cmd,
        filters=command('browse') & CustomFilters.authorized
    )
)

bot.add_handler(CallbackQueryHandler(page_callback, filters=filters.regex(r"^animepage_")))
bot.add_handler(CallbackQueryHandler(btn_callback, filters=filters.regex(r"^animebtn_")))
bot.add_handler(CallbackQueryHandler(desc_callback, filters=filters.regex(r"^animedesc_")))
bot.add_handler(CallbackQueryHandler(char_callback, filters=filters.regex(r"^animechar_")))
bot.add_handler(CallbackQueryHandler(browse_callback, filters=filters.regex(r"^animebrowse_")))