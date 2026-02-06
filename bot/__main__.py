from aiofiles import open as aiopen
from aiofiles.os import path as aiopath, remove
from asyncio import gather, create_subprocess_exec, sleep
from os import execl as osexecl
from psutil import disk_usage, cpu_percent, swap_memory, cpu_count, virtual_memory, net_io_counters, boot_time
from pyrogram.filters import command, regex
from pyrogram.handlers import MessageHandler, CallbackQueryHandler
from signal import signal, SIGINT
from sys import executable
from time import time

from bot import bot, bot_loop, bot_start_time, LOGGER, DATABASE_URL
from bot.helper.ext_utils.bot_utils import cmd_exec, sync_to_async
from bot.helper.ext_utils.files_utils import clean_all, exit_clean_up, get_readable_file_size, get_readable_time
from bot.helper.telegram_helper.bot_commands import BotCommands
from bot.helper.telegram_helper.button_build import ButtonMaker
from bot.helper.telegram_helper.filters import CustomFilters
from bot.helper.telegram_helper.message_utils import send_message, edit_message, send_file, delete_message
from bot.modules import *

async def stats(_, message):
    if await aiopath.exists(".git"):
        last_commit = await cmd_exec(
            "git log -1 --date=short --pretty=format:'%cd <b>From</b> %cr'", True
        )
        last_commit = last_commit[0]
    else:
        last_commit = "No UPSTREAM_REPO"
    total, used, free, disk = disk_usage("/")
    swap = swap_memory()
    memory = virtual_memory()
    stats = (
        f"<b>Commit Date:</b> {last_commit}\n\n"
        f"<b>Bot Uptime:</b> {get_readable_time(time() - bot_start_time)}\n"
        f"<b>OS Uptime:</b> {get_readable_time(time() - boot_time())}\n\n"
        f"<b>Total Disk Space:</b> {get_readable_file_size(total)}\n"
        f"<b>Used:</b> {get_readable_file_size(used)} | <b>Free:</b> {get_readable_file_size(free)}\n\n"
        f"<b>Upload:</b> {get_readable_file_size(net_io_counters().bytes_sent)}\n"
        f"<b>Download:</b> {get_readable_file_size(net_io_counters().bytes_recv)}\n\n"
        f"<b>CPU:</b> {cpu_percent(interval=0.5)}%\n"
        f"<b>RAM:</b> {memory.percent}%\n"
        f"<b>DISK:</b> {disk}%\n\n"
        f"<b>Physical Cores:</b> {cpu_count(logical=False)}\n"
        f"<b>Total Cores:</b> {cpu_count(logical=True)}\n\n"
        f"<b>SWAP:</b> {get_readable_file_size(swap.total)} | <b>Used:</b> {swap.percent}%\n"
        f"<b>Memory Total:</b> {get_readable_file_size(memory.total)}\n"
        f"<b>Memory Free:</b> {get_readable_file_size(memory.available)}\n"
        f"<b>Memory Used:</b> {get_readable_file_size(memory.used)}\n"
    )
    await send_message(message, stats)


async def start(client, message):
    if await CustomFilters.authorized(client, message):
        start_string = f"""
This is a personal bot intended for multifunctional use.
"""
        await send_message(message, start_string)
    else:
        await send_message(
            message,
            "This is an anime bot.Source code is https://github.com/lostb053/anibot"
        )


async def restart(_, message):
    buttons = ButtonMaker()
    buttons.data_button("Yes", f"restart {message.from_user.id} confirm")
    buttons.data_button("No", f"restart {message.from_user.id} cancel")
    await send_message(message, "Do you want to restart the bot?", buttons.build_menu(2))


async def confirm_restart(_, query):
    await query.answer()
    data = query.data.split()
    if len(data) < 3:
        await query.answer("Invalid restart action!", show_alert=True)
        return
    message = query.message
    user_id = int(data[1])
    action = data[2]
    
    if user_id != query.from_user.id:
        await query.answer("You are not authorized to use this action!", show_alert=True)
        return
      
    if action == "confirm":
        reply_to = message.reply_to_message or message
        restart_message = await send_message(reply_to, "Restarting...")
        if isinstance(restart_message, str):
            LOGGER.error(f"Failed to send restart message: {restart_message}")
            restart_message = await message.reply("Restarting...")
        await delete_message(message)
        await clean_all()
        try:
            proc1 = await create_subprocess_exec("pkill", "-9", "-f", "gunicorn|ffmpeg|rclone")
            await proc1.wait()
        except Exception as e:
            LOGGER.warning(f"Failed to kill processes: {e}")

        try:
            if await aiopath.exists("update.py"):
                proc2 = await create_subprocess_exec("python3", "update.py")
                await proc2.wait()
        except Exception as e:
            LOGGER.warning(f"Failed to run update.py: {e}")

        if hasattr(restart_message, "chat") and hasattr(restart_message, "id"):
            async with aiopen(".restartmsg", "w") as f:
                await f.write(f"{restart_message.chat.id}\n{restart_message.id}\n")
        else:
            LOGGER.error("Restart message metadata could not be persisted")
        osexecl(executable, executable, "-m", "bot")
    else:
        await delete_message(message.reply_to_message)
        await delete_message(message)


async def ping(_, message):
    start_time = int(round(time() * 1000))
    reply = await send_message(message, "Starting Ping")
    end_time = int(round(time() * 1000))
    await edit_message(reply, f"{end_time - start_time} ms")


async def log(_, message):
    await send_file(message, "log.txt")


help_string = f"""
NOTE: Try each command without any argument to see more detalis.
/{BotCommands.StatsCommand}: Show stats of the machine where the bot is hosted in.
/{BotCommands.PingCommand}: Check how long it takes to Ping the Bot (Only Owner & Sudo).
/{BotCommands.AuthorizeCommand}: Authorize a chat or a user to use the bot (Only Owner & Sudo).
/{BotCommands.UnAuthorizeCommand}: Unauthorize a chat or a user to use the bot (Only Owner & Sudo).
/{BotCommands.AddSudoCommand}: Add sudo user (Only Owner).
/{BotCommands.RmSudoCommand}: Remove sudo users (Only Owner).
/{BotCommands.RestartCommand}: Restart and update the bot (Only Owner & Sudo).
/{BotCommands.LogCommand}: Get a log file of the bot. Handy for getting crash reports (Only Owner & Sudo).
/{BotCommands.ShellCommand}: Run shell commands (Only Owner).
/{BotCommands.AExecCommand}: Exec async functions (Only Owner).
/{BotCommands.ExecCommand}: Exec sync functions (Only Owner).
/{BotCommands.ClearLocalsCommand}: Clear {BotCommands.AExecCommand} or {BotCommands.ExecCommand} locals (Only Owner).
"""


async def bot_help(_, message):
    await send_message(message, help_string)


async def restart_notification():
    chat_id, msg_id = 0, 0
    if await aiopath.isfile(".restartmsg"):
        try:
            async with aiopen(".restartmsg", "r") as f:
                content = await f.read()
            lines = [line.strip() for line in content.splitlines() if line.strip()]
            if len(lines) >= 2:
                chat_id, msg_id = int(lines[0]), int(lines[1])
            else:
                LOGGER.error("Invalid .restartmsg format: expected chat_id and msg_id")
                await remove(".restartmsg")
        except Exception as e:
            LOGGER.error(f"Failed to read .restartmsg: {e}")
            await remove(".restartmsg")

    if chat_id and msg_id and await aiopath.isfile(".restartmsg"):
        try:
            await bot.edit_message_text(
                chat_id=chat_id, message_id=msg_id, text="Restarted Successfully!"
            )
        except Exception as e:
            LOGGER.error(f"Failed to edit restart message: {e}")
    if await aiopath.isfile(".restartmsg"):
        await remove(".restartmsg")


async def main():
    await gather(
        restart_notification(),
    )

    bot.add_handler(MessageHandler(start, filters=command(BotCommands.StartCommand)))
    bot.add_handler(
        MessageHandler(
            log, filters=command(BotCommands.LogCommand) & CustomFilters.sudo
        )
    )
    bot.add_handler(
        MessageHandler(
            restart, filters=command(BotCommands.RestartCommand) & CustomFilters.sudo
        )
    )
    bot.add_handler(
        CallbackQueryHandler(
            confirm_restart, filters=regex("^restart")
        )
    )
    bot.add_handler(
        MessageHandler(
            ping, filters=command(BotCommands.PingCommand) & CustomFilters.authorized
        )
    )
    bot.add_handler(
        MessageHandler(
            bot_help,
            filters=command(BotCommands.HelpCommand) & CustomFilters.authorized,
        )
    )
    bot.add_handler(
        MessageHandler(
            stats, filters=command(BotCommands.StatsCommand) & CustomFilters.authorized
        )
    )
    LOGGER.info("Bot Started!")
    signal(SIGINT, exit_clean_up)


bot_loop.run_until_complete(main())
bot_loop.run_forever()
