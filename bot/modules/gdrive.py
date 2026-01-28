import io, os, pickle, asyncio
from math import floor
from time import time
from os.path import splitext
from collections import OrderedDict
from functools import partial

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

from pyrogram.filters import create
from pyrogram.handlers import MessageHandler, CallbackQueryHandler
from pyrogram import filters

from bot import LOGGER, bot
from bot.helper.ext_utils.bot_utils import sync_to_async, new_task
from bot.helper.telegram_helper.filters import CustomFilters
from bot.helper.telegram_helper.button_build import ButtonMaker
from bot.helper.telegram_helper.message_utils import send_message, edit_message, delete_message, send_file

from aiofiles import open as aiopen
from aiofiles.os import path as aiopath, remove as aioremove
from asyncio.subprocess import PIPE, create_subprocess_exec as exec

# Configuration
TOKEN_PATH = "token.pickle"
CREDENTIALS_PATH = "credentials.json"
SIZE_UNITS = ["B", "KB", "MB", "GB", "TB", "PB"]
ITEMS_PER_PAGE = 10
CACHE_TTL = 60
CACHE_MAX_SIZE = 100
SCOPES = ['https://www.googleapis.com/auth/drive']

gdrive_dict = {}
gdrive_service = None
handler_dict = {}
start = 0


class LRUCache:
    def __init__(self, max_size=CACHE_MAX_SIZE):
        self.cache = OrderedDict()
        self.max_size = max_size
    
    def get(self, key):
        if key in self.cache:
            timestamp, value = self.cache[key]
            if time() - timestamp < CACHE_TTL:
                self.cache.move_to_end(key)
                return value
            del self.cache[key]
        return None
    
    def set(self, key, value):
        self.cache[key] = (time(), value)
        self.cache.move_to_end(key)
        if len(self.cache) > self.max_size:
            self.cache.popitem(last=False)
    
    def clear(self):
        self.cache.clear()


drive_cache = LRUCache()


def get_readable_file_size(size_in_bytes):
    if size_in_bytes is None:
        return "0B"
    index = 0
    while size_in_bytes >= 1024 and index < len(SIZE_UNITS) - 1:
        size_in_bytes /= 1024
        index += 1
    return f"{size_in_bytes:.2f}{SIZE_UNITS[index]}" if index > 0 else f"{size_in_bytes:.2f}B"


def get_used_bar(percentage):
    filled = floor(percentage / 10)
    empty = 10 - filled
    return "".join(["█" for _ in range(filled)]) + "".join(["░" for _ in range(empty)])


async def init_drive_service():
    global gdrive_service
    if gdrive_service:
        return gdrive_service
    
    try:
        creds = None
        if os.path.exists(TOKEN_PATH):
            try:
                with open(TOKEN_PATH, 'rb') as token:
                    creds = pickle.load(token)
                LOGGER.info("Loaded credentials from pickle file")
            except Exception as e:
                LOGGER.error(f"Failed to load credentials: {e}")
        
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                LOGGER.info("Refreshing credentials...")
                await sync_to_async(creds.refresh, Request())
                with open(TOKEN_PATH, 'wb') as token:
                    pickle.dump(creds, token)
            else:
                if not os.path.exists(CREDENTIALS_PATH):
                    LOGGER.error(f"Credentials file not found: {CREDENTIALS_PATH}")
                    return None
                flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES)
                creds = await sync_to_async(flow.run_local_server, port=0)
                with open(TOKEN_PATH, 'wb') as token:
                    pickle.dump(creds, token)
        
        gdrive_service = await sync_to_async(build, 'drive', 'v3', credentials=creds)
        LOGGER.info("Google Drive service initialized")
        return gdrive_service
    except Exception as e:
        LOGGER.error(f"Failed to init Drive: {e}", exc_info=True)
        return None


def get_gdrive_data(key, user_id):
    return gdrive_dict.get(user_id, {}).get(key, "")


def update_gdrive_data(key, value, user_id):
    if user_id in gdrive_dict:
        gdrive_dict[user_id][key] = value
    else:
        gdrive_dict[user_id] = {key: value}


def clear_gdrive_data(user_id):
    if user_id in gdrive_dict:
        del gdrive_dict[user_id]


def gdrive_list_next_page(info, offset=0, max_results=ITEMS_PER_PAGE):
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


async def list_gdrive_dir(service, folder_id='root'):
    try:
        query = f"'{folder_id}' in parents and trashed=false"
        results = await sync_to_async(
            service.files().list,
            q=query,
            pageSize=1000,
            fields="files(id, name, mimeType, size, modifiedTime, webViewLink, webContentLink)",
            orderBy="folder,name"
        )
        response = await sync_to_async(results.execute)
        return response.get('files', [])
    except Exception as e:
        LOGGER.error(f"Error listing dir: {e}", exc_info=True)
        return []


async def get_file_metadata(service, file_id):
    try:
        result = await sync_to_async(
            service.files().get,
            fileId=file_id,
            fields="id, name, mimeType, size, modifiedTime, webViewLink, webContentLink, parents"
        )
        return await sync_to_async(result.execute)
    except Exception as e:
        LOGGER.error(f"Error getting metadata: {e}", exc_info=True)
        return None


async def delete_file(service, file_id):
    try:
        await sync_to_async(service.files().delete(fileId=file_id).execute)
        return True
    except Exception as e:
        LOGGER.error(f"Error deleting: {e}", exc_info=True)
        return False


async def rename_file(service, file_id, new_name):
    try:
        await sync_to_async(service.files().update(fileId=file_id, body={'name': new_name}).execute)
        return True
    except Exception as e:
        LOGGER.error(f"Error renaming: {e}", exc_info=True)
        return False


async def create_folder(service, folder_name, parent_id='root'):
    try:
        file_metadata = {
            'name': folder_name,
            'mimeType': 'application/vnd.google-apps.folder',
            'parents': [parent_id]
        }
        result = await sync_to_async(service.files().create(body=file_metadata, fields='id').execute)
        return result.get('id')
    except Exception as e:
        LOGGER.error(f"Error creating folder: {e}", exc_info=True)
        return None


async def search_files(service, query):
    try:
        search_query = f"name contains '{query}' and trashed=false"
        results = await sync_to_async(
            service.files().list,
            q=search_query,
            pageSize=50,
            fields="files(id, name, mimeType, size, webViewLink)",
            orderBy="folder,name"
        )
        response = await sync_to_async(results.execute)
        return response.get('files', [])
    except Exception as e:
        LOGGER.error(f"Error searching: {e}", exc_info=True)
        return []


async def get_storage_info(service):
    try:
        result = await sync_to_async(service.about().get(fields="storageQuota, user").execute)
        return result
    except Exception as e:
        LOGGER.error(f"Error getting storage: {e}", exc_info=True)
        return None


async def event_handler(client, query, pfunc, rfunc):
    chat_id = query.message.chat.id
    handler_dict[chat_id] = True
    start_time = time()

    async def event_filter(_, __, event):
        user = event.from_user or event.sender_chat
        return bool(
            user.id == query.from_user.id
            and event.chat.id == chat_id
            and event.text
        )

    handler = client.add_handler(
        MessageHandler(pfunc, filters=create(event_filter)), group=-1
    )
    while handler_dict[chat_id]:
        await asyncio.sleep(0.5)
        if time() - start_time > 60:
            handler_dict[chat_id] = False
            await rfunc()
    client.remove_handler(*handler)


async def update_gdrive_menu(query, folder_id='root', folder_name='My Drive'):
    """Update menu and ensure current folder data is synced to prevent stale cache issues"""
    user_id = query.from_user.id
    update_gdrive_data("current_folder_id", folder_id, user_id)
    update_gdrive_data("current_folder_name", folder_name, user_id)
    msg, button = await get_gdrive_buttons(user_id, folder_id, folder_name)
    await edit_message(query.message, msg, button)


async def get_gdrive_buttons(user_id, folder_id='root', folder_name='My Drive', offset=0):
    """Build Google Drive menu with manual keyboard layout for precise button positioning"""
    service = await init_drive_service()
    if not service:
        return "❌ Failed to initialize Drive service", None
    
    from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    
    # Get folder contents from cache or API
    cache_key = f"list:{folder_id}"
    cached_items = drive_cache.get(cache_key)
    items = cached_items if cached_items else await list_gdrive_dir(service, folder_id)
    if not cached_items:
        drive_cache.set(cache_key, items)
    
    total = len(items)
    update_gdrive_data("items", items, user_id)
    update_gdrive_data("current_folder_id", folder_id, user_id)
    update_gdrive_data("current_folder_name", folder_name, user_id)
    
    keyboard = []
    
    # Row 1: Top action buttons
    keyboard.append([
        InlineKeyboardButton("⚙️ Folder Options", callback_data=f"gd folderact {user_id}"),
        InlineKeyboardButton("🔍 Search", callback_data=f"gd search {user_id}")
    ])
    
    msg = f"📂 Your Google Drive files and folders\n\n<b>Current Folder:</b> <code>{folder_name}</code>"
    
    if total == 0:
        keyboard.append([InlineKeyboardButton("⌀ Nothing to show ⌀", callback_data=f"gd pages {user_id}")])
    else:
        # File/folder list - one button per row
        page, next_offset = await sync_to_async(gdrive_list_next_page, items, offset)
        for idx, item in enumerate(page):
            name = item['name']
            file_id = item['id']
            update_gdrive_data(str(idx), file_id, user_id)
            update_gdrive_data(f"name_{idx}", name, user_id)
            
            if item['mimeType'] == "application/vnd.google-apps.folder":
                keyboard.append([InlineKeyboardButton(f"📁 {name}", callback_data=f"gd open {idx} {user_id}")])
            else:
                size = get_readable_file_size(int(item.get('size', 0)))
                keyboard.append([InlineKeyboardButton(f"[{size}] {name}", callback_data=f"gd file {idx} {user_id}")])
        
        # Pagination row - dynamic: [⇐] [N/M] [⇒]
        current_page = (offset // ITEMS_PER_PAGE) + 1
        total_pages = (total + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE
        
        pagination_row = []
        if offset > 0:
            prev_offset = max(offset - ITEMS_PER_PAGE, 0)
            pagination_row.append(InlineKeyboardButton("⇐", callback_data=f"gd page {prev_offset} {user_id}"))
        pagination_row.append(InlineKeyboardButton(f"{current_page} / {total_pages}", callback_data=f"gd pages {user_id}"))
        if next_offset < total:
            pagination_row.append(InlineKeyboardButton("⇒", callback_data=f"gd page {next_offset} {user_id}"))
        keyboard.append(pagination_row)
    
    # Navigation row - dynamic: [« Back] [✘ Close] or just [✘ Close]
    nav_row = []
    if folder_id != 'root':
        nav_row.append(InlineKeyboardButton("« Back", callback_data=f"gd back {user_id}"))
    nav_row.append(InlineKeyboardButton("✘ Close", callback_data=f"gd close {user_id}"))
    keyboard.append(nav_row)
    
    return msg, InlineKeyboardMarkup(keyboard)


@new_task
async def gdrive_search(_, message, pre_event):
    user_id = message.from_user.id
    chat_id = message.chat.id
    handler_dict[chat_id] = False
    
    text = message.text.strip()
    if not text:
        await update_gdrive_menu(pre_event)
        return
    
    await delete_message(message)
    question_msg = pre_event.message
    await edit_message(question_msg, "⏳ Searching...")
    
    service = await init_drive_service()
    if not service:
        await edit_message(question_msg, "❌ Failed to init Drive")
        await update_gdrive_menu(pre_event)
        return
    
    results = await search_files(service, text)
    
    if results:
        msg = f"<b>Found {len(results)} files:\n\n</b>"
        for idx, file in enumerate(results[:50], start=1):
            name = file['name']
            link = file.get('webViewLink', 'No link')
            msg += f"{idx}. <a href='{link}'>{name}</a>\n"
        if len(results) > 50:
            msg += f"\n<i>... and {len(results) - 50} more</i>"
        await edit_message(question_msg, msg)
    else:
        await edit_message(question_msg, "No files found")
    
    await update_gdrive_menu(pre_event)


@new_task
async def gdrive_mkdir(_, message, pre_event):
    user_id = message.from_user.id
    chat_id = message.chat.id
    handler_dict[chat_id] = False
    
    text = message.text.strip()
    if not text:
        await update_gdrive_menu(pre_event)
        return
    
    await delete_message(message)
    question_msg = pre_event.message
    await edit_message(question_msg, "⏳ Creating...")
    
    text = text.replace("..", "").replace("/", "_")
    service = await init_drive_service()
    parent_id = get_gdrive_data("current_folder_id", user_id)
    
    folder_id = await create_folder(service, text, parent_id)
    
    if folder_id:
        drive_cache.clear()
        await edit_message(question_msg, f"✅ Created: <code>{text}</code>")
    else:
        await edit_message(question_msg, "❌ Failed to create")
    
    await update_gdrive_menu(pre_event)


@new_task
async def gdrive_rename(_, message, pre_event):
    user_id = message.from_user.id
    chat_id = message.chat.id
    handler_dict[chat_id] = False
    
    text = message.text.strip()
    if not text:
        await update_gdrive_menu(pre_event)
        return
    
    await delete_message(message)
    question_msg = pre_event.message
    file_id = get_gdrive_data("selected_file_id", user_id)
    
    service = await init_drive_service()
    if not service:
        await update_gdrive_menu(pre_event)
        return
    
    metadata = await get_file_metadata(service, file_id)
    if not metadata:
        await update_gdrive_menu(pre_event)
        return
    
    original_name = metadata.get('name', '')
    _, ext = splitext(original_name)
    
    await edit_message(question_msg, "⏳ Renaming...")
    text = text.replace("..", "").replace("/", "_")
    new_name = f"{text}{ext}" if ext else text
    
    success = await rename_file(service, file_id, new_name)
    
    if success:
        drive_cache.clear()
        msg = f"✅ Renamed\n\n<b>Old:</b> <code>{original_name}</code>\n<b>New:</b> <code>{new_name}</code>"
        await edit_message(question_msg, msg)
    else:
        await edit_message(question_msg, "❌ Failed")
    
    await update_gdrive_menu(pre_event)


async def handle_mediainfo(message, user_id, file_id):
    buttons = ButtonMaker()
    buttons.data_button("« Back", f"gd backlist {user_id}", "footer")
    buttons.data_button("✘ Close", f"gd close {user_id}", "footer")
    
    await edit_message(message, "⏳ Getting mediainfo...", buttons.build_menu(2))
    
    temp_file = None
    output_file = None
    
    try:
        service = await init_drive_service()
        if not service:
            await edit_message(message, "❌ Failed", buttons.build_menu(2))
            return
        
        metadata = await get_file_metadata(service, file_id)
        if not metadata:
            await edit_message(message, "❌ File not found", buttons.build_menu(2))
            return
        
        file_name = metadata.get('name', 'unknown')
        temp_file = f"temp_{user_id}_{int(time())}"
        output_file = f"mediainfo_{user_id}_{int(time())}.txt"
        
        request = service.files().get_media(fileId=file_id)
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, request, chunksize=10*1024*1024)
        
        done = False
        bytes_downloaded = 0
        while not done and bytes_downloaded < 10*1024*1024:
            status, done = await sync_to_async(downloader.next_chunk)
            if status:
                bytes_downloaded = status.resumable_progress
                if bytes_downloaded >= 10*1024*1024:
                    break
        
        async with aiopen(temp_file, 'wb') as f:
            await f.write(fh.getvalue())
        
        process = await exec('mediainfo', temp_file, stdout=PIPE, stderr=PIPE)
        
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=60)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            await edit_message(message, "⏱ Timed out", buttons.build_menu(2))
            return
        
        if process.returncode != 0:
            await edit_message(message, "❌ Mediainfo error", buttons.build_menu(2))
            return
        
        output = stdout.decode().strip()
        if not output:
            await edit_message(message, "❌ No info found", buttons.build_menu(2))
            return
        
        async with aiopen(output_file, 'w') as f:
            await f.write(f"MediaInfo: {file_name}\n\n{output}")
        
        await send_file(message, output_file)
        await edit_message(message, "✅ Complete", buttons.build_menu(2))
    except Exception as e:
        LOGGER.error(f"Mediainfo error: {e}", exc_info=True)
        await edit_message(message, f"❌ Error: <code>{str(e)[:200]}</code>", buttons.build_menu(2))
    finally:
        if temp_file and await aiopath.exists(temp_file):
            await aioremove(temp_file)
        if output_file:
            await asyncio.sleep(2)
            if await aiopath.exists(output_file):
                await aioremove(output_file)


@new_task
async def gdrive_listener(client, query):
    """
    Main callback handler for Google Drive menu interactions
    Callback format: "gd action user_id" (space-separated)
    """
    user_id = query.from_user.id
    message = query.message
    data = query.data.split()
    
    # Verify user owns this menu
    if int(data[2]) != user_id and not await CustomFilters.sudo("", query):
        await query.answer("Not yours!", show_alert=True)
        return
    
    handler_dict[message.chat.id] = False
    
    if data[1] == "close":
        await query.answer()
        if message.reply_to_message:
            await delete_message(message.reply_to_message)
        await delete_message(message)
    
    elif data[1] == "pages":
        await query.answer()
    
    elif data[1] == "back":
        await query.answer()
        service = await init_drive_service()
        if not service:
            await query.answer("Failed to init", show_alert=True)
            return
        
        folder_id = get_gdrive_data("current_folder_id", user_id)
        if folder_id == 'root':
            await delete_message(message.reply_to_message)
            await delete_message(message)
        else:
            metadata = await get_file_metadata(service, folder_id)
            if not metadata:
                await update_gdrive_menu(query, 'root', 'My Drive')
                return
            
            parent_id = metadata.get('parents', ['root'])[0]
            if parent_id == 'root':
                await update_gdrive_menu(query, 'root', 'My Drive')
            else:
                parent_meta = await get_file_metadata(service, parent_id)
                parent_name = parent_meta.get('name', 'Folder') if parent_meta else 'Folder'
                await update_gdrive_menu(query, parent_id, parent_name)
    
    elif data[1] == "backlist":
        await query.answer()
        folder_id = get_gdrive_data("current_folder_id", user_id)
        folder_name = get_gdrive_data("current_folder_name", user_id)
        await update_gdrive_menu(query, folder_id, folder_name)
    
    elif data[1] == "page":
        await query.answer()
        offset = int(data[2])
        folder_id = get_gdrive_data("current_folder_id", user_id)
        folder_name = get_gdrive_data("current_folder_name", user_id)
        msg, button = await get_gdrive_buttons(user_id, folder_id, folder_name, offset)
        await edit_message(message, msg, button)
    
    elif data[1] == "open":
        await query.answer()
        file_id = get_gdrive_data(data[2], user_id)
        file_name = get_gdrive_data(f"name_{data[2]}", user_id)
        await update_gdrive_menu(query, file_id, file_name)
    
    elif data[1] == "file":
        await query.answer()
        file_id = get_gdrive_data(data[2], user_id)
        file_name = get_gdrive_data(f"name_{data[2]}", user_id)
        update_gdrive_data("selected_file_id", file_id, user_id)
        update_gdrive_data("selected_file_name", file_name, user_id)
        
        buttons = ButtonMaker()
        buttons.data_button("✏️ Rename", f"gd rename {user_id}")
        buttons.data_button("✗ Delete", f"gd delete {user_id}")
        buttons.data_button("🔗 Get Link", f"gd getlink {user_id}")
        buttons.data_button("📄 Mediainfo", f"gd mediainfo {user_id}")
        buttons.data_button("« Back", f"gd backlist {user_id}", "footer")
        buttons.data_button("✘ Close", f"gd close {user_id}", "footer")
        await edit_message(message, f"<b>Selected:</b> <code>{file_name}</code>", buttons.build_menu(2))
    
    elif data[1] == "folderact":
        await query.answer()
        folder_id = get_gdrive_data("current_folder_id", user_id)
        folder_name = get_gdrive_data("current_folder_name", user_id)
        update_gdrive_data("selected_file_id", folder_id, user_id)
        
        buttons = ButtonMaker()
        buttons.data_button("✏️ Rename", f"gd renamefld {user_id}")
        buttons.data_button("✗ Delete", f"gd deletefld {user_id}")
        buttons.data_button("🔗 Folder Link", f"gd getlink {user_id}")
        buttons.data_button("📁 Create Folder", f"gd mkdir {user_id}")
        buttons.data_button("« Back", f"gd backlist {user_id}", "footer")
        buttons.data_button("✘ Close", f"gd close {user_id}", "footer")
        await edit_message(message, f"<b>Selected:</b> <code>{folder_name}</code>", buttons.build_menu(2))
    
    elif data[1] == "search":
        await query.answer()
        buttons = ButtonMaker()
        buttons.data_button("« Back", f"gd backlist {user_id}", "footer")
        buttons.data_button("✘ Close", f"gd close {user_id}", "footer")
        await edit_message(message, "Send file name to search\n\nTimeout: 60 sec", buttons.build_menu(2))
        pfunc = partial(gdrive_search, pre_event=query)
        rfunc = partial(update_gdrive_menu, query)
        await event_handler(client, query, pfunc, rfunc)
    
    elif data[1] == "mkdir":
        await query.answer()
        buttons = ButtonMaker()
        buttons.data_button("« Back", f"gd backlist {user_id}", "footer")
        buttons.data_button("✘ Close", f"gd close {user_id}", "footer")
        await edit_message(message, "Send folder name\n\nTimeout: 60 sec", buttons.build_menu(2))
        pfunc = partial(gdrive_mkdir, pre_event=query)
        rfunc = partial(update_gdrive_menu, query)
        await event_handler(client, query, pfunc, rfunc)
    
    elif data[1] in ["rename", "renamefld"]:
        await query.answer()
        file_id = get_gdrive_data("selected_file_id", user_id)
        service = await init_drive_service()
        if not service:
            return
        
        metadata = await get_file_metadata(service, file_id)
        if not metadata:
            return
        
        original_name = metadata.get('name', '')
        buttons = ButtonMaker()
        buttons.data_button("« Back", f"gd backlist {user_id}", "footer")
        buttons.data_button("✘ Close", f"gd close {user_id}", "footer")
        await edit_message(message, f"Send new name (without extension)\n\n<b>Current:</b> <code>{original_name}</code>\n\nTimeout: 60 sec", buttons.build_menu(2))
        pfunc = partial(gdrive_rename, pre_event=query)
        rfunc = partial(update_gdrive_menu, query)
        await event_handler(client, query, pfunc, rfunc)
    
    elif data[1] in ["delete", "deletefld"]:
        await query.answer()
        is_folder = data[1] == "deletefld"
        buttons = ButtonMaker()
        msg = f"⚠️ Delete this {'folder' if is_folder else 'file'} permanently?"
        buttons.data_button("Yes", f"gd confirmdel {user_id}")
        buttons.data_button("No", f"gd backlist {user_id}")
        await edit_message(message, msg, buttons.build_menu(2))
    
    elif data[1] == "confirmdel":
        await query.answer()
        service = await init_drive_service()
        file_id = get_gdrive_data("selected_file_id", user_id)
        current_folder = get_gdrive_data("current_folder_id", user_id)
        
        success = await delete_file(service, file_id)
        
        if success:
            drive_cache.clear()
            if file_id == current_folder:
                await update_gdrive_menu(query, 'root', 'My Drive')
            else:
                folder_name = get_gdrive_data("current_folder_name", user_id)
                await update_gdrive_menu(query, current_folder, folder_name)
        else:
            await edit_message(message, "❌ Failed to delete")
    
    elif data[1] == "getlink":
        await query.answer()
        service = await init_drive_service()
        file_id = get_gdrive_data("selected_file_id", user_id)
        metadata = await get_file_metadata(service, file_id)
        
        if not metadata:
            await query.answer("Not found", show_alert=True)
            return
        
        link = metadata.get('webViewLink', 'No link')
        buttons = ButtonMaker()
        buttons.data_button("« Back", f"gd backlist {user_id}", "footer")
        buttons.data_button("✘ Close", f"gd close {user_id}", "footer")
        await edit_message(message, f"🔗 <b>Link:</b>\n\n<code>{link}</code>", buttons.build_menu(2))
    
    elif data[1] == "mediainfo":
        await query.answer()
        file_id = get_gdrive_data("selected_file_id", user_id)
        await handle_mediainfo(message, user_id, file_id)


@new_task
async def handle_gdrive(_, message):
    try:
        msg, button = await get_gdrive_buttons(message.from_user.id)
        await send_message(message, msg, button)
    except Exception as e:
        LOGGER.error(f"Handle gdrive error: {e}", exc_info=True)
        await send_message(message, f"❌ Error: <code>{str(e)}</code>")


@new_task
async def handle_storage_info(_, message):
    try:
        service = await init_drive_service()
        if not service:
            await send_message(message, "❌ Failed to init Drive service")
            return
        
        user_id = message.from_user.id
        info = await get_storage_info(service)
        
        if not info:
            await send_message(message, "❌ Failed to get storage info")
            return
        
        quota = info.get('storageQuota', {})
        user = info.get('user', {})
        total = int(quota.get('limit', 0))
        used = int(quota.get('usage', 0))
        
        if total == 0:
            msg = "📊 <b>Google Drive Storage</b>\n\n"
            msg += f"<b>User:</b> {user.get('displayName', 'Unknown')}\n"
            msg += f"<b>Email:</b> {user.get('emailAddress', 'Unknown')}\n"
            msg += f"<b>Storage:</b> Unlimited"
        else:
            used_percentage = (used / total) * 100
            used_bar = get_used_bar(used_percentage)
            msg = "📊 <b>Google Drive Storage</b>\n\n"
            msg += f"<b>User:</b> {user.get('displayName', 'Unknown')}\n"
            msg += f"<b>Email:</b> {user.get('emailAddress', 'Unknown')}\n\n"
            msg += f"{used_bar} | {round(used_percentage)}%\n\n"
            msg += f"<b>Storage:</b> {get_readable_file_size(used)} / {get_readable_file_size(total)}"
        
        buttons = ButtonMaker()
        buttons.data_button("♻️", f"gd refreshstorage {user_id}", "footer")
        buttons.data_button("✘", f"gd closestorage {user_id}", "footer")
        await send_message(message, msg, buttons.build_menu(2))
    except Exception as e:
        LOGGER.error(f"Error in storage_info: {e}", exc_info=True)


@new_task
async def storage_callback(client, query):
    user_id = query.from_user.id
    message = query.message
    data = query.data.split()
    
    if int(data[2]) != user_id and not await CustomFilters.sudo("", query):
        await query.answer("Not yours!", show_alert=True)
        return
    
    if data[1] == "closestorage":
        await query.answer()
        if message.reply_to_message:
            await delete_message(message.reply_to_message)
        await delete_message(message)
    
    elif data[1] == "refreshstorage":
        await query.answer()
        service = await init_drive_service()
        if not service:
            await query.answer("Failed", show_alert=True)
            return
        
        info = await get_storage_info(service)
        if not info:
            await query.answer("Failed", show_alert=True)
            return
        
        quota = info.get('storageQuota', {})
        user = info.get('user', {})
        total = int(quota.get('limit', 0))
        used = int(quota.get('usage', 0))
        
        if total == 0:
            msg = "📊 <b>Google Drive Storage</b>\n\n"
            msg += f"<b>User:</b> {user.get('displayName', 'Unknown')}\n"
            msg += f"<b>Email:</b> {user.get('emailAddress', 'Unknown')}\n"
            msg += f"<b>Storage:</b> Unlimited"
        else:
            used_percentage = (used / total) * 100
            used_bar = get_used_bar(used_percentage)
            msg = "📊 <b>Google Drive Storage</b>\n\n"
            msg += f"<b>User:</b> {user.get('displayName', 'Unknown')}\n"
            msg += f"<b>Email:</b> {user.get('emailAddress', 'Unknown')}\n\n"
            msg += f"{used_bar} | {round(used_percentage)}%\n\n"
            msg += f"<b>Storage:</b> {get_readable_file_size(used)} / {get_readable_file_size(total)}"
        
        buttons = ButtonMaker()
        buttons.data_button("♻️", f"gd refreshstorage {user_id}", "footer")
        buttons.data_button("✘", f"gd closestorage {user_id}", "footer")
        await edit_message(message, msg, buttons.build_menu(2))


# Register handlers
bot.add_handler(MessageHandler(handle_gdrive, filters=filters.command("gd") & CustomFilters.authorized))
bot.add_handler(MessageHandler(handle_storage_info, filters=filters.command("gdinfo") & CustomFilters.authorized))
bot.add_handler(CallbackQueryHandler(gdrive_listener, filters=filters.regex("^gd") & ~filters.regex("refreshstorage|closestorage")))
bot.add_handler(CallbackQueryHandler(storage_callback, filters=filters.regex("refreshstorage|closestorage")))