from PIL import Image
from aiofiles.os import remove, path as aiopath, makedirs
from asyncio import create_subprocess_exec, gather, wait_for
from asyncio.subprocess import PIPE
from os import path as ospath, cpu_count
from re import search as re_search
from time import time
from aioshutil import rmtree

from bot import LOGGER, subprocess_lock
from bot.helper.ext_utils.bot_utils import cmd_exec
from bot.helper.ext_utils.bot_utils import sync_to_async


async def createThumb(msg, _id=""):
    if not _id:
        _id = msg.id
    path = "Thumbnails/"
    await makedirs(path, exist_ok=True)
    photo_dir = await msg.download()
    des_dir = f"{path}{_id}.jpg"
    await sync_to_async(Image.open(photo_dir).convert("RGB").save, des_dir, "JPEG")
    await remove(photo_dir)
    return des_dir


async def is_multi_streams(path):
    try:
        result = await cmd_exec(
            [
                "ffprobe",
                "-hide_banner",
                "-loglevel",
                "error",
                "-print_format",
                "json",
                "-show_streams",
                path,
            ]
        )
    except Exception as e:
        LOGGER.error(f"Get Video Streams: {e}. Mostly File not found! - File: {path}")
        return False
    if result[0] and result[2] == 0:
        fields = eval(result[0]).get("streams")
        if fields is None:
            LOGGER.error(f"get_video_streams: {result}")
            return False
        videos = 0
        audios = 0
        for stream in fields:
            if stream.get("codec_type") == "video":
                videos += 1
            elif stream.get("codec_type") == "audio":
                audios += 1
        return videos > 1 or audios > 1
    return False


async def get_media_info(path):
    try:
        result = await cmd_exec(
            [
                "ffprobe",
                "-hide_banner",
                "-loglevel",
                "error",
                "-print_format",
                "json",
                "-show_format",
                path,
            ]
        )
    except Exception as e:
        LOGGER.error(f"Get Media Info: {e}. Mostly File not found! - File: {path}")
        return 0, None, None
    if result[0] and result[2] == 0:
        fields = eval(result[0]).get("format")
        if fields is None:
            LOGGER.error(f"get_media_info: {result}")
            return 0, None, None
        duration = round(float(fields.get("duration", 0)))
        tags = fields.get("tags", {})
        artist = tags.get("artist") or tags.get("ARTIST") or tags.get("Artist")
        title = tags.get("title") or tags.get("TITLE") or tags.get("Title")
        return duration, artist, title
    return 0, None, None


async def take_ss(video_file, ss_nb) -> bool:
    ss_nb = min(ss_nb, 10)
    duration = (await get_media_info(video_file))[0]
    if duration != 0:
        dirpath, name = video_file.rsplit("/", 1)
        name, _ = ospath.splitext(name)
        dirpath = f"{dirpath}/{name}_mltbss/"
        await makedirs(dirpath, exist_ok=True)
        interval = duration // (ss_nb + 1)
        cap_time = interval
        cmds = []
        for i in range(ss_nb):
            output = f"{dirpath}SS.{name}_{i:02}.png"
            cmd = [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                f"{cap_time}",
                "-i",
                video_file,
                "-q:v",
                "1",
                "-frames:v",
                "1",
                output,
            ]
            cap_time += interval
            cmds.append(cmd_exec(cmd))
        try:
            resutls = await wait_for(gather(*cmds), timeout=60)
            if resutls[0][2] != 0:
                LOGGER.error(
                    f"Error while creating sreenshots from video. Path: {video_file}. stderr: {resutls[0][1]}"
                )
                await rmtree(dirpath, ignore_errors=True)
                return False
        except:
            LOGGER.error(
                f"Error while creating sreenshots from video. Path: {video_file}. Error: Timeout some issues with ffmpeg with specific arch!"
            )
            await rmtree(dirpath, ignore_errors=True)
            return False
        return dirpath
    else:
        LOGGER.error("take_ss: Can't get the duration of video")
        await rmtree(dirpath, ignore_errors=True)
        return False


async def get_audio_thumb(audio_file):
    des_dir = "Thumbnails/"
    await makedirs(des_dir, exist_ok=True)
    des_dir = f"Thumbnails/{time()}.jpg"
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        audio_file,
        "-an",
        "-vcodec",
        "copy",
        des_dir,
    ]
    _, err, code = await cmd_exec(cmd)
    if code != 0 or not await aiopath.exists(des_dir):
        LOGGER.error(
            f"Error while extracting thumbnail from audio. Name: {audio_file} stderr: {err}"
        )
        return None
    return des_dir


async def create_thumbnail(video_file, duration):
    des_dir = "Thumbnails"
    await makedirs(des_dir, exist_ok=True)
    des_dir = ospath.join(des_dir, f"{time()}.jpg")
    if duration is None:
        duration = (await get_media_info(video_file))[0]
    if duration == 0:
        duration = 3
    duration = duration // 2
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{duration}",
        "-i",
        video_file,
        "-vf",
        "thumbnail",
        "-frames:v",
        "1",
        des_dir,
    ]
    try:
        _, err, code = await wait_for(cmd_exec(cmd), timeout=60)
        if code != 0 or not await aiopath.exists(des_dir):
            LOGGER.error(
                f"Error while extracting thumbnail from video. Name: {video_file} stderr: {err}"
            )
            return None
    except:
        LOGGER.error(
            f"Error while extracting thumbnail from video. Name: {video_file}. Error: Timeout some issues with ffmpeg with specific arch!"
        )
        return None
    return des_dir


async def createSampleVideo(listener, video_file, sample_duration, part_duration):
    filter_complex = ""
    dir, name = video_file.rsplit("/", 1)
    output_file = f"{dir}/SAMPLE.{name}"
    segments = [(0, part_duration)]
    duration = (await get_media_info(video_file))[0]
    remaining_duration = duration - (part_duration * 2)
    parts = (sample_duration - (part_duration * 2)) // part_duration
    time_interval = remaining_duration // parts
    next_segment = time_interval
    for _ in range(parts):
        segments.append((next_segment, next_segment + part_duration))
        next_segment += time_interval
    segments.append((duration - part_duration, duration))

    for i, (start, end) in enumerate(segments):
        filter_complex += (
            f"[0:v]trim=start={start}:end={end},setpts=PTS-STARTPTS[v{i}]; "
        )
        filter_complex += (
            f"[0:a]atrim=start={start}:end={end},asetpts=PTS-STARTPTS[a{i}]; "
        )

    for i in range(len(segments)):
        filter_complex += f"[v{i}][a{i}]"

    filter_complex += f"concat=n={len(segments)}:v=1:a=1[vout][aout]"

    cmd = [
        "ffmpeg",
        "-i",
        video_file,
        "-filter_complex",
        filter_complex,
        "-map",
        "[vout]",
        "-map",
        "[aout]",
        "-c:v",
        "libx264",
        "-c:a",
        "aac",
        "-threads",
        f"{cpu_count() // 2}",
        output_file,
    ]

    if listener.isCancelled:
        return False
    listener.suproc = await create_subprocess_exec(*cmd, stderr=PIPE)
    _, stderr = await listener.suproc.communicate()
    if listener.isCancelled:
        return False
    code = listener.suproc.returncode
    if code == -9:
        listener.isCancelled = True
        return False
    elif code == 0:
        return output_file
    else:
        try:
            stderr = stderr.decode().strip()
        except:
            stderr = "Unable to decode the error!"
        LOGGER.error(
            f"{stderr}. Something went wrong while creating sample video, mostly file is corrupted. Path: {video_file}"
        )
        if await aiopath.exists(output_file):
            await remove(output_file)
        return False