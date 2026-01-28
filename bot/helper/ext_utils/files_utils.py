from aiofiles.os import remove, path as aiopath, makedirs as aiomakedirs, listdir, rmdir
from aioshutil import rmtree as aiormtree
from asyncio import create_subprocess_exec
from magic import Magic
from os import walk, path as ospath, makedirs
from re import split as re_split, I, search as re_search, escape
from shutil import rmtree
from subprocess import run as srun
from sys import exit as sexit

from bot import LOGGER, close_db, DOWNLOAD_DIR
from bot.helper.ext_utils.bot_utils import sync_to_async, cmd_exec

SIZE_UNITS = ["B", "KB", "MB", "GB", "TB", "PB"]


async def clean_all():
    LOGGER.info("Cleaning Download Directory...")
    await (await create_subprocess_exec("rm", "-rf", DOWNLOAD_DIR)).wait()
    await aiomakedirs(DOWNLOAD_DIR, exist_ok=True)


def exit_clean_up(signal, frame):
    try:
        LOGGER.info("Please wait a while cleaning up")
        close_db()
        clean_all()
        srun(["pkill", "-9", "-f", "ffmpeg"])
        sexit(0)
    except KeyboardInterrupt:
        LOGGER.warning("Force Exiting before the cleanup finishes!")
        sexit(1)


async def get_path_size(path):
    if await aiopath.isfile(path):
        return await aiopath.getsize(path)
    total_size = 0
    for root, _, files in await sync_to_async(walk, path):
        for f in files:
            abs_path = ospath.join(root, f)
            total_size += await aiopath.getsize(abs_path)
    return total_size


def get_mime_type(file_path):
    mime = Magic(mime=True)
    mime_type = mime.from_file(file_path)
    mime_type = mime_type or "text/plain"
    return mime_type


def get_readable_file_size(size_in_bytes: int):
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


def get_readable_time(seconds: int):
    periods = [("d", 86400), ("h", 3600), ("m", 60), ("s", 1)]
    result = ""
    for period_name, period_seconds in periods:
        if seconds >= period_seconds:
            period_value, seconds = divmod(seconds, period_seconds)
            result += f"{int(period_value)}{period_name}"
    return result
