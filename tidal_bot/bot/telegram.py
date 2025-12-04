import logging
from pathlib import Path

import yaml
from telegram import Bot
from telegram.constants import MessageLimit
from telegram.error import TelegramError

logger = logging.getLogger("bot")


class TelegramBot:
    def __init__(self) -> None:
        config_file = Path(__file__).parent.parent.parent / "config" / "telegram.yaml"
        if not config_file.exists():
            logger.error("Telegram config file %s does not exist", config_file)
            raise FileNotFoundError(f"Telegram config file {config_file} not found")

        with config_file.open() as f:
            config = yaml.safe_load(f)
            try:
                telegram_config = config["telegram"]
                bot_token = telegram_config["bot_token"]
                self._chat_id = telegram_config["chat_id"]
            except KeyError as e:
                logger.error(
                    "Telegram config file %s is missing required fields: %s",
                    config_file,
                    e,
                )
                raise

        self._bot = Bot(token=bot_token)

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
                    chat_id=self._chat_id,
                    message_thread_id=message_thread_id,
                    text=to_send,
                    parse_mode="MarkdownV2",
                )
            except TelegramError as e:
                logger.error("Failed to send Telegram message: %s", e)
