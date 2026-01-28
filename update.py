from dotenv import load_dotenv, dotenv_values
from logging import (
    FileHandler,
    StreamHandler,
    INFO,
    basicConfig,
    error as log_error,
    info as log_info,
    getLogger,
    ERROR,
)
from os import path, environ, remove
from subprocess import run as srun

getLogger("pymongo").setLevel(ERROR)

if path.exists("log.txt"):
    with open("log.txt", "r+") as f:
        f.truncate(0)

basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[FileHandler("log.txt"), StreamHandler()],
    level=INFO,
)

load_dotenv("config.env", override=True)

try:
    if bool(environ.get("_____REMOVE_THIS_LINE_____")):
        log_error("The README.md file there to be read! Exiting now!")
        exit(1)
except:
    pass

BOT_TOKEN = environ.get("BOT_TOKEN", "")
if len(BOT_TOKEN) == 0:
    log_error("BOT_TOKEN variable is missing! Exiting now")
    exit(1)

bot_id = BOT_TOKEN.split(":", 1)[0]

UPSTREAM_REPO = environ.get("UPSTREAM_REPO", "")
if len(UPSTREAM_REPO) == 0:
    UPSTREAM_REPO = None

UPSTREAM_BRANCH = environ.get("UPSTREAM_BRANCH", "")
if len(UPSTREAM_BRANCH) == 0:
    UPSTREAM_BRANCH = "master"

if UPSTREAM_REPO is not None:
    if path.exists(".git"):
        srun(["rm", "-rf", ".git"])

    update = srun(
        [
            f"git init -q && "
            f"git config --global user.email e.soethuya322@gmail.com && "
            f"git config --global user.name Antidaze && "
            f"git add . && "
            f"git commit -sm update -q && "
            f"git remote add origin {UPSTREAM_REPO} && "
            f"git fetch origin -q && "
            f"git reset --hard origin/{UPSTREAM_BRANCH} -q"
        ],
        shell=True,
    )

    if update.returncode == 0:
        log_info("Successfully updated with latest commit from UPSTREAM_REPO")
    else:
        log_error("Something went wrong while updating, check UPSTREAM_REPO if valid or not!")
