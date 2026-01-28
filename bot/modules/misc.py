import os
import shutil
import asyncio
from pathlib import Path
from pyrogram.filters import command
from pyrogram.handlers import MessageHandler

from bot import LOGGER, bot, config_dict
from bot.helper.ext_utils.bot_utils import new_task, cmd_exec
from bot.helper.telegram_helper.bot_commands import BotCommands
from bot.helper.telegram_helper.filters import CustomFilters
from bot.helper.telegram_helper.message_utils import send_message, edit_message, send_file, delete_message


@new_task
async def github_clone_handler(_, message):
    cmd = message.text.split(maxsplit=1)
    if len(cmd) == 1:
        await send_message(message, f"usage: <code>/github <url></code>")
        return
      
    msg = await send_message(message, "⏳ Processing GitHub repository...")
    url = cmd[1].strip()
    if not url.startswith(('http://', 'https://')):
        url = f'https://{url}'
    if "github.com" not in url:
        await edit_message(msg, "âŒ Invalid GitHub URL!")
        return
    
    try:
        repo_name = url.rstrip('/').split('/')[-1].replace('.git', '')
        if not repo_name:
            raise ValueError("Invalid repo name")
        LOGGER.info(f"Extracted repo name: {repo_name}")
    except Exception as e:
        LOGGER.error(f"Repo name extraction error: {e}")
        await edit_message(msg, "âŒ Could not extract repository name from URL!")
        return
    
    user_id = message.from_user.id
    download_dir = config_dict.get('DOWNLOAD_DIR', 'downloads/')
    LOGGER.info(f"Download dir: {download_dir}")
    base_dir = Path(download_dir).resolve()
    LOGGER.info(f"Base dir resolved: {base_dir}")
    
    try:
        base_dir.mkdir(parents=True, exist_ok=True)
        LOGGER.info(f"Base dir created/verified: {base_dir}")
    except Exception as e:
        LOGGER.error(f"Failed to create base dir: {e}")
        await edit_message(msg, f"âŒ Failed to create download directory: {e}")
        return
    
    temp_dir = base_dir / f"{user_id}_{repo_name}"
    zip_path = base_dir / f"{repo_name}.zip"
    LOGGER.info(f"Temp dir: {temp_dir}, Zip path: {zip_path}")
    
    try:
        LOGGER.info(f"Checking if temp_dir exists: {temp_dir}")
        if temp_dir.exists():
            LOGGER.info(f"Removing existing temp_dir: {temp_dir}")
            shutil.rmtree(temp_dir)
        
        LOGGER.info(f"Checking if zip exists: {zip_path}")
        if zip_path.exists():
            LOGGER.info(f"Removing existing zip: {zip_path}")
            os.remove(zip_path)
        
        LOGGER.info(f"Creating temp_dir: {temp_dir}")
        temp_dir.mkdir(parents=True, exist_ok=True)
        
        LOGGER.info("Updating message for cloning...")
        await edit_message(msg, f"ðŸ“¥ Cloning repository: <code>{repo_name}</code>...")
        
        clone_cmd = f"git clone --depth 1 '{url}' '{temp_dir}'"
        LOGGER.info(f"Running command: {clone_cmd}")
        stdout, stderr, returncode = await cmd_exec(clone_cmd)
        LOGGER.info(f"Clone result - returncode: {returncode}, stdout: {stdout[:200] if stdout else 'None'}, stderr: {stderr[:200] if stderr else 'None'}")
        
        if returncode != 0:
            error_msg = stderr if stderr else "Unknown error"
            if "Authentication failed" in error_msg or "could not read" in error_msg:
                await edit_message(msg, "âŒ Authentication failed! Check your access token.")
            elif "Repository not found" in error_msg:
                await edit_message(msg, "âŒ Repository not found or you don't have access!")
            else:
                await edit_message(msg, f"âŒ Clone failed:\n<code>{error_msg[:500]}</code>")
            return
        
        git_dir = temp_dir / ".git"
        if git_dir.exists():
            shutil.rmtree(git_dir)
        
        await edit_message(msg, f"ðŸ“¦ Creating archive: <code>{repo_name}.zip</code>...")    
        shutil.make_archive(str(zip_path.with_suffix('')), 'zip', str(temp_dir))
        
        if not zip_path.exists():
            await edit_message(msg, "âŒ Failed to create zip archive!")
            return
    
        file_size = zip_path.stat().st_size
        size_mb = file_size / (1024 * 1024)
        
        await edit_message(msg, f"ðŸ“¤ Uploading <code>{repo_name}.zip</code> ({size_mb:.2f} MB)...")
        await send_file(message, zip_path, caption=f"ðŸ“¦ <b>{repo_name}</b>\nðŸ’¾ Size: {size_mb:.2f} MB")
        await delete_message(msg)
        
    except asyncio.TimeoutError:
        await edit_message(msg, "âŒ Operation timed out! Repository might be too large.")
    except Exception as e:
        LOGGER.error(f"GitHub clone error: {e}")
        await edit_message(msg, f"âŒ Error: <code>{str(e)[:500]}</code>")
    finally:
        try:
            if temp_dir.exists():
                shutil.rmtree(temp_dir)
            if zip_path.exists():
                os.remove(zip_path)
        except Exception as e:
            LOGGER.error(f"Cleanup error: {e}")


bot.add_handler(
    MessageHandler(
        github_clone_handler,
        filters=command("github") & CustomFilters.authorized
    )
)