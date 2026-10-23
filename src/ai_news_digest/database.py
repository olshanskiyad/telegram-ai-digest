import json
import logging
from pathlib import Path
from typing import Any, Optional

import aiosqlite

from ai_news_digest.config import settings
from ai_news_digest.models import RawItemPayload


logger = logging.getLogger(__name__)


DEFAULT_DIGEST_PROMPT = """Ты - ИТ-журналист с острым языком и эксперт по AI. Пишешь для предпринимателя, который ценит время и конкретику.

Стиль:
- никакой воды и канцелярита
- избегай слов: «революционный», «уникальный», «инновационный», «в современном мире»
- прямой порядок слов, минимум тяжёлых оборотов
- тон живой, как разговор с коллегой за кофе
- уместны лёгкий скепсис или энтузиазм, но без пафоса

Формат дайджеста:
- сохраняй блоки «Новости» и «Полезное»
- каждый пункт должен состоять из короткого заголовка и ровно одного абзаца
- в абзаце кратко ответь на два вопроса: что случилось и почему это важно
- 2-3 коротких предложения на пункт
- без хэштегов, без канцелярита, без второго отдельного абзаца
- ссылки оформляются отдельно словом «Источник»
"""


DEFAULT_WEB_SOURCES = [
    {"title": "OpenAI Blog", "feed_url": "https://openai.com/blog/rss.xml", "source_type": "official", "weight": 1.3},
    {"title": "Anthropic News", "feed_url": "https://www.anthropic.com/news", "source_type": "official", "weight": 1.2},
    {"title": "Google AI", "feed_url": "https://blog.google/technology/ai/rss/", "source_type": "official", "weight": 1.2},
    {"title": "Google DeepMind", "feed_url": "https://deepmind.google/discover/blog/", "source_type": "official", "weight": 1.2},
    {"title": "Microsoft AI Blog", "feed_url": "https://news.microsoft.com/source/topics/ai/", "source_type": "official", "weight": 1.2},
    {"title": "AWS Machine Learning Blog", "feed_url": "https://aws.amazon.com/blogs/machine-learning/feed/", "source_type": "official", "weight": 1.1},
    {"title": "Google Cloud AI/ML", "feed_url": "https://cloud.google.com/blog/topics/ai-machine-learning", "source_type": "official", "weight": 1.1},
    {"title": "Hugging Face Blog", "feed_url": "https://huggingface.co/blog/feed.xml", "source_type": "tools", "weight": 1.1},
    {"title": "NVIDIA Newsroom", "feed_url": "https://nvidianews.nvidia.com/rss", "source_type": "official", "weight": 1.0},
    {"title": "The New Stack AI", "feed_url": "https://thenewstack.io/category/ai/feed/", "source_type": "news", "weight": 1.0},
    {"title": "VentureBeat AI", "feed_url": "https://venturebeat.com/category/ai/feed/", "source_type": "news", "weight": 1.0},
    {"title": "TechCrunch AI", "feed_url": "https://techcrunch.com/category/artificial-intelligence/feed/", "source_type": "news", "weight": 0.9},
    {"title": "The Verge AI", "feed_url": "https://www.theverge.com/rss/artificial-intelligence/index.xml", "source_type": "news", "weight": 0.8},
]


class Database:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    async def init_db(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA foreign_keys=ON")

            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    timezone TEXT NOT NULL,
                    morning_web_fetch TEXT NOT NULL,
                    morning_digest_time TEXT NOT NULL,
                    evening_web_fetch TEXT NOT NULL,
                    evening_digest_time TEXT NOT NULL,
                    default_news_count INTEGER NOT NULL,
                    default_useful_count INTEGER NOT NULL,
                    dedup_window_days INTEGER NOT NULL,
                    max_per_source INTEGER NOT NULL,
                    relaxed_max_per_source INTEGER NOT NULL,
                    digest_style_prompt TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS prompt_templates (
                    template_key TEXT PRIMARY KEY,
                    prompt_text TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS tg_sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    username TEXT,
                    invite_link TEXT,
                    source_type TEXT NOT NULL DEFAULT 'channel',
                    is_active INTEGER NOT NULL DEFAULT 1,
                    last_seen_message_id INTEGER,
                    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS web_sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    feed_url TEXT NOT NULL UNIQUE,
                    source_type TEXT NOT NULL DEFAULT 'news',
                    weight REAL NOT NULL DEFAULT 1.0,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    last_checked_at TIMESTAMP,
                    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS raw_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_kind TEXT NOT NULL,
                    source_id INTEGER NOT NULL,
                    external_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    text TEXT,
                    url TEXT,
                    published_at TEXT,
                    fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    raw_hash TEXT NOT NULL,
                    language TEXT,
                    extra_json TEXT,
                    UNIQUE(source_kind, source_id, external_id)
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS story_clusters (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    cluster_key TEXT NOT NULL UNIQUE,
                    canonical_title TEXT NOT NULL,
                    summary_candidate TEXT,
                    primary_url TEXT,
                    primary_source_kind TEXT,
                    primary_source_id INTEGER,
                    topic_tags TEXT,
                    block_type TEXT,
                    novelty_score REAL DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS cluster_items (
                    cluster_id INTEGER NOT NULL,
                    raw_item_id INTEGER NOT NULL,
                    PRIMARY KEY (cluster_id, raw_item_id),
                    FOREIGN KEY (cluster_id) REFERENCES story_clusters(id) ON DELETE CASCADE,
                    FOREIGN KEY (raw_item_id) REFERENCES raw_items(id) ON DELETE CASCADE
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS digest_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_type TEXT NOT NULL,
                    requested_topic TEXT,
                    window_start TEXT,
                    window_end TEXT,
                    status TEXT NOT NULL DEFAULT 'queued',
                    result_message TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    finished_at TIMESTAMP
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS digest_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    digest_run_id INTEGER NOT NULL,
                    cluster_id INTEGER,
                    section_name TEXT NOT NULL,
                    rank_order INTEGER NOT NULL,
                    final_title TEXT NOT NULL,
                    final_body TEXT NOT NULL,
                    final_url TEXT,
                    source_label TEXT,
                    FOREIGN KEY (digest_run_id) REFERENCES digest_runs(id) ON DELETE CASCADE
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS published_stories (
                    cluster_key TEXT PRIMARY KEY,
                    first_published_at TEXT NOT NULL,
                    last_published_at TEXT NOT NULL,
                    last_digest_run_id INTEGER
                )
                """
            )

            await db.execute(
                """
                INSERT OR IGNORE INTO settings (
                    id,
                    timezone,
                    morning_web_fetch,
                    morning_digest_time,
                    evening_web_fetch,
                    evening_digest_time,
                    default_news_count,
                    default_useful_count,
                    dedup_window_days,
                    max_per_source,
                    relaxed_max_per_source,
                    digest_style_prompt
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    1,
                    settings.TIMEZONE,
                    settings.MORNING_WEB_FETCH,
                    settings.MORNING_DIGEST_TIME,
                    settings.EVENING_WEB_FETCH,
                    settings.EVENING_DIGEST_TIME,
                    settings.DEFAULT_NEWS_COUNT,
                    settings.DEFAULT_USEFUL_COUNT,
                    settings.DEDUP_WINDOW_DAYS,
                    settings.MAX_PER_SOURCE,
                    settings.RELAXED_MAX_PER_SOURCE,
                    DEFAULT_DIGEST_PROMPT,
                ),
            )

            await db.execute(
                """
                INSERT OR IGNORE INTO prompt_templates (template_key, prompt_text)
                SELECT 'base_digest_prompt', digest_style_prompt
                FROM settings
                WHERE id = 1
                """
            )

            for source in DEFAULT_WEB_SOURCES:
                row = await db.execute(
                    "SELECT id FROM web_sources WHERE title = ?",
                    (source["title"],),
                )
                existing = await row.fetchone()
                if existing is None:
                    await db.execute(
                        """
                        INSERT OR IGNORE INTO web_sources (
                            title, feed_url, source_type, weight, is_active
                        ) VALUES (?, ?, ?, ?, 1)
                        """,
                        (
                            source["title"],
                            source["feed_url"],
                            source["source_type"],
                            source["weight"],
                        ),
                    )
                else:
                    await db.execute(
                        """
                        UPDATE web_sources
                        SET feed_url = ?, source_type = ?, weight = ?
                        WHERE title = ?
                        """,
                        (
                            source["feed_url"],
                            source["source_type"],
                            source["weight"],
                            source["title"],
                        ),
                    )

            await db.commit()
            logger.info("Database initialized at %s", self.db_path)

    async def _fetch_all(self, query: str, params: tuple[Any, ...] = ()) -> list[aiosqlite.Row]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(query, params)
            return await cursor.fetchall()

    async def _fetch_one(self, query: str, params: tuple[Any, ...] = ()) -> Optional[aiosqlite.Row]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(query, params)
            return await cursor.fetchone()

    async def list_tg_sources(self) -> list[aiosqlite.Row]:
        return await self._fetch_all(
            "SELECT * FROM tg_sources ORDER BY is_active DESC, added_at DESC"
        )

    async def list_active_tg_sources(self) -> list[aiosqlite.Row]:
        return await self._fetch_all(
            "SELECT * FROM tg_sources WHERE is_active = 1 ORDER BY added_at ASC"
        )

    async def add_tg_source(
        self,
        chat_id: int,
        title: str,
        username: Optional[str],
        invite_link: Optional[str],
        source_type: str,
    ) -> bool:
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    """
                    INSERT INTO tg_sources (chat_id, title, username, invite_link, source_type)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(chat_id) DO UPDATE SET
                        title = excluded.title,
                        username = excluded.username,
                        invite_link = COALESCE(excluded.invite_link, tg_sources.invite_link),
                        source_type = excluded.source_type,
                        is_active = 1
                    """,
                    (chat_id, title, username, invite_link, source_type),
                )
                await db.commit()
            return True
        except Exception as exc:
            logger.error("Failed to add TG source %s: %s", chat_id, exc)
            return False

    async def set_tg_source_active(self, source_id: int, is_active: bool) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE tg_sources SET is_active = ? WHERE id = ?",
                (1 if is_active else 0, source_id),
            )
            await db.commit()

    async def delete_tg_source(self, source_id: int) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM tg_sources WHERE id = ?", (source_id,))
            await db.commit()

    async def update_tg_source_last_seen(self, chat_id: int, message_id: int) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE tg_sources SET last_seen_message_id = ? WHERE chat_id = ?",
                (message_id, chat_id),
            )
            await db.commit()

    async def list_web_sources(self) -> list[aiosqlite.Row]:
        return await self._fetch_all(
            "SELECT * FROM web_sources ORDER BY is_active DESC, weight DESC, added_at DESC"
        )

    async def list_active_web_sources(self) -> list[aiosqlite.Row]:
        return await self._fetch_all(
            "SELECT * FROM web_sources WHERE is_active = 1 ORDER BY weight DESC, added_at ASC"
        )

    async def add_web_source(self, title: str, feed_url: str, source_type: str, weight: float) -> bool:
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    """
                    INSERT INTO web_sources (title, feed_url, source_type, weight)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(feed_url) DO UPDATE SET
                        title = excluded.title,
                        source_type = excluded.source_type,
                        weight = excluded.weight,
                        is_active = 1
                    """,
                    (title, feed_url, source_type, weight),
                )
                await db.commit()
            return True
        except Exception as exc:
            logger.error("Failed to add web source %s: %s", feed_url, exc)
            return False

    async def set_web_source_active(self, source_id: int, is_active: bool) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE web_sources SET is_active = ? WHERE id = ?",
                (1 if is_active else 0, source_id),
            )
            await db.commit()

    async def delete_web_source(self, source_id: int) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM web_sources WHERE id = ?", (source_id,))
            await db.commit()

    async def touch_web_source(self, source_id: int) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE web_sources SET last_checked_at = CURRENT_TIMESTAMP WHERE id = ?",
                (source_id,),
            )
            await db.commit()

    async def get_settings(self) -> aiosqlite.Row:
        row = await self._fetch_one("SELECT * FROM settings WHERE id = 1")
        if row is None:
            raise RuntimeError("settings row is missing")
        return row

    async def get_prompt_template(self, template_key: str = 'base_digest_prompt') -> Optional[str]:
        row = await self._fetch_one(
            "SELECT prompt_text FROM prompt_templates WHERE template_key = ?",
            (template_key,),
        )
        if row is None:
            return None
        return str(row['prompt_text'])

    async def update_schedule(self, morning_fetch: str, morning_digest: str, evening_fetch: str, evening_digest: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                UPDATE settings
                SET morning_web_fetch = ?, morning_digest_time = ?,
                    evening_web_fetch = ?, evening_digest_time = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = 1
                """,
                (morning_fetch, morning_digest, evening_fetch, evening_digest),
            )
            await db.commit()

    async def update_prompt(self, prompt: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                UPDATE settings
                SET digest_style_prompt = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = 1
                """,
                (prompt,),
            )
            await db.commit()

    async def store_raw_item(self, item: RawItemPayload, extra: Optional[dict[str, Any]] = None) -> bool:
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    """
                    INSERT OR IGNORE INTO raw_items (
                        source_kind, source_id, external_id, title, text,
                        url, published_at, raw_hash, language, extra_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item.source_kind,
                        item.source_id,
                        item.external_id,
                        item.title,
                        item.text,
                        item.url,
                        item.published_at,
                        item.raw_hash,
                        item.language,
                        json.dumps(extra, ensure_ascii=False) if extra else None,
                    ),
                )
                await db.commit()
                return db.total_changes > 0
        except Exception as exc:
            logger.error("Failed to store raw item %s/%s: %s", item.source_kind, item.external_id, exc)
            return False

    async def recent_raw_items(self, limit: int = 20) -> list[aiosqlite.Row]:
        return await self._fetch_all(
            """
            SELECT
                raw_items.*,
                COALESCE(web_sources.title, tg_sources.title) AS source_title
            FROM raw_items
            LEFT JOIN web_sources
                ON raw_items.source_kind = 'web' AND raw_items.source_id = web_sources.id
            LEFT JOIN tg_sources
                ON raw_items.source_kind = 'telegram' AND raw_items.source_id = tg_sources.id
            ORDER BY datetime(COALESCE(raw_items.published_at, raw_items.fetched_at)) DESC, raw_items.id DESC
            LIMIT ?
            """,
            (limit,),
        )

    async def create_digest_run(self, run_type: str, requested_topic: Optional[str] = None) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                INSERT INTO digest_runs (run_type, requested_topic, status)
                VALUES (?, ?, 'queued')
                """,
                (run_type, requested_topic),
            )
            await db.commit()
            return int(cursor.lastrowid)

    async def update_digest_run_status(self, run_id: int, status: str, result_message: Optional[str] = None) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                UPDATE digest_runs
                SET status = ?, result_message = ?, finished_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (status, result_message, run_id),
            )
            await db.commit()

    async def set_digest_run_window(self, run_id: int, window_start: str, window_end: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE digest_runs SET window_start = ?, window_end = ? WHERE id = ?",
                (window_start, window_end, run_id),
            )
            await db.commit()

    async def get_raw_items_for_window(self, window_start: str, window_end: str) -> list[aiosqlite.Row]:
        return await self._fetch_all(
            """
            SELECT
                raw_items.*,
                COALESCE(web_sources.title, tg_sources.title) AS source_title
            FROM raw_items
            LEFT JOIN web_sources
                ON raw_items.source_kind = 'web' AND raw_items.source_id = web_sources.id
            LEFT JOIN tg_sources
                ON raw_items.source_kind = 'telegram' AND raw_items.source_id = tg_sources.id
            WHERE datetime(COALESCE(raw_items.published_at, raw_items.fetched_at)) >= datetime(?)
              AND datetime(COALESCE(raw_items.published_at, raw_items.fetched_at)) <= datetime(?)
            ORDER BY datetime(COALESCE(raw_items.published_at, raw_items.fetched_at)) DESC, raw_items.id DESC
            """,
            (window_start, window_end),
        )

    async def get_published_cluster_keys_since(self, since_iso: str) -> set[str]:
        rows = await self._fetch_all(
            "SELECT cluster_key FROM published_stories WHERE datetime(last_published_at) >= datetime(?)",
            (since_iso,),
        )
        return {row['cluster_key'] for row in rows}

    async def upsert_story_clusters(self, clusters: list) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            for cluster in clusters:
                await db.execute(
                    """
                    INSERT INTO story_clusters (
                        cluster_key, canonical_title, summary_candidate, primary_url,
                        primary_source_kind, primary_source_id, topic_tags, block_type, novelty_score
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(cluster_key) DO UPDATE SET
                        canonical_title = excluded.canonical_title,
                        summary_candidate = excluded.summary_candidate,
                        primary_url = excluded.primary_url,
                        primary_source_kind = excluded.primary_source_kind,
                        primary_source_id = excluded.primary_source_id,
                        topic_tags = excluded.topic_tags,
                        block_type = excluded.block_type,
                        novelty_score = excluded.novelty_score
                    """,
                    (
                        cluster.cluster_key,
                        cluster.title,
                        cluster.summary_text,
                        cluster.primary_url,
                        cluster.primary_source_kind,
                        cluster.primary_source_id,
                        json.dumps(cluster.topic_tags, ensure_ascii=False),
                        cluster.block_hint,
                        0,
                    ),
                )
                row = await db.execute(
                    "SELECT id FROM story_clusters WHERE cluster_key = ?",
                    (cluster.cluster_key,),
                )
                cluster_row = await row.fetchone()
                if cluster_row is None:
                    continue
                cluster_id = cluster_row[0]
                for raw_item_id in cluster.raw_item_ids:
                    await db.execute(
                        "INSERT OR IGNORE INTO cluster_items (cluster_id, raw_item_id) VALUES (?, ?)",
                        (cluster_id, raw_item_id),
                    )
            await db.commit()

    async def save_digest_entries(self, run_id: int, items: list) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            for item in items:
                await db.execute(
                    """
                    INSERT INTO digest_entries (
                        digest_run_id, cluster_id, section_name, rank_order,
                        final_title, final_body, final_url, source_label
                    ) VALUES (?, NULL, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        item.section_name,
                        item.rank_order,
                        item.title,
                        item.body,
                        item.url,
                        item.source_label,
                    ),
                )
            await db.commit()

    async def mark_published_stories(self, cluster_keys: list[str], run_id: int, published_at: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            for cluster_key in cluster_keys:
                await db.execute(
                    """
                    INSERT INTO published_stories (cluster_key, first_published_at, last_published_at, last_digest_run_id)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(cluster_key) DO UPDATE SET
                        last_published_at = excluded.last_published_at,
                        last_digest_run_id = excluded.last_digest_run_id
                    """,
                    (cluster_key, published_at, published_at, run_id),
                )
            await db.commit()










