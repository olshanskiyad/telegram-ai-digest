from aiogram import Bot

from ai_news_digest.config import settings


class Publisher:
    def __init__(self, bot: Bot) -> None:
        self.bot = bot

    async def send_to_channel(self, text: str) -> None:
        await self.bot.send_message(
            chat_id=settings.PUBLISH_CHAT_ID,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
