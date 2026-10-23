import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, MenuButtonCommands

from ai_news_digest import admin_bot
from ai_news_digest.collectors.telegram_collector import TelegramCollector
from ai_news_digest.collectors.web_collector import WebCollector
from ai_news_digest.config import settings
from ai_news_digest.database import Database
from ai_news_digest.deepseek_client import DeepSeekClient
from ai_news_digest.logging_setup import setup_logging
from ai_news_digest.pipeline.chat_summary import ChatSummarizer
from ai_news_digest.pipeline.digest_builder import DigestBuilder
from ai_news_digest.pipeline.unread_channels_summary import UnreadChannelsSummarizer
from ai_news_digest.publisher import Publisher
from ai_news_digest.scheduler import DigestScheduler


logger = logging.getLogger(__name__)


async def _configure_bot(bot: Bot) -> None:
    await bot.delete_webhook(drop_pending_updates=False)
    await bot.set_my_commands(
        [
            BotCommand(command='start', description='Открыть меню'),
        ]
    )
    await bot.set_chat_menu_button(menu_button=MenuButtonCommands())


async def main() -> None:
    setup_logging()

    db = Database(settings.DB_PATH)
    await db.init_db()

    bot = Bot(token=settings.BOT_TOKEN.get_secret_value())
    await _configure_bot(bot)
    dp = Dispatcher(storage=MemoryStorage())

    llm_client = DeepSeekClient()
    telegram_collector = TelegramCollector(db)
    web_collector = WebCollector(db)
    digest_builder = DigestBuilder(db, llm_client=llm_client)
    chat_summarizer = ChatSummarizer(llm_client=llm_client)
    unread_channels_summarizer = UnreadChannelsSummarizer(llm_client=llm_client)
    publisher = Publisher(bot)

    admin_bot.db = db
    admin_bot.telegram_collector = telegram_collector
    admin_bot.web_collector = web_collector
    admin_bot.digest_builder = digest_builder
    admin_bot.chat_summarizer = chat_summarizer
    admin_bot.unread_channels_summarizer = unread_channels_summarizer
    admin_bot.publisher = publisher

    dp.include_router(admin_bot.router)

    scheduler = DigestScheduler(
        web_collector=web_collector,
        digest_builder=digest_builder,
        publisher=publisher,
        schedule=dict(await db.get_settings()),
    )
    scheduler.start()

    logger.info('Starting AI News Digest service')
    await asyncio.gather(
        dp.start_polling(bot),
        telegram_collector.start(),
    )


if __name__ == '__main__':
    asyncio.run(main())
