import asyncio
from dataclasses import dataclass
from os import remove
from pathlib import Path
from secrets import token_hex
from shutil import make_archive, rmtree
from urllib.parse import urlparse

from aiohttp import ClientSession, ClientTimeout
from pyrogram import filters
from pyrogram.handlers import CallbackQueryHandler, MessageHandler
from pyrogram.types import InputMediaPhoto

from bot import LOGGER, bot, config_dict
from bot.helper.ext_utils.bot_utils import cmd_exec, new_task
from bot.helper.telegram_helper.button_build import ButtonMaker
from bot.helper.telegram_helper.filters import CustomFilters
from bot.helper.telegram_helper.message_utils import (
    delete_message,
    edit_message,
    send_file,
    send_message,
    send_photo,
)

NSFW_API_URL = "https://api.waifu.pics/nsfw/waifu"
NSFW_CAPTION = "🔞 NSFW Anime"


@dataclass(slots=True)
class GitHubRequestPaths:
    temp_dir: Path
    zip_path: Path


def _sanitize_repo_name(url: str) -> str:
    path = urlparse(url).path.strip("/")
    if not path:
        return "repository"
    candidate = path.split("/")[-1].replace(".git", "")
    cleaned = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in candidate)
    cleaned = cleaned.strip("._-")
    return cleaned or "repository"


def _is_valid_github_url(url: str) -> bool:
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    if hostname not in {"github.com", "www.github.com"}:
        return False

    # Require an owner/repo-style path for GitHub cloning URLs.
    path_parts = [part for part in parsed.path.strip("/").split("/") if part]
    return len(path_parts) >= 2


def _build_request_paths(user_id: int, repo_name: str) -> GitHubRequestPaths:
    base_dir = Path(config_dict.get("DOWNLOAD_DIR") or "downloads/").resolve()
    request_id = token_hex(4)
    return GitHubRequestPaths(
        temp_dir=base_dir / f"{user_id}_{repo_name}_{request_id}",
        zip_path=base_dir / f"{repo_name}_{request_id}.zip",
    )


def _build_nsfw_buttons(owner_id: int):
    buttons = ButtonMaker()
    buttons.data_button("🔄 Refresh", f"nsfw:refresh:{owner_id}")
    buttons.data_button("✖️ Close", f"nsfw:close:{owner_id}")
    return buttons.build_menu(2)


async def _fetch_nsfw_image_url() -> str | None:
    try:
        timeout = ClientTimeout(total=15)
        async with ClientSession(timeout=timeout) as session:
            async with session.get(NSFW_API_URL, ssl=False) as response:
                if response.status != 200:
                    return None
                payload = await response.json(content_type=None)
                return payload.get("url")
    except Exception as err:
        LOGGER.error(f"NSFW fetch error: {err}")
        return None


def _query_owner_allowed(query, owner_id: int) -> bool:
    user = query.from_user
    return bool(user and user.id == owner_id)


async def _safe_delete_related_nsfw_command(query):
    reply = query.message.reply_to_message
    if not reply or not reply.text:
        return
    if reply.text.lstrip().startswith("/nsfw"):
        await delete_message(reply)


@new_task
async def github_clone_handler(_, message):
    cmd = message.text.split(maxsplit=1)
    if len(cmd) == 1:
        await send_message(message, "usage: <code>/github &lt;url&gt;</code>")
        return

    progress = await send_message(message, "⏳ Processing GitHub repository...")
    repo_url = cmd[1].strip()
    if not repo_url.startswith(("http://", "https://")):
        repo_url = f"https://{repo_url}"

    if not _is_valid_github_url(repo_url):
        await edit_message(progress, "❌ Invalid GitHub URL!")
        return

    repo_name = _sanitize_repo_name(repo_url)
    paths = _build_request_paths(message.from_user.id, repo_name)

    try:
        paths.temp_dir.parent.mkdir(parents=True, exist_ok=True)

        await edit_message(progress, f"📥 Cloning repository: <code>{repo_name}</code>...")
        stdout, stderr, return_code = await cmd_exec(
            ["git", "clone", "--depth", "1", repo_url, str(paths.temp_dir)]
        )

        if return_code != 0:
            error_msg = stderr or stdout or "Unknown error"
            if "Repository not found" in error_msg:
                await edit_message(progress, "❌ Repository not found or access denied.")
            elif "Authentication failed" in error_msg or "could not read Username" in error_msg:
                await edit_message(progress, "❌ Authentication failed for this repository.")
            else:
                await edit_message(progress, f"❌ Clone failed:\n<code>{error_msg[:500]}</code>")
            return

        git_dir = paths.temp_dir / ".git"
        if git_dir.exists():
            rmtree(git_dir, ignore_errors=True)

        await edit_message(progress, f"📦 Creating archive: <code>{paths.zip_path.name}</code>...")
        await asyncio.to_thread(
            make_archive,
            str(paths.zip_path.with_suffix("")),
            "zip",
            str(paths.temp_dir),
        )

        if not paths.zip_path.exists():
            await edit_message(progress, "❌ Failed to create zip archive!")
            return

        size_mb = paths.zip_path.stat().st_size / (1024 * 1024)
        await edit_message(progress, f"📤 Uploading <code>{paths.zip_path.name}</code> ({size_mb:.2f} MB)...")
        await send_file(
            message,
            paths.zip_path,
            caption=f"📦 <b>{repo_name}</b>\n💾 Size: {size_mb:.2f} MB",
        )
        await delete_message(progress)

    except Exception as err:
        LOGGER.error(f"GitHub clone error: {err}")
        await edit_message(progress, f"❌ Error: <code>{str(err)[:500]}</code>")
    finally:
        if paths.temp_dir.exists():
            rmtree(paths.temp_dir, ignore_errors=True)
        if paths.zip_path.exists():
            remove(paths.zip_path)


@new_task
async def nsfw_handler(_, message):
    image_url = await _fetch_nsfw_image_url()
    if not image_url:
        await send_message(message, "❌ Could not fetch NSFW image right now. Try again later.")
        return

    await send_photo(
        message,
        image_url,
        caption=NSFW_CAPTION,
        buttons=_build_nsfw_buttons(message.from_user.id),
    )


@new_task
async def nsfw_callback_handler(_, query):
    parts = query.data.split(":")
    if len(parts) != 3 or parts[0] != "nsfw":
        await query.answer("Invalid action", show_alert=True)
        return

    action = parts[1]
    try:
        owner_id = int(parts[2])
    except ValueError:
        await query.answer("Invalid owner", show_alert=True)
        return

    if not _query_owner_allowed(query, owner_id):
        await query.answer("This action is not for you.", show_alert=True)
        return

    if action == "close":
        await query.answer()
        await delete_message(query.message)
        await _safe_delete_related_nsfw_command(query)
        return

    if action != "refresh":
        await query.answer("Unknown action", show_alert=True)
        return

    await query.answer("Refreshing...")
    image_url = await _fetch_nsfw_image_url()
    if not image_url:
        await query.answer("Failed to fetch new image", show_alert=True)
        return

    try:
        await query.message.edit_media(
            media=InputMediaPhoto(media=image_url, caption=NSFW_CAPTION),
            reply_markup=_build_nsfw_buttons(owner_id),
        )
    except Exception as err:
        LOGGER.error(f"NSFW refresh error: {err}")
        await query.answer("Failed to refresh image", show_alert=True)


bot.add_handler(
    MessageHandler(
        github_clone_handler,
        filters=filters.command("github") & CustomFilters.authorized,
    )
)

bot.add_handler(
    MessageHandler(
        nsfw_handler,
        filters=filters.command("nsfw") & CustomFilters.authorized,
    )
)

bot.add_handler(
    CallbackQueryHandler(
        nsfw_callback_handler,
        filters=filters.regex(r"^nsfw:(refresh|close):"),
    )
)
