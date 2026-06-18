"""Telegram alert sender."""
import logging
import requests

logger = logging.getLogger(__name__)


class TelegramAlerter:
    def __init__(self, config):
        self.token = config.TELEGRAM_BOT_TOKEN
        self.chat_id = config.TELEGRAM_CHAT_ID
        self._enabled = bool(self.token and self.chat_id)

    def send(self, message: str):
        if not self._enabled:
            logger.info(f"[ALERT] {message[:120]}")
            return
        try:
            requests.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json={"chat_id": self.chat_id, "text": message, "parse_mode": "HTML"},
                timeout=5,
            )
        except Exception as e:
            logger.error(f"Telegram send: {e}")

    def discover_chat_id(self) -> str | None:
        """Call once from the console to find your chat_id after messaging the bot."""
        try:
            r = requests.get(
                f"https://api.telegram.org/bot{self.token}/getUpdates", timeout=5
            )
            updates = r.json().get("result", [])
            if updates:
                chat_id = str(updates[-1]["message"]["chat"]["id"])
                logger.info(f"Discovered TELEGRAM_CHAT_ID={chat_id}")
                return chat_id
        except Exception as e:
            logger.error(f"discover_chat_id: {e}")
        return None
