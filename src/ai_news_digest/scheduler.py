import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from ai_news_digest.config import settings


logger = logging.getLogger(__name__)


def _split_hhmm(value: str) -> tuple[int, int]:
    hour_str, minute_str = value.strip().split(":")
    return int(hour_str), int(minute_str)


class DigestScheduler:
    def __init__(self, web_collector, digest_builder, publisher, schedule: dict | None = None) -> None:
        self.web_collector = web_collector
        self.digest_builder = digest_builder
        self.publisher = publisher
        # Расписание из базы (кнопка «Расписание» в боте), иначе из .env
        self.schedule = schedule or {}
        self.scheduler = AsyncIOScheduler(timezone=settings.TIMEZONE)

    def configure(self) -> None:
        morning_fetch_hour, morning_fetch_minute = self._time("morning_web_fetch", settings.MORNING_WEB_FETCH)
        morning_digest_hour, morning_digest_minute = self._time("morning_digest_time", settings.MORNING_DIGEST_TIME)
        evening_fetch_hour, evening_fetch_minute = self._time("evening_web_fetch", settings.EVENING_WEB_FETCH)
        evening_digest_hour, evening_digest_minute = self._time("evening_digest_time", settings.EVENING_DIGEST_TIME)

        self.scheduler.add_job(
            self.web_collector.run_once,
            CronTrigger(hour=morning_fetch_hour, minute=morning_fetch_minute, timezone=settings.TIMEZONE),
            id="morning_web_fetch",
            replace_existing=True,
        )
        self.scheduler.add_job(
            self.web_collector.run_once,
            CronTrigger(hour=evening_fetch_hour, minute=evening_fetch_minute, timezone=settings.TIMEZONE),
            id="evening_web_fetch",
            replace_existing=True,
        )
        self.scheduler.add_job(
            self._publish_scheduled_digest,
            CronTrigger(hour=morning_digest_hour, minute=morning_digest_minute, timezone=settings.TIMEZONE),
            kwargs={"slot": "morning"},
            id="morning_digest",
            replace_existing=True,
        )
        self.scheduler.add_job(
            self._publish_scheduled_digest,
            CronTrigger(hour=evening_digest_hour, minute=evening_digest_minute, timezone=settings.TIMEZONE),
            kwargs={"slot": "evening"},
            id="evening_digest",
            replace_existing=True,
        )

    def _time(self, key: str, default: str) -> tuple[int, int]:
        return _split_hhmm(self.schedule.get(key) or default)

    def start(self) -> None:
        self.configure()
        self.scheduler.start()
        logger.info("Scheduler started with timezone %s", settings.TIMEZONE)

    async def _publish_scheduled_digest(self, slot: str) -> None:
        title = "Утренний AI-дайджест" if slot == "morning" else "Вечерний AI-дайджест"
        text, items, _ = await self.digest_builder.build_digest(
            run_type=f"scheduled_{slot}",
            requested_topic=None,
            mark_published=True,
            heading=title,
        )
        if not items:
            logger.info("No stories for the %s digest, nothing published", slot)
            return
        await self.publisher.send_to_channel(text)
