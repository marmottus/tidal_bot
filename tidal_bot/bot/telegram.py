import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError
from telegram import (
    Bot,
    Update,
)
from telegram.constants import MessageLimit
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

logger = logging.getLogger("bot")

SPECIAL_CHARS = [
    "\\",
    "_",
    "*",
    "[",
    "]",
    "(",
    ")",
    "~",
    "`",
    ">",
    "<",
    "&",
    "#",
    "+",
    "-",
    "=",
    "|",
    "{",
    "}",
    ".",
    "!",
]


def markdown_escape(name: str) -> str:
    for char in SPECIAL_CHARS:
        name = name.replace(char, f"\\{char}")
    return name


class TelegramConfig(BaseModel):
    bot_token: str
    chat_id: int
    allowed_users: list[int]


class TelegramBot:
    def __init__(
        self,
        sync_callback: Callable[["TelegramBot"], Awaitable[None]],
        list_callback: Callable[["TelegramBot"], Awaitable[None]],
    ) -> None:
        config_file = Path(__file__).parent.parent.parent / "config" / "telegram.yaml"
        if not config_file.exists():
            logger.error("Telegram config file %s does not exist", config_file)
            raise FileNotFoundError(f"Telegram config file {config_file} not found")

        with config_file.open() as f:
            config = yaml.safe_load(f)
            try:
                telegram_config: dict[str, Any] = config["telegram"]
                self._config = TelegramConfig.model_validate(telegram_config)
            except ValidationError as e:
                logger.error("Telegram config file %s is invalid: %s", config_file, e)
                raise
            except KeyError as e:
                logger.error(
                    "Telegram config file %s is missing required fields: %s",
                    config_file,
                    e,
                )
                raise

        self._sync_callback = sync_callback
        self._list_callback = list_callback

        self._bot = Bot(token=self._config.bot_token)
        self._app = Application.builder().bot(self._bot).build()

        self._app.add_handler(CommandHandler("sync", self._sync_command))
        self._app.add_handler(CommandHandler("list", self._list_command))
        self._app.add_handler(CommandHandler("good", self._good_command))

        self._polling_task: asyncio.Task[asyncio.Queue[object]] | None = None

    async def start(self) -> None:
        logger.info("Start Telegram Bot")

        if self._polling_task is not None and not self._polling_task.done():
            logger.debug("Application is already running")
            return

        await self._app.initialize()
        await self._app.start()

        updater = self._app.updater

        if updater is None:
            logger.error("updater is not initialized")
            return

        loop = asyncio.get_running_loop()
        self._polling_task = loop.create_task(updater.start_polling())

        logger.info("Telegram bot started")

    async def stop(self) -> None:
        updater = self._app.updater

        logger.info("Stop Telegram Bot")

        if updater is None:
            logger.debug("updater is not initialized")
        else:
            await updater.stop()

        if self._polling_task is not None:
            _ = await asyncio.gather(self._polling_task, return_exceptions=True)
            self._polling_task = None

        logger.info("Telegram Bot stopped")

    async def send_message(
        self,
        message: str,
        message_thread_id: int | None = None,
    ) -> None:
        lines_to_send = message.split("\n")
        while lines_to_send:
            to_send = ""
            i = 0
            for line in lines_to_send:
                if len(to_send) + len(line) + 1 > MessageLimit.MAX_TEXT_LENGTH:
                    break
                to_send += line + "\n"
                i += 1

            lines_to_send = lines_to_send[i:]
            to_send = to_send[:-1]  # remove last newline

            try:
                await self._bot.send_message(
                    chat_id=self._config.chat_id,
                    message_thread_id=message_thread_id,
                    text=to_send,
                    parse_mode="MarkdownV2",
                )
            except TelegramError as e:
                logger.error("Failed to send Telegram message: %s", e)

    def _is_comand_allowed(self, update: Update) -> bool:
        message = update.message
        if message is None or message.text is None:
            return False

        from_user = message.from_user
        if from_user is None:
            return False

        if from_user.id not in self._config.allowed_users:
            return False

        chat = message.chat

        if chat.id != self._config.chat_id:
            return False

        return True

    async def _list_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not self._is_comand_allowed(update):
            return

        await self._list_callback(self)

    async def _good_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not self._is_comand_allowed(update):
            return

        message = update.message
        if message is None or message.text is None:
            return

        from_user = message.from_user
        if from_user is None:
            return

        name = from_user.full_name

        answer = random.choice(  # noqa: S311
            (
                f"Thank you {name}!",
                f"Dziękuję {name}!",
                f"Merci {name}!",
                f"Grazie {name}!",
                f"Danke schön {name}!",
                f"ありがとう {name}!",
                "😊😊😊",
                "XD",
            )
        )

        await message.reply_text(answer)

    async def _sync_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not self._is_comand_allowed(update):
            return

        await self._sync_callback(self)
