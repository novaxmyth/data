import asyncio
from json import loads
from math import floor
from time import time
from os.path import splitext
from configparser import ConfigParser
from aiofiles import open as aiopen
from aiofiles.os import path as aiopath, remove
from asyncio.subprocess import PIPE, create_subprocess_exec as exec
from asyncio import sleep, TimeoutError
from functools import lru_cache
from contextlib import asynccontextmanager
from collections import OrderedDict

from pyrogram import filters
from pyrogram.filters import command, regex
from pyrogram.handlers import MessageHandler, CallbackQueryHandler

from bot import LOGGER, bot
from bot.helper.ext_utils.bot_utils import sync_to_async, cmd_exec
from bot.helper.telegram_helper.filters import CustomFilters
from bot.helper.telegram_helper.button_build import ButtonMaker
from bot.helper.telegram_helper.message_utils import send_message, edit_message, delete_message, send_file


# Configuration
rclone_config = "/usr/src/app/rclone.conf"
SIZE_UNITS = ["B", "KB", "MB", "GB", "TB", "PB"]
RCLONE_TIMEOUT = 300
MEDIAINFO_TIMEOUT = 60
SEARCH_TIMEOUT = 180
MAX_STREAM_CHUNK = 8192
ITEMS_PER_PAGE = 10
CACHE_TTL = 60
CACHE_MAX_SIZE = 100

rclone_dict = {}
active_tasks = {}  # Track background tasks


def get_message_owner_id(message, fallback_user_id=None):
    """Get owner user id from callback/menu message with fallback."""
    if getattr(message, "reply_to_message", None) and message.reply_to_message.from_user:
        return message.reply_to_message.from_user.id
    if getattr(message, "from_user", None):
        return message.from_user.id
    return fallback_user_id


def get_user_tag(message):
    """Build a safe user tag for status messages."""
    if getattr(message, "reply_to_message", None) and message.reply_to_message.from_user:
        source_user = message.reply_to_message.from_user
    else:
        source_user = getattr(message, "from_user", None)

    if not source_user:
        return "user"
    if source_user.username:
        return f"@{source_user.username}"
    return source_user.mention


class LRUCache:
    """Simple LRU cache with size limit"""
    def __init__(self, max_size=CACHE_MAX_SIZE):
        self.cache = OrderedDict()
        self.max_size = max_size
    
    def get(self, key):
        if key in self.cache:
            timestamp, value = self.cache[key]
            if time() - timestamp < CACHE_TTL:
                self.cache.move_to_end(key)
                return value
            else:
                del self.cache[key]
        return None
    
    def set(self, key, value):
        self.cache[key] = (time(), value)
        self.cache.move_to_end(key)
        if len(self.cache) > self.max_size:
            self.cache.popitem(last=False)
    
    def clear(self):
        self.cache.clear()


process_cache = LRUCache()


class Menus:
    MYFILES = "myfilesmenu"
    STORAGE = "storagemenu"
    CLEANUP = "cleanupmenu"


def get_readable_file_size(size_in_bytes: int):
    """Convert bytes to human readable format"""
    if size_in_bytes is None:
        return "0B"
    index = 0
    while size_in_bytes >= 1024 and index < len(SIZE_UNITS) - 1:
        size_in_bytes /= 1024
        index += 1
    return (
        f"{size_in_bytes:.2f}{SIZE_UNITS[index]}"
        if index > 0
        else f"{size_in_bytes:.2f}B"
    )


def rcloneListNextPage(info, offset=0, max_results=ITEMS_PER_PAGE):
    """Paginate rclone list results"""
    start = offset
    end = max_results + start
    total = len(info)
    next_offset = offset + max_results

    if end > total:
        next_page = info[start:]
    elif start >= total:
        next_page = []
    else:
        next_page = info[start:end]

    return next_page, next_offset


def rcloneListButtonMaker(info, button, menu_type, dir_callback, file_callback, user_id):
    """Create buttons for rclone list items"""
    for index, dir in enumerate(info):
        path = dir["Path"]
        update_rclone_data(str(index), path, user_id)

        if dir["MimeType"] == "inode/directory":
            button.data_button(
                f"📁 {path}", data=f"{menu_type}^{dir_callback}^{index}^{user_id}"
            )
        else:
            size = get_readable_file_size(dir["Size"])
            button.data_button(
                f"[{size}] {path}",
                data=f"{menu_type}^{file_callback}^{index}^True^{user_id}",
            )


def get_rclone_data(key, user_id):
    """Get user-specific rclone data"""
    value_dict = rclone_dict.get(user_id, {})
    return value_dict.get(key, "")


def update_rclone_data(key, value, user_id):
    """Update user-specific rclone data"""
    if user_id in rclone_dict:
        rclone_dict[user_id][key] = value
    else:
        rclone_dict[user_id] = {key: value}


def clear_rclone_data(user_id):
    """Clear user-specific rclone data"""
    if user_id in rclone_dict:
        del rclone_dict[user_id]


async def create_next_buttons(
    next_offset,
    prev_offset,
    _next_offset,
    data_back_cb,
    total,
    user_id,
    buttons,
    filter,
    menu_type,
    is_second_menu=False,
):
    """Create pagination buttons"""
    current_page = round(int(next_offset) / ITEMS_PER_PAGE) + 1
    total_pages = max(round(total / ITEMS_PER_PAGE), 1)

    if next_offset == 0:
        buttons.data_button(
            f"🔢 {current_page} / {total_pages}",
            f"{menu_type}^pages",
            "footer",
        )
        if total > ITEMS_PER_PAGE:
            buttons.data_button(
                "NEXT ⏩",
                f"{filter} {_next_offset} {is_second_menu} {data_back_cb}",
                "footer",
            )
    elif next_offset >= total:
        buttons.data_button(
            "⏪ BACK",
            f"{filter} {prev_offset} {is_second_menu} {data_back_cb}",
            "footer",
        )
        buttons.data_button(
            f"🔢 {current_page} / {total_pages}",
            f"{menu_type}^pages",
            "footer",
        )
    else:
        buttons.data_button(
            "⏪ BACK",
            f"{filter} {prev_offset} {is_second_menu} {data_back_cb}",
            "footer",
        )
        buttons.data_button(
            f"🔢 {current_page} / {total_pages}",
            f"{menu_type}^pages",
            "footer",
        )
        buttons.data_button(
            "NEXT ⏩",
            f"{filter} {_next_offset} {is_second_menu} {data_back_cb}",
            "footer",
        )

    buttons.data_button(
        "⬅️ Back", f"{menu_type}^{data_back_cb}^{user_id}", "footer"
    )
    buttons.data_button(
        "✘ Close", f"{menu_type}^close^{user_id}", "footer"
    )


@asynccontextmanager
async def rclone_process(*args):
    """Context manager for rclone processes with proper cleanup"""
    process = None
    try:
        process = await exec(*args, stdout=PIPE, stderr=PIPE)
        yield process
    except Exception as e:
        LOGGER.error(f"Process error: {e}")
        raise
    finally:
        if process and process.returncode is None:
            try:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    LOGGER.warning("Process didn't terminate, killing...")
                    process.kill()
                    await process.wait()
            except Exception as e:
                LOGGER.error(f"Process cleanup error: {e}")


async def execute_rclone_cmd(cmd, timeout=RCLONE_TIMEOUT, cache_key=None):
    """Execute rclone command with error handling and optional caching"""
    # Check cache first
    if cache_key:
        cached = process_cache.get(cache_key)
        if cached is not None:
            LOGGER.info(f"Cache hit for: {cache_key}")
            return cached

    try:
        async with rclone_process(*cmd) as process:
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(), timeout=timeout
                )
            except asyncio.TimeoutError:
                LOGGER.error(f"Command timeout after {timeout}s: {' '.join(cmd)}")
                return None, f"Operation timed out after {timeout} seconds", -1

            return_code = process.returncode
            stdout = stdout.decode().strip()
            stderr = stderr.decode().strip()

            result = (stdout, stderr, return_code)

            # Cache successful results
            if cache_key and return_code == 0:
                process_cache.set(cache_key, result)

            # Log detailed errors for debugging
            if return_code != 0:
                LOGGER.error(f"Command failed (rc={return_code}): {' '.join(cmd)}")
                LOGGER.error(f"Stderr: {stderr}")

            return result

    except Exception as e:
        LOGGER.error(f"Command execution error: {e}", exc_info=True)
        return None, str(e), -1


async def is_valid_path(remote, path, message):
    """Validate if rclone path exists"""
    try:
        cmd = [
            "rclone",
            "lsjson",
            f"--config={rclone_config}",
            f"{remote}:{path}",
            "--fast-list",
            "--max-depth=1",
        ]
        stdout, stderr, return_code = await execute_rclone_cmd(cmd, timeout=30)

        if return_code != 0:
            LOGGER.error(f"Invalid path: {remote}:{path} - {stderr}")
            return False
        return True
    except Exception as e:
        LOGGER.error(f"Path validation error: {e}")
        return False


async def list_remotes(
    message, menu_type, remote_type="remote", is_second_menu=False, edit=False
):
    """List available rclone remotes"""
    try:
        if message.reply_to_message:
            user_id = get_message_owner_id(message)
        else:
            user_id = message.from_user.id

        conf = ConfigParser()
        conf.read(rclone_config)
        buttons = ButtonMaker()

        for remote in conf.sections():
            crypt_icon = ""
            is_crypt = False
            if conf.get(remote, "type") == "crypt":
                is_crypt = True
                crypt_icon = "🔐"
            buttons.data_button(
                f"{crypt_icon} {remote}",
                f"{menu_type}^{remote_type}^{remote}^{is_crypt}^{user_id}",
            )

        if menu_type == Menus.CLEANUP:
            msg = "Select cloud to delete trash"
        elif menu_type == Menus.STORAGE:
            msg = "Select cloud to view info"
        else:
            msg = "Select cloud where your files are stored\n\n"

        buttons.data_button("✘ Close", f"{menu_type}^close^{user_id}", "footer")

        if edit:
            await edit_message(message, msg, buttons.build_menu(2))
        else:
            await send_message(message, msg, buttons.build_menu(2))

    except Exception as e:
        LOGGER.error(f"Error listing remotes: {e}", exc_info=True)
        error_msg = f"Failed to list remotes.\n\n<b>Error:</b> <code>{str(e)}</code>"
        if edit:
            await edit_message(message, error_msg)
        else:
            await send_message(message, error_msg)


async def list_folder(
    message,
    rclone_remote,
    base_dir,
    menu_type,
    is_second_menu=False,
    is_crypt=False,
    edit=False,
):
    """List folder contents from rclone remote"""
    try:
        user_id = get_message_owner_id(message)
        buttons = ButtonMaker()
        msg = ""
        next_type = ""
        dir_callback = "remote_dir"
        file_callback = ""
        back_callback = "back"

        cmd = [
            "rclone",
            "lsjson",
            f"--config={rclone_config}",
            f"{rclone_remote}:{base_dir}",
            "--fast-list",
            "--max-depth=1",
        ]

        if menu_type == Menus.MYFILES:
            next_type = "next_myfiles"
            file_callback = "file_action"
            cmd.extend(["--no-modtime"])
            buttons.data_button(
                "⚙️ Folder Options", f"{menu_type}^folder_action^{user_id}"
            )
            buttons.data_button("🔍 Search", f"myfilesmenu^search^{user_id}")
            msg = f"Your cloud files are listed below\n\n<b>Path:</b><code>{rclone_remote}:{base_dir}</code>"

        # Use cache for frequently accessed folders
        cache_key = f"ls:{rclone_remote}:{base_dir}"
        res, err, rc = await execute_rclone_cmd(cmd, cache_key=cache_key)

        if rc != 0:
            LOGGER.error(f"Error listing folder: {err}")
            error_msg = f"Failed to list folder contents.\n\n<b>Path:</b> <code>{rclone_remote}:{base_dir}</code>\n<b>Error:</b> <code>{err[:200]}</code>"
            if edit:
                await edit_message(message, error_msg)
            else:
                await send_message(message, error_msg)
            return

        info = loads(res) if res else []

        if is_second_menu:
            sinfo = sorted(info, key=lambda x: x.get("Name", ""))
        else:
            sinfo = sorted(info, key=lambda x: x.get("Size", 0), reverse=True)

        total = len(info)
        update_rclone_data("info", sinfo, user_id)

        if total == 0:
            buttons.data_button("⌀ Nothing to show ⌀", f"{menu_type}^pages^{user_id}")
        else:
            page, next_offset = await sync_to_async(rcloneListNextPage, sinfo)

            await sync_to_async(
                rcloneListButtonMaker,
                info=page,
                button=buttons,
                menu_type=menu_type,
                dir_callback=dir_callback,
                file_callback=file_callback,
                user_id=user_id,
            )

            current_page = 1
            total_pages = max(round(total / ITEMS_PER_PAGE), 1)

            if total <= ITEMS_PER_PAGE:
                buttons.data_button(
                    f"🔢 {current_page} / {total_pages}",
                    f"{menu_type}^pages^{user_id}",
                    "footer",
                )
            else:
                buttons.data_button(
                    f"🔢 {current_page} / {total_pages}",
                    f"{menu_type}^pages^{user_id}",
                    "footer",
                )
                buttons.data_button(
                    "NEXT ⏩",
                    f"{next_type} {next_offset} {is_second_menu} {back_callback}",
                    "footer",
                )

        buttons.data_button(
            "⬅️ Back", f"{menu_type}^{back_callback}^{user_id}", "footer"
        )
        buttons.data_button(
            "✘ Close", f"{menu_type}^close^{user_id}", "footer"
        )

        if edit:
            await edit_message(message, msg, buttons.build_menu(1))
        else:
            await send_message(message, msg, buttons.build_menu(1))

    except Exception as e:
        LOGGER.error(f"Error in list_folder: {e}", exc_info=True)
        error_msg = f"An unexpected error occurred.\n\n<code>{str(e)}</code>"
        if edit:
            await edit_message(message, error_msg)
        else:
            await send_message(message, error_msg)


async def storage_menu_cb(client, callback_query):
    """Handle storage menu callbacks"""
    try:
        query = callback_query
        data = query.data
        cmd = data.split("^")
        message = query.message
        user_id = query.from_user.id

        if int(cmd[-1]) != user_id:
            await query.answer("Not yours!", show_alert=True)
            return

        if cmd[1] == "remote":
            await rclone_about(message, query, cmd[2], user_id)
        elif cmd[1] == "back":
            await list_remotes(message, menu_type=Menus.STORAGE, edit=True)
            await query.answer()
        elif cmd[1] == "close":
            await query.answer()
            await delete_message(message.reply_to_message)
            await delete_message(message)

    except Exception as e:
        LOGGER.error(f"Storage menu callback error: {e}", exc_info=True)
        await query.answer(f"Error: {str(e)[:100]}", show_alert=True)


async def rclone_about(message, query, remote_name, user_id):
    """Get storage information for a remote"""
    try:
        button = ButtonMaker()
        cmd = [
            "rclone",
            "about",
            "--json",
            f"--config={rclone_config}",
            f"{remote_name}:",
        ]

        stdout, stderr, return_code = await execute_rclone_cmd(cmd, timeout=30)

        if return_code != 0:
            LOGGER.error(f"Error getting storage info: {stderr}")
            await query.answer(f"Failed: {stderr[:100]}", show_alert=True)
            return

        info = loads(stdout) if stdout else {}

        if len(info) == 0:
            await query.answer("Team Drive with Unlimited Storage", show_alert=True)
            return

        result_msg = "<b>📊 Storage Details</b>\n"

        try:
            used = get_readable_file_size(info["used"])
            total = get_readable_file_size(info["total"])
            free = get_readable_file_size(info["free"])
            used_percentage = 100 * float(info["used"]) / float(info["total"])
            used_bar = get_used_bar(used_percentage)
            used_percentage = f"{round(used_percentage, 2)}%"
            free_percentage = round((info["free"] * 100) / info["total"], 2)
            free_percentage = f"{free_percentage}%"
            result_msg += used_bar
            result_msg += f"<b>\nUsed:</b> {used} of {total}"
            result_msg += f"<b>\nFree:</b> {free} of {total}"
            result_msg += f"<b>\nTrashed:</b> {get_readable_file_size(info.get('trashed', 0))}"
            result_msg += f"<b>\n\nStorage used:</b> {used_percentage}"
            result_msg += f"<b>\nStorage free:</b> {free_percentage}"
        except KeyError as e:
            LOGGER.error(f"Missing key in storage info: {e}")
            result_msg += f"<b>\nN/A:</b> Information not available"

        button.data_button("⬅️ Back", f"storagemenu^back^{user_id}", "footer")
        button.data_button("✘ Close", f"storagemenu^close^{user_id}", "footer")

        await edit_message(message, result_msg, button.build_menu(1))

    except Exception as e:
        LOGGER.error(f"Error in rclone_about: {e}", exc_info=True)
        await query.answer(f"Error: {str(e)[:100]}", show_alert=True)


def get_used_bar(percentage):
    """Create a visual bar for storage usage"""
    filled = floor(percentage / 10)
    empty = 10 - filled
    return "".join(["▓ " for _ in range(filled)]) + "".join(["░" for _ in range(empty)])


async def myfiles_settings(message, remote, remote_path, edit=False, is_folder=False):
    """Display file/folder settings menu"""
    try:
        if message.reply_to_message:
            user_id = get_message_owner_id(message)
        else:
            user_id = message.from_user.id

        buttons = ButtonMaker()

        if len(remote_path) == 0:
            buttons.data_button("📊 Folder size", f"myfilesmenu^size^{user_id}")
            buttons.data_button("📁 Create empty dir", f"myfilesmenu^mkdir^{user_id}")
            buttons.data_button("🗑 Delete empty dir", f"myfilesmenu^rmdir^{user_id}")
            buttons.data_button("🗑 Delete duplicate files", f"myfilesmenu^dedupe^{user_id}")
        else:
            if is_folder:
                buttons.data_button("📊 Folder size", f"myfilesmenu^size^{user_id}")
                buttons.data_button("🗑 Delete duplicate files", f"myfilesmenu^dedupe^{user_id}")
                buttons.data_button("🗑 Delete folder", f"myfilesmenu^delete^folder^{user_id}")
                buttons.data_button("📁 Create empty dir", f"myfilesmenu^mkdir^{user_id}")
                buttons.data_button("🗑 Delete empty dir", f"myfilesmenu^rmdir^{user_id}")
            else:
                buttons.data_button("📝 Rename", f"myfilesmenu^rename^file^{user_id}")
                buttons.data_button("🗑 Delete", f"myfilesmenu^delete^file^{user_id}")
                buttons.data_button("🔗 Get Link", f"myfilesmenu^getlink^{user_id}")
                buttons.data_button("📄 Get Mediainfo", f"myfilesmenu^mediainfo^{user_id}")

        buttons.data_button("⬅️ Back", f"myfilesmenu^back_remotes_menu^{user_id}", "footer")
        buttons.data_button("✘ Close", f"myfilesmenu^close^{user_id}", "footer")

        msg = f"<b>Path:</b><code>{remote}:{remote_path}</code>"

        if edit:
            await edit_message(message, msg, buttons.build_menu(2))
        else:
            await send_message(message, msg, buttons.build_menu(2))

    except Exception as e:
        LOGGER.error(f"Error in myfiles_settings: {e}", exc_info=True)


async def calculate_size_background(message, remote_path, remote, user_id):
    """Calculate folder size in background with progress updates"""
    buttons = ButtonMaker()
    buttons.data_button("⬅️ Back", f"myfilesmenu^back_remotes_menu^{user_id}", "footer")
    buttons.data_button("✘ Close", f"myfilesmenu^close^{user_id}", "footer")
    
    try:
        start_time = time()
        await edit_message(
            message,
            "**⏳ Calculating folder size...**\n\nThis may take a while for large folders.",
            buttons.build_menu(1)
        )

        cmd = [
            "rclone",
            "size",
            "--fast-list",
            f"--config={rclone_config}",
            f"{remote}:{remote_path}",
            "--json",
        ]

        stdout, stderr, return_code = await execute_rclone_cmd(cmd, timeout=RCLONE_TIMEOUT)

        if return_code != 0:
            LOGGER.error(f"Error calculating size: {stderr}")
            await edit_message(
                message,
                f"❌ Failed to calculate folder size.\n\n<b>Error:</b> <code>{stderr[:200]}</code>",
                buttons.build_menu(1)
            )
            return

        data = loads(stdout) if stdout else {}
        files = data.get("count", 0)
        size = data.get("bytes", 0)
        total_size = get_readable_file_size(size)
        elapsed = round(time() - start_time, 2)
        
        msg = f"<b>✅ Calculation Complete</b>\n\n"
        msg += f"<b>Total Files:</b> {files}\n"
        msg += f"<b>Folder Size:</b> {total_size}\n"
        msg += f"<b>Time taken:</b> {elapsed}s"
        
        await edit_message(message, msg, buttons.build_menu(1))

    except Exception as e:
        LOGGER.error(f"Error calculating size: {e}", exc_info=True)
        await edit_message(
            message,
            f"❌ Error: <code>{str(e)}</code>",
            buttons.build_menu(1)
        )


async def calculate_size(message, remote_path, remote, user_id):
    """Calculate total size of a folder (wrapper to run in background)"""
    # Cancel any existing size calculation
    task_key = f"size_{user_id}"
    if task_key in active_tasks:
        active_tasks[task_key].cancel()
    
    # Start new background task
    task = asyncio.create_task(calculate_size_background(message, remote_path, remote, user_id))
    active_tasks[task_key] = task
    
    # Cleanup task when done
    def cleanup(t):
        if task_key in active_tasks:
            del active_tasks[task_key]
    
    task.add_done_callback(cleanup)


async def search_action(client, message, query, remote, user_id):
    """Search for files in remote"""
    try:
        question = await send_message(message, "Send file name to search, /ignore to cancel")

        async def handle_response(client, response_message):
            try:
                if response_message.from_user.id == user_id:
                    text = response_message.text
                    if "/ignore" in text:
                        await edit_message(question, "Search canceled.")
                        await delete_message(response_message)
                    else:
                        await edit_message(
                            question,
                            "**⏳ Searching file(s) on remote...**\n\nPlease wait, it may take some time"
                        )

                        cmd = [
                            "rclone",
                            "lsjson",
                            "--files-only",
                            "--fast-list",
                            "--no-modtime",
                            "--ignore-case",
                            "-R",
                            f"--config={rclone_config}",
                            "--include",
                            f"*{text}*",
                            f"{remote}:",
                        ]

                        out, err, return_code = await execute_rclone_cmd(cmd, timeout=SEARCH_TIMEOUT)

                        if return_code != 0:
                            LOGGER.error(f"Search error: {err}")
                            await edit_message(question, f"An error occurred during search.\n\n<code>{err[:200]}</code>")
                        elif out and len(out) > 0:
                            data = loads(out)
                            msg = f"<b>Found {len(data)} files:\n\n</b>"

                            for index, file in enumerate(data[:50], start=1):
                                name = file["Name"]
                                path = file["Path"]

                                # Try to get link
                                cmd_link = [
                                    "rclone",
                                    "link",
                                    f"--config={rclone_config}",
                                    f"{remote}:{path}",
                                ]

                                link_out, link_err, link_rc = await execute_rclone_cmd(cmd_link, timeout=10)

                                if link_rc == 0 and link_out:
                                    msg += f"{index}. <a href='{link_out}'>{name}</a>\n"
                                else:
                                    msg += f"{index}. <code>{name}</code>\n"

                            if len(data) > 50:
                                msg += f"\n<i>... and {len(data) - 50} more files</i>"

                            await edit_message(question, msg)
                        else:
                            await edit_message(question, "No file(s) found")

                client.remove_handler(handler)

            except Exception as e:
                LOGGER.error(f"Error in search response handler: {e}", exc_info=True)
                await edit_message(question, f"An error occurred during search.\n\n<code>{str(e)}</code>")
                client.remove_handler(handler)

        handler = MessageHandler(handle_response, filters.text & filters.user(user_id))
        client.add_handler(handler)

    except Exception as e:
        LOGGER.error(f"Error in search_action: {e}", exc_info=True)


async def delete_selection(message, user_id, is_folder=False):
    """Confirmation dialog for deletion"""
    try:
        buttons = ButtonMaker()
        msg = ""

        if is_folder:
            buttons.data_button("Yes", f"myfilesmenu^yes^folder^{user_id}")
            buttons.data_button("No", f"myfilesmenu^no^folder^{user_id}")
            msg = "⚠️ Are you sure you want to delete this folder permanently?"
        else:
            buttons.data_button("Yes", f"myfilesmenu^yes^file^{user_id}")
            buttons.data_button("No", f"myfilesmenu^no^file^{user_id}")
            msg = "⚠️ Are you sure you want to delete this file permanently?"

        await edit_message(message, msg, buttons.build_menu(2))

    except Exception as e:
        LOGGER.error(f"Error in delete_selection: {e}", exc_info=True)


async def delete_selected(message, user_id, remote_path, remote, is_folder=False):
    """Delete selected file or folder"""
    try:
        buttons = ButtonMaker()
        msg = ""

        if is_folder:
            success = await rclone_purge(message, remote_path, remote, rclone_config)
            if success:
                msg = "✅ The folder has been deleted successfully!"
            else:
                msg = "❌ Failed to delete the folder. Check logs for details."
        else:
            success = await rclone_delete(message, remote_path, remote, rclone_config)
            if success:
                msg = "✅ The file has been deleted successfully!"
            else:
                msg = "❌ Failed to delete the file. Check logs for details."

        buttons.data_button("⬅️ Back", f"myfilesmenu^back_remotes_menu^{user_id}", "footer")
        buttons.data_button("✘ Close", f"myfilesmenu^close^{user_id}", "footer")

        await edit_message(message, msg, buttons.build_menu(1))
        
        # Invalidate cache for this path
        process_cache.clear()

    except Exception as e:
        LOGGER.error(f"Error in delete_selected: {e}", exc_info=True)
        await edit_message(message, f"An error occurred during deletion.\n\n<code>{str(e)}</code>")


async def delete_empty_dir(message, user_id, remote, remote_path):
    """Delete empty directories"""
    try:
        buttons = ButtonMaker()
        success = await rclone_rmdirs(message, remote, remote_path, rclone_config)

        buttons.data_button("⬅️ Back", f"myfilesmenu^back_remotes_menu^{user_id}", "footer")
        buttons.data_button("✘ Close", f"myfilesmenu^close^{user_id}", "footer")

        if success:
            msg = "✅ Directories successfully deleted!"
        else:
            msg = "❌ Failed to delete directories. Check logs for details."

        await edit_message(message, msg, buttons.build_menu(1))
        
        # Invalidate cache
        process_cache.clear()

    except Exception as e:
        LOGGER.error(f"Error in delete_empty_dir: {e}", exc_info=True)
        await edit_message(message, f"An error occurred.\n\n<code>{str(e)}</code>")


async def rclone_purge(message, remote_path, remote, rclone_config):
    """Purge (delete) a folder"""
    try:
        await edit_message(message, "⏳ Deleting folder...")
        
        cmd = ["rclone", "purge", f"--config={rclone_config}", f"{remote}:{remote_path}"]
        stdout, stderr, return_code = await execute_rclone_cmd(cmd, timeout=RCLONE_TIMEOUT)

        if return_code != 0:
            LOGGER.error(f"Error purging folder: {stderr}")
            return False
        return True

    except Exception as e:
        LOGGER.error(f"Error in rclone_purge: {e}", exc_info=True)
        return False


async def rclone_delete(message, remote_path, remote, rclone_config):
    """Delete a file"""
    try:
        await edit_message(message, "⏳ Deleting file...")
        
        cmd = ["rclone", "delete", f"--config={rclone_config}", f"{remote}:{remote_path}"]
        stdout, stderr, return_code = await execute_rclone_cmd(cmd, timeout=60)

        if return_code != 0:
            LOGGER.error(f"Error deleting file: {stderr}")
            return False
        return True

    except Exception as e:
        LOGGER.error(f"Error in rclone_delete: {e}", exc_info=True)
        return False


async def rclone_rmdirs(message, remote, remote_path, rclone_config):
    """Remove empty directories"""
    try:
        await edit_message(
            message,
            "**⏳ Removing empty directories...**\n\nPlease wait, it may take some time depending on number of dirs"
        )

        cmd = ["rclone", "rmdirs", f"--config={rclone_config}", f"{remote}:{remote_path}"]
        stdout, stderr, return_code = await execute_rclone_cmd(cmd, timeout=RCLONE_TIMEOUT)

        if return_code != 0:
            LOGGER.error(f"Error removing directories: {stderr}")
            return False
        return True

    except Exception as e:
        LOGGER.error(f"Error in rclone_rmdirs: {e}", exc_info=True)
        return False


async def rclone_mkdir(client, message, remote, remote_path, tag):
    """Create a new directory"""
    try:
        user_id = get_message_owner_id(message)
        question = await send_message(message, "Send name for directory, /ignore to cancel")

        async def handle_response(client, response_message):
            try:
                if response_message.from_user.id == user_id:
                    text = response_message.text
                    if "/ignore" in text:
                        await edit_message(question, "Directory creation canceled.")
                        await delete_message(response_message)
                    else:
                        await edit_message(question, "⏳ Creating Directory...")
                        
                        # Sanitize directory name
                        text = text.strip().replace("..", "").replace("/", "_")
                        path = f"{remote_path}/{text}".strip("/")

                        cmd = [
                            "rclone",
                            "mkdir",
                            f"--config={rclone_config}",
                            f"{remote}:{path}",
                        ]

                        stdout, stderr, return_code = await execute_rclone_cmd(cmd, timeout=30)

                        if return_code != 0:
                            LOGGER.error(f"Error creating directory: {stderr}")
                            await edit_message(question, f"An error occurred during directory creation.\n\n<code>{stderr[:200]}</code>")
                        else:
                            msg = "<b>✅ Directory created successfully.</b>\n\n"
                            msg += f"<b>Path: </b><code>{remote}:{path}</code>\n\n"
                            msg += f"<b>cc:</b> {tag}\n\n"
                            await edit_message(question, msg)
                            
                            # Invalidate cache
                            process_cache.clear()

                client.remove_handler(handler)

            except Exception as e:
                LOGGER.error(f"Error in mkdir response handler: {e}", exc_info=True)
                await edit_message(question, f"An error occurred.\n\n<code>{str(e)}</code>")
                client.remove_handler(handler)

        handler = MessageHandler(handle_response, filters.text & filters.user(user_id))
        client.add_handler(handler)

    except Exception as e:
        LOGGER.error(f"Error in rclone_mkdir: {e}", exc_info=True)


async def rclone_dedupe(message, remote, remote_path, user_id, tag):
    """Remove duplicate files"""
    try:
        msg = "**⏳ Deleting duplicate files**\n"
        msg += "\nIt may take some time depending on number of duplicates files"
        await edit_message(message, msg)

        cmd = [
            "rclone",
            "dedupe",
            "newest",
            "--tpslimit",
            "4",
            "--transfers",
            "1",
            "--fast-list",
            f"--config={rclone_config}",
            f"{remote}:{remote_path}",
        ]

        stdout, stderr, return_code = await execute_rclone_cmd(cmd, timeout=RCLONE_TIMEOUT)

        if return_code != 0:
            LOGGER.error(f"Error deduplicating: {stderr}")
            msg = f"❌ Dedupe failed.\n\n<b>Error:</b> <code>{stderr[:200]}</code>"
        else:
            msg = "<b>✅ Dedupe completed successfully</b>\n"
            msg += f"<b>cc:</b> {tag}\n"

        button = ButtonMaker()
        button.data_button("⬅️ Back", f"myfilesmenu^back_remotes_menu^{user_id}", "footer")
        button.data_button("✘ Close", f"myfilesmenu^close^{user_id}", "footer")
        await edit_message(message, msg, button.build_menu(1))
        
        # Invalidate cache
        process_cache.clear()

    except Exception as e:
        LOGGER.error(f"Error in rclone_dedupe: {e}", exc_info=True)
        await edit_message(message, f"An error occurred during deduplication.\n\n<code>{str(e)}</code>")


async def rclone_rename(client, message, remote, remote_path, tag):
    """Rename a file"""
    try:
        user_id = get_message_owner_id(message)
        question = await send_message(message, "Send new name for file, /ignore to cancel")

        async def handle_response(client, response_message):
            try:
                if response_message.from_user.id == user_id:
                    text = response_message.text
                    if "/ignore" in text:
                        await edit_message(question, "Rename canceled.")
                        await delete_message(response_message)
                    else:
                        await edit_message(question, "⏳ Renaming file...")
                        
                        # Sanitize filename
                        text = text.strip().replace("..", "").replace("/", "_")
                        list_base = remote_path.split("/")

                        if len(list_base) > 1:
                            dest = "/".join(list_base[:-1])
                            file = list_base[-1]
                            _, ext = splitext(file)
                            path = f"{dest}/{text}{ext}"
                        else:
                            file = list_base[0]
                            _, ext = splitext(file)
                            path = f"{text}{ext}"

                        cmd = [
                            "rclone",
                            "moveto",
                            f"--config={rclone_config}",
                            f"{remote}:{remote_path}",
                            f"{remote}:{path}",
                        ]

                        stdout, stderr, return_code = await execute_rclone_cmd(cmd, timeout=60)

                        if return_code != 0:
                            LOGGER.error(f"Error renaming file: {stderr}")
                            await edit_message(question, f"An error occurred during renaming.\n\n<code>{stderr[:200]}</code>")
                        else:
                            msg = "<b>✅ File renamed successfully.</b>\n\n"
                            msg += f"<b>Old path: </b><code>{remote}:{remote_path}</code>\n\n"
                            msg += f"<b>New path: </b><code>{remote}:{path}</code>\n\n"
                            msg += f"<b>cc: {tag}</b>"
                            await edit_message(question, msg)
                            
                            # Invalidate cache
                            process_cache.clear()

                client.remove_handler(handler)

            except Exception as e:
                LOGGER.error(f"Error in rename response handler: {e}", exc_info=True)
                await edit_message(question, f"An error occurred.\n\n<code>{str(e)}</code>")
                client.remove_handler(handler)

        handler = MessageHandler(handle_response, filters.text & filters.user(user_id))
        client.add_handler(handler)

    except Exception as e:
        LOGGER.error(f"Error in rclone_rename: {e}", exc_info=True)


async def rclone_get_link(client, message, remote, remote_path, user_id):
    """Get direct link for a file"""
    try:
        await edit_message(message, "⏳ Getting link...")
        
        cmd_link = ["rclone", "link", f"--config={rclone_config}", f"{remote}:{remote_path}"]
        stdout, stderr, return_code = await execute_rclone_cmd(cmd_link, timeout=30)

        buttons = ButtonMaker()
        buttons.data_button("⬅️ Back", f"myfilesmenu^back_remotes_menu^{user_id}", "footer")
        buttons.data_button("✘ Close", f"myfilesmenu^close^{user_id}", "footer")

        if return_code != 0:
            error_msg = stderr if stderr else "Unknown error"
            await edit_message(message, f"❌ Error: <code>{error_msg[:200]}</code>", buttons.build_menu(2))
        else:
            direct_link = stdout if stdout else "No link available"
            await edit_message(
                message,
                f"<b>🔗 Direct Link:</b>\n\n<code>{direct_link}</code>",
                buttons.build_menu(2)
            )

    except Exception as e:
        LOGGER.error(f"Error in rclone_get_link: {e}", exc_info=True)
        await edit_message(message, f"An error occurred while getting the link.\n\n<code>{str(e)}</code>")


async def rclone_get_mediainfo(client, message, remote, remote_path, user_id):
    """Get media information for a file"""
    buttons = ButtonMaker()
    buttons.data_button("⬅️ Back", f"myfilesmenu^back_remotes_menu^{user_id}", "footer")
    buttons.data_button("✘ Close", f"myfilesmenu^close^{user_id}", "footer")

    await edit_message(message, "⏳ Getting media info...", buttons.build_menu(2))

    rclone_proc = None
    mediainfo_proc = None
    file_name = None

    try:
        file_name = f"mediainfo_{user_id}_{int(time())}.txt"

        # Start rclone cat process with 10MB limit
        rclone_proc = await exec(
            "rclone",
            "cat",
            f"--config={rclone_config}",
            f"{remote}:{remote_path}",
            "--count=10485760",
            stdout=PIPE,
            stderr=PIPE
        )

        # Start mediainfo process
        mediainfo_proc = await exec(
            "mediainfo",
            "-",
            stdin=PIPE,
            stdout=PIPE,
            stderr=PIPE
        )

        # Process streams with timeout
        await asyncio.wait_for(
            process_streams(rclone_proc, mediainfo_proc, file_name, message, buttons),
            timeout=MEDIAINFO_TIMEOUT
        )

    except asyncio.TimeoutError:
        LOGGER.error("Mediainfo operation timed out")
        await edit_message(message, "⏱️ Operation timed out (60s limit)", buttons.build_menu(2))
    except Exception as e:
        LOGGER.error(f"Error in rclone_get_mediainfo: {e}", exc_info=True)
        await edit_message(message, f"❌ Error: <code>{str(e)[:300]}</code>", buttons.build_menu(2))
    finally:
        # Cleanup processes - more aggressive approach
        for proc in [rclone_proc, mediainfo_proc]:
            if proc and proc.returncode is None:
                try:
                    proc.terminate()
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=5)
                    except asyncio.TimeoutError:
                        LOGGER.warning("Process didn't terminate gracefully, killing...")
                        proc.kill()
                        await proc.wait()
                except Exception as e:
                    LOGGER.error(f"Process cleanup error: {e}")

        # Cleanup temporary file
        if file_name:
            try:
                if await aiopath.exists(file_name):
                    await remove(file_name)
            except Exception as e:
                LOGGER.error(f"File cleanup error: {e}")


async def process_streams(rclone, mediainfo, file_name, message, buttons):
    """Process streams between rclone and mediainfo"""
    try:
        # Stream data from rclone to mediainfo with timeout protection
        bytes_read = 0
        max_bytes = 10 * 1024 * 1024  # 10MB hard limit
        
        while bytes_read < max_bytes:
            chunk = await rclone.stdout.read(MAX_STREAM_CHUNK)
            if not chunk:
                break
            bytes_read += len(chunk)
            mediainfo.stdin.write(chunk)
            await mediainfo.stdin.drain()

        mediainfo.stdin.close()
        stdout, stderr = await mediainfo.communicate()

        # Check rclone errors
        rclone_rc = await rclone.wait()
        if rclone_rc != 0:
            rclone_err = (await rclone.stderr.read()).decode().strip()
            LOGGER.error(f"Rclone error (rc={rclone_rc}): {rclone_err}")
            await edit_message(message, f"❌ Rclone error: <code>{rclone_err[:200]}</code>", buttons.build_menu(2))
            return

        # Check mediainfo errors
        if mediainfo.returncode != 0:
            mediainfo_err = stderr.decode().strip()
            LOGGER.error(f"Mediainfo error: {mediainfo_err}")
            await edit_message(message, f"❌ Mediainfo error: <code>{mediainfo_err[:200]}</code>", buttons.build_menu(2))
            return

        output = stdout.decode().strip()
        if not output:
            await edit_message(message, "❌ No media information found", buttons.build_menu(2))
            return

        # Write to file and send
        async with aiopen(file_name, "w") as f:
            await f.write(output)

        await send_file(message, file_name)
        await edit_message(message, "✅ Mediainfo generated successfully", buttons.build_menu(2))

    except Exception as e:
        LOGGER.error(f"Error in process_streams: {e}", exc_info=True)
        await edit_message(message, f"❌ Stream error: <code>{str(e)[:200]}</code>", buttons.build_menu(2))


async def myfiles_callback(client, callback_query):
    """Handle myfiles menu callbacks"""
    try:
        query = callback_query
        data = query.data
        cmd = data.split("^")
        message = query.message
        tag = get_user_tag(message)
        user_id = query.from_user.id
        base_dir = get_rclone_data("MYFILES_BASE_DIR", user_id)
        rclone_remote = get_rclone_data("MYFILES_REMOTE", user_id)
        is_folder = False

        if int(cmd[-1]) != user_id:
            await query.answer("Not yours!", show_alert=True)
            return

        if cmd[1] == "remote":
            update_rclone_data("MYFILES_BASE_DIR", "", user_id)
            update_rclone_data("MYFILES_REMOTE", cmd[2], user_id)
            await list_folder(message, cmd[2], "", menu_type=Menus.MYFILES, edit=True)
            await query.answer()

        elif cmd[1] == "remote_dir":
            path = get_rclone_data(cmd[2], user_id)
            base_dir += path + "/"
            if await is_valid_path(rclone_remote, base_dir, message):
                update_rclone_data("MYFILES_BASE_DIR", base_dir, user_id)
                await list_folder(
                    message, rclone_remote, base_dir, menu_type=Menus.MYFILES, edit=True
                )
            else:
                await query.answer("Invalid path!", show_alert=True)
            await query.answer()

        elif cmd[1] == "back":
            if len(base_dir) == 0:
                await list_remotes(message, menu_type=Menus.MYFILES, edit=True)
                await query.answer()
                return

            base_dir_split = base_dir.rstrip("/").split("/")[:-1]
            base_dir = "/".join(base_dir_split)
            if base_dir:
                base_dir += "/"

            update_rclone_data("MYFILES_BASE_DIR", base_dir, user_id)
            await list_folder(
                message, rclone_remote, base_dir, menu_type=Menus.MYFILES, edit=True
            )
            await query.answer()

        elif cmd[1] == "back_remotes_menu":
            await list_remotes(message, menu_type=Menus.MYFILES, edit=True)
            await query.answer()

        elif cmd[1] == "file_action":
            path = get_rclone_data(cmd[2], user_id)
            base_dir += path
            update_rclone_data("MYFILES_BASE_DIR", base_dir, user_id)
            await myfiles_settings(
                message, rclone_remote, base_dir, edit=True, is_folder=False
            )
            await query.answer()

        elif cmd[1] == "folder_action":
            await myfiles_settings(
                message, rclone_remote, base_dir, edit=True, is_folder=True
            )
            await query.answer()

        elif cmd[1] == "search":
            await query.answer()
            await search_action(client, message, query, rclone_remote, user_id)

        elif cmd[1] == "delete":
            if cmd[2] == "folder":
                is_folder = True
            await delete_selection(message, user_id, is_folder=is_folder)
            await query.answer()

        elif cmd[1] == "size":
            await query.answer()
            await calculate_size(message, base_dir, rclone_remote, user_id)

        elif cmd[1] == "mkdir":
            await query.answer()
            await rclone_mkdir(client, message, rclone_remote, base_dir, tag)

        elif cmd[1] == "rmdir":
            await query.answer()
            await delete_empty_dir(message, user_id, rclone_remote, base_dir)

        elif cmd[1] == "dedupe":
            await query.answer()
            await rclone_dedupe(message, rclone_remote, base_dir, user_id, tag)

        elif cmd[1] == "rename":
            await query.answer()
            await rclone_rename(client, message, rclone_remote, base_dir, tag)

        elif cmd[1] == "getlink":
            await query.answer()
            await rclone_get_link(client, message, rclone_remote, base_dir, user_id)

        elif cmd[1] == "mediainfo":
            await query.answer()
            await rclone_get_mediainfo(client, message, rclone_remote, base_dir, user_id)

        elif cmd[1] == "yes":
            if cmd[2] == "folder":
                is_folder = True
            await delete_selected(
                message, user_id, base_dir, rclone_remote, is_folder=is_folder
            )
            await query.answer()

        elif cmd[1] == "no":
            await query.answer()
            await delete_message(message.reply_to_message)
            await delete_message(message)

        elif cmd[1] == "pages":
            await query.answer()

        elif cmd[1] == "close":
            await query.answer()
            await delete_message(message.reply_to_message)
            await delete_message(message)

        else:
            await query.answer()

    except Exception as e:
        LOGGER.error(f"Error in myfiles_callback: {e}", exc_info=True)
        await query.answer(f"Error: {str(e)[:100]}", show_alert=True)


async def next_page_myfiles(client, callback_query):
    """Handle pagination for myfiles"""
    try:
        query = callback_query
        data = query.data
        message = query.message
        await query.answer()
        user_id = get_message_owner_id(message, fallback_user_id=query.from_user.id)
        _, next_offset, _, data_back_cb = data.split()

        info = get_rclone_data("info", user_id)
        total = len(info)
        next_offset = int(next_offset)
        prev_offset = max(next_offset - ITEMS_PER_PAGE, 0)

        buttons = ButtonMaker()
        buttons.data_button(f"⚙️ Folder Options", f"myfilesmenu^folder_action^{user_id}")
        buttons.data_button("🔍 Search", f"myfilesmenu^search^{user_id}")

        next_info, _next_offset = await sync_to_async(
            rcloneListNextPage, info, next_offset
        )

        await sync_to_async(
            rcloneListButtonMaker,
            info=next_info,
            button=buttons,
            menu_type=Menus.MYFILES,
            dir_callback="remote_dir",
            file_callback="file_action",
            user_id=user_id,
        )

        await create_next_buttons(
            next_offset,
            prev_offset,
            _next_offset,
            data_back_cb,
            total,
            user_id,
            buttons,
            filter="next_myfiles",
            menu_type=Menus.MYFILES,
        )

        remote = get_rclone_data("MYFILES_REMOTE", user_id)
        base_dir = get_rclone_data("MYFILES_BASE_DIR", user_id)
        msg = f"Your cloud files are listed below\n\n<b>Path:</b><code>{remote}:{base_dir}</code>"

        await edit_message(message, msg, buttons.build_menu(1))

    except Exception as e:
        LOGGER.error(f"Error in next_page_myfiles: {e}", exc_info=True)
        await query.answer(f"Error: {str(e)[:100]}", show_alert=True)


async def handle_storage(_, message):
    """Handle /storage command"""
    try:
        await list_remotes(message, menu_type=Menus.STORAGE)
    except Exception as e:
        LOGGER.error(f"Error handling storage command: {e}", exc_info=True)
        await send_message(message, f"An error occurred. Please try again.\n\n<code>{str(e)}</code>")


async def handle_myfiles(client, message):
    """Handle /myfiles command"""
    try:
        await list_remotes(message, menu_type=Menus.MYFILES)
    except Exception as e:
        LOGGER.error(f"Error handling myfiles command: {e}", exc_info=True)
        await send_message(message, f"An error occurred. Please try again.\n\n<code>{str(e)}</code>")


# Cleanup function for graceful shutdown
async def cleanup_resources():
    """Cleanup resources on shutdown"""
    LOGGER.info("Cleaning up myfiles resources...")
    
    # Cancel all active tasks
    for task_key, task in list(active_tasks.items()):
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    
    # Clear caches
    process_cache.clear()
    rclone_dict.clear()
    
    LOGGER.info("Myfiles cleanup complete")


# Register handlers
bot.add_handler(
    MessageHandler(
        handle_storage,
        filters=command("storage") & CustomFilters.authorized,
    )
)
bot.add_handler(
    MessageHandler(
        handle_myfiles,
        filters=command("myfiles") & CustomFilters.authorized,
    )
)
bot.add_handler(CallbackQueryHandler(storage_menu_cb, filters=regex("storagemenu")))
bot.add_handler(CallbackQueryHandler(myfiles_callback, filters=regex("myfilesmenu")))
bot.add_handler(CallbackQueryHandler(next_page_myfiles, filters=regex("next_myfiles")))
