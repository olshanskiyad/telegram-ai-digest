import asyncio

from ai_news_digest.scheduler import DigestScheduler


class FakeBuilder:
    def __init__(self, items):
        self.items = items

    async def build_digest(self, **kwargs):
        return "text", self.items, 1


class FakePublisher:
    def __init__(self):
        self.sent = []

    async def send_to_channel(self, text):
        self.sent.append(text)


def test_schedule_from_database_wins_over_env():
    scheduler = DigestScheduler(None, None, None, schedule={"morning_digest_time": "07:30"})
    assert scheduler._time("morning_digest_time", "09:00") == (7, 30)
    assert scheduler._time("evening_digest_time", "19:00") == (19, 0)


def test_empty_digest_is_not_published():
    publisher = FakePublisher()
    scheduler = DigestScheduler(None, FakeBuilder([]), publisher)
    asyncio.run(scheduler._publish_scheduled_digest("morning"))
    assert publisher.sent == []


def test_digest_with_stories_is_published():
    publisher = FakePublisher()
    scheduler = DigestScheduler(None, FakeBuilder(["story"]), publisher)
    asyncio.run(scheduler._publish_scheduled_digest("evening"))
    assert publisher.sent == ["text"]
