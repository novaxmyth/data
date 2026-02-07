import asyncio
import json
import os
import tempfile
from dataclasses import dataclass
from typing import Iterable

from aiohttp import ClientSession, FormData
from pyrogram.handlers import MessageHandler
from pyrogram.filters import command

from bot import LOGGER, bot
from bot.helper.ext_utils.bot_utils import new_task
from bot.helper.telegram_helper.filters import CustomFilters
from bot.helper.telegram_helper.message_utils import send_message, edit_message

NHENTAI_API_BASE = "https://nhentai.net/api"
NHENTAI_GALLERY_URL = f"{NHENTAI_API_BASE}/gallery/{{code}}"
NHENTAI_SEARCH_URL = f"{NHENTAI_API_BASE}/galleries/search"
TELEGRAPH_API_BASE = "https://api.telegra.ph"
TELEGRAPH_UPLOAD_URL = "https://telegra.ph/upload"
MAX_IMAGES_PER_PAGE = 50
REQUEST_TIMEOUT = 30
NHENTAI_IMAGE_HEADERS = {
    "Referer": "https://nhentai.net/",
    "User-Agent": "Mozilla/5.0 (compatible; NHentaiTelegraphBot/1.0)",
}
TELEGRAPH_SHORT_NAME = "NHentai"
TELEGRAPH_AUTHOR_NAME = "NHentai"

_telegraph_token: str | None = None
_telegraph_lock = asyncio.Lock()


@dataclass(slots=True)
class NhentaiGallery:
    code: int
    title: str
    media_id: str
    page_types: list[str]


def _pick_title(data: dict) -> str:
    title = data.get("title") or {}
    return title.get("english") or title.get("pretty") or title.get("japanese") or "NHentai"


def _ext_from_type(image_type: str) -> str:
    return {"j": "jpg", "p": "png", "g": "gif"}.get(image_type, "jpg")


def _build_image_urls(media_id: str, page_types: Iterable[str]) -> list[str]:
    urls = []
    for idx, img_type in enumerate(page_types, start=1):
        ext = _ext_from_type(img_type)
        urls.append(f"https://i.nhentai.net/galleries/{media_id}/{idx}.{ext}")
    return urls


async def _fetch_json(session: ClientSession, url: str, params: dict | None = None) -> dict:
    async with session.get(url, params=params, timeout=REQUEST_TIMEOUT) as response:
        response.raise_for_status()
        return await response.json()


async def _fetch_gallery(session: ClientSession, code: int) -> NhentaiGallery:
    data = await _fetch_json(session, NHENTAI_GALLERY_URL.format(code=code))
    images = data.get("images", {})
    pages = images.get("pages", [])
    page_types = [page.get("t", "j") for page in pages]
    return NhentaiGallery(
        code=int(data.get("id", code)),
        title=_pick_title(data),
        media_id=str(data.get("media_id")),
        page_types=page_types,
    )


async def _search_gallery(session: ClientSession, query: str) -> NhentaiGallery | None:
    payload = await _fetch_json(session, NHENTAI_SEARCH_URL, params={"query": query, "page": 1})
    results = payload.get("result") or []
    if not results:
        return None
    best = results[0]
    images = best.get("images", {})
    page_types = [page.get("t", "j") for page in images.get("pages", [])]
    return NhentaiGallery(
        code=int(best.get("id")),
        title=_pick_title(best),
        media_id=str(best.get("media_id")),
        page_types=page_types,
    )


def _guess_content_type(image_url: str) -> tuple[str, str]:
    ext = image_url.rsplit(".", 1)[-1].lower()
    if ext in {"jpg", "jpeg"}:
        return "image/jpeg", "jpg"
    if ext == "png":
        return "image/png", "png"
    if ext == "gif":
        return "image/gif", "gif"
    return "application/octet-stream", "bin"


async def _telegraph_request(
    session: ClientSession,
    endpoint: str,
    *,
    data: dict | None = None,
) -> dict:
    url = f"{TELEGRAPH_API_BASE}/{endpoint}"
    async with session.post(url, data=data, timeout=REQUEST_TIMEOUT) as response:
        response.raise_for_status()
        payload = await response.json(content_type=None)
    if not payload.get("ok"):
        LOGGER.error("Telegraph API error on %s: %s", endpoint, payload)
        raise RuntimeError(f"Telegraph API error: {payload}")
    return payload.get("result") or {}


async def _ensure_telegraph_token(session: ClientSession) -> str:
    global _telegraph_token
    if _telegraph_token:
        return _telegraph_token
    async with _telegraph_lock:
        if _telegraph_token:
            return _telegraph_token
        result = await _telegraph_request(
            session,
            "createAccount",
            data={
                "short_name": TELEGRAPH_SHORT_NAME,
                "author_name": TELEGRAPH_AUTHOR_NAME,
            },
        )
        token = result.get("access_token")
        if not token:
            raise RuntimeError("Telegraph did not return an access token.")
        _telegraph_token = token
        return token


async def _upload_to_telegraph(session: ClientSession, image_url: str) -> str | None:
    tmp_path = None
    try:
        async with session.get(
            image_url,
            timeout=REQUEST_TIMEOUT,
            headers=NHENTAI_IMAGE_HEADERS,
        ) as response:
            response.raise_for_status()
            data = await response.read()
            content_type = response.headers.get("Content-Type")
    except Exception as err:
        LOGGER.error(f"NHentai image download failed: {err}")
        return None

    fallback_type, extension = _guess_content_type(image_url)
    normalized_type = (content_type or "").split(";", 1)[0].strip().lower()
    if normalized_type not in {"image/jpeg", "image/png", "image/gif"}:
        normalized_type = fallback_type
    upload_type = normalized_type

    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=f".{extension}") as temp_file:
            temp_file.write(data)
            tmp_path = temp_file.name

        form = FormData()
        with open(tmp_path, "rb") as temp_file:
            form.add_field(
                "file",
                temp_file,
                filename=f"image.{extension}",
                content_type=upload_type,
            )
            async with session.post(
                TELEGRAPH_UPLOAD_URL, data=form, timeout=REQUEST_TIMEOUT
            ) as response:
                if response.status >= 400:
                    error_body = await response.text()
                    LOGGER.error(
                        f"Telegraph upload failed: status={response.status} body={error_body}"
                    )
                    return None
                payload = await response.json(content_type=None)

        if not payload or not isinstance(payload, list):
            LOGGER.error("Telegraph upload returned unexpected payload: %s", payload)
            return None
        src = payload[0].get("src")
        if not src:
            LOGGER.error("Telegraph upload missing src in payload: %s", payload)
            return None
        return f"https://telegra.ph{src}"
    except Exception as err:
        LOGGER.error(f"Telegraph upload failed: {err}")
        return None
    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except OSError:
                LOGGER.warning("Failed to remove temp file: %s", tmp_path)


async def _create_telegraph_page(session: ClientSession, title: str, image_urls: list[str]) -> str | None:
    content = [{"tag": "img", "attrs": {"src": url}} for url in image_urls]
    try:
        token = await _ensure_telegraph_token(session)
        result = await _telegraph_request(
            session,
            "createPage",
            data={
                "access_token": token,
                "title": title,
                "author_name": TELEGRAPH_AUTHOR_NAME,
                "content": json.dumps(content, separators=(",", ":")),
            },
        )
        return result.get("url")
    except Exception as err:
        LOGGER.error(f"Telegraph createPage failed: {err}")
        return None


async def _chunked(iterable: list[str], size: int) -> list[list[str]]:
    return [iterable[i : i + size] for i in range(0, len(iterable), size)]


async def _build_telegraph_pages(gallery: NhentaiGallery) -> list[str]:
    image_urls = _build_image_urls(gallery.media_id, gallery.page_types)
    if not image_urls:
        return []

    async with ClientSession() as session:
        uploaded_images = []
        for image_url in image_urls:
            telegraph_image = await _upload_to_telegraph(session, image_url)
            if telegraph_image:
                uploaded_images.append(telegraph_image)
            await asyncio.sleep(0.2)

        if not uploaded_images:
            return []

        pages = []
        for index, chunk in enumerate(await _chunked(uploaded_images, MAX_IMAGES_PER_PAGE), start=1):
            page_title = gallery.title if len(uploaded_images) <= MAX_IMAGES_PER_PAGE else f"{gallery.title} (Part {index})"
            page_url = await _create_telegraph_page(session, page_title, chunk)
            if page_url:
                pages.append(page_url)
            await asyncio.sleep(0.2)

        return pages


@new_task
async def nhentai_handler(_, message):
    cmd = message.text.split(maxsplit=1)
    if len(cmd) == 1:
        await send_message(message, "usage: <code>/nhentai &lt;code or title&gt;</code>")
        return

    query = cmd[1].strip()
    status = await send_message(message, "🔎 Searching NHentai...")

    async with ClientSession() as session:
        try:
            if query.isdigit():
                gallery = await _fetch_gallery(session, int(query))
            else:
                gallery = await _search_gallery(session, query)
        except Exception as err:
            LOGGER.error(f"NHentai fetch failed: {err}")
            await edit_message(status, "❌ Failed to fetch NHentai data.")
            return

    if not gallery:
        await edit_message(status, "❌ No results found.")
        return

    await edit_message(status, f"📦 Uploading <b>{gallery.title}</b> to Telegraph...")
    pages = await _build_telegraph_pages(gallery)
    if not pages:
        await edit_message(status, "❌ Failed to upload images to Telegraph.")
        return

    if len(pages) == 1:
        message_text = f"✅ <b>{gallery.title}</b>\n{pages[0]}"
    else:
        links = "\n".join(f"Part {idx}: {url}" for idx, url in enumerate(pages, start=1))
        message_text = f"✅ <b>{gallery.title}</b>\n{links}"

    await edit_message(status, message_text)


bot.add_handler(
    MessageHandler(
        nhentai_handler,
        filters=command("nhentai") & CustomFilters.authorized,
    )
)
