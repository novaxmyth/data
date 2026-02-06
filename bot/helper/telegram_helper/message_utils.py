from asyncio import sleep
from pyrogram.errors import FloodWait
from re import match as re_match
from time import time
from bot import config_dict, LOGGER, bot


async def send_message(message, text, buttons=None, block=True):
    try:
        return await message.reply(
            text=text,
            quote=True,
            disable_notification=True,
            reply_markup=buttons,
        )
    except FloodWait as f:
        LOGGER.warning(str(f))
        if not block:
            return str(f)
        await sleep(f.value * 1.2)
        return await send_message(message, text, buttons)
    except Exception as e:
        LOGGER.error(str(e))
        return str(e)


async def edit_message(message, text, buttons=None, block=True):
    try:
        return await message.edit(
            text=text,
            reply_markup=buttons,
        )
    except FloodWait as f:
        LOGGER.warning(str(f))
        if not block:
            return str(f)
        await sleep(f.value * 1.2)
        return await edit_message(message, text, buttons)
    except Exception as e:
        LOGGER.error(str(e))
        return str(e)


async def send_file(message, file, caption=""):
    try:
        return await message.reply_document(
            document=file, quote=True, caption=caption, disable_notification=True
        )
    except FloodWait as f:
        LOGGER.warning(str(f))
        await sleep(f.value * 1.2)
        return await send_file(message, file, caption)
    except Exception as e:
        LOGGER.error(str(e))
        return str(e)


async def send_photo(message, photo, caption="", buttons=None):
    try:
        return await message.reply_photo(
            photo=photo,
            quote=True,
            caption=caption,
            disable_notification=True,
            reply_markup=buttons,
        )
    except FloodWait as f:
        LOGGER.warning(str(f))
        await sleep(f.value * 1.2)
        return await send_photo(message, photo, caption, buttons)
    except Exception as e:
        LOGGER.error(str(e))
        return str(e)


async def delete_message(message):
    try:
        await message.delete()
    except Exception as e:
        LOGGER.error(str(e))


async def auto_delete_message(cmd_message=None, bot_message=None):
    await sleep(60)
    if cmd_message is not None:
        await deleteMessage(cmd_message)
    if bot_message is not None:
        await deleteMessage(bot_message)
