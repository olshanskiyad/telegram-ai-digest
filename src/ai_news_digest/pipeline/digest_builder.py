import logging
import re
from collections import Counter
from datetime import datetime, timedelta, timezone

from ai_news_digest.content_filters import is_excluded_telegram_post, looks_promotional_text
from ai_news_digest.database import Database
from ai_news_digest.deepseek_client import DeepSeekClient
from ai_news_digest.models import FinalDigestItem
from ai_news_digest.pipeline.ranking import DigestFormatter, StoryClusterer, StoryRanker


logger = logging.getLogger(__name__)


class DigestBuilder:
    def __init__(self, db: Database, llm_client: DeepSeekClient | None = None) -> None:
        self.db = db
        self.clusterer = StoryClusterer()
        self.ranker = StoryRanker(llm_client=llm_client)
        self.formatter = DigestFormatter(llm_client=llm_client)

    async def build_digest(
        self,
        *,
        run_type: str,
        requested_topic: str | None = None,
        mark_published: bool = False,
        heading: str | None = None,
        exclude_published: bool = True,
    ) -> tuple[str, list[FinalDigestItem], int]:
        run_id = await self.db.create_digest_run(run_type=run_type, requested_topic=requested_topic)
        settings_row = await self.db.get_settings()

        window_end_dt = datetime.now(timezone.utc)
        window_hours = self._get_window_hours(run_type=run_type, requested_topic=requested_topic)
        window_start_dt = window_end_dt - timedelta(hours=window_hours)
        raw_items = await self.db.get_raw_items_for_window(window_start_dt.isoformat(), window_end_dt.isoformat())
        raw_items = self._filter_raw_rows(raw_items, window_start_dt)

        if self._should_expand_window(raw_items, run_type=run_type, requested_topic=requested_topic):
            expanded_hours = self._get_expanded_window_hours(raw_items, run_type=run_type, requested_topic=requested_topic)
            expanded_start_dt = window_end_dt - timedelta(hours=expanded_hours)
            raw_items = await self.db.get_raw_items_for_window(expanded_start_dt.isoformat(), window_end_dt.isoformat())
            raw_items = self._filter_raw_rows(raw_items, expanded_start_dt)
            window_start_dt = expanded_start_dt
            window_hours = expanded_hours

        window_start = window_start_dt.isoformat()
        window_end = window_end_dt.isoformat()
        await self.db.set_digest_run_window(run_id, window_start, window_end)

        if not raw_items:
            message = 'Пока нет данных для сборки дайджеста.'
            await self.db.update_digest_run_status(run_id, 'empty', message)
            return message, [], run_id

        clusters = self.clusterer.cluster(raw_items)
        await self.db.upsert_story_clusters(clusters)

        fresh_clusters = clusters
        if exclude_published:
            dedup_since_dt = window_end_dt - timedelta(days=int(settings_row['dedup_window_days']))
            published_cluster_keys = await self.db.get_published_cluster_keys_since(dedup_since_dt.isoformat())
            fresh_clusters = [cluster for cluster in clusters if cluster.cluster_key not in published_cluster_keys]
            if not fresh_clusters:
                message = 'Все подходящие сюжеты уже были в прошлых дайджестах за окно дедупа.'
                await self.db.update_digest_run_status(run_id, 'empty_after_dedup', message)
                return message, [], run_id

        ranked = await self.ranker.rank(fresh_clusters, requested_topic=requested_topic)
        news_items, useful_items = self.ranker.select_diverse(
            ranked,
            news_count=int(settings_row['default_news_count']),
            useful_count=int(settings_row['default_useful_count']),
            max_per_source=int(settings_row['max_per_source']),
            relaxed_max_per_source=int(settings_row['relaxed_max_per_source']),
            requested_topic=requested_topic,
        )

        total_target = int(settings_row['default_news_count']) + int(settings_row['default_useful_count'])
        current_total = len(news_items) + len(useful_items)
        if current_total < total_target:
            remaining = [item for item in ranked if item not in news_items and item not in useful_items]
            top_up = self.ranker._pick_with_source_cap(
                remaining,
                total_target - current_total,
                int(settings_row['max_per_source']),
                int(settings_row['relaxed_max_per_source']),
            )
            for item in top_up:
                if item.block_type == 'useful':
                    useful_items.append(item)
                else:
                    news_items.append(item)

        if not news_items and not useful_items:
            message = 'Не удалось набрать достаточно качественных элементов для дайджеста.'
            await self.db.update_digest_run_status(run_id, 'empty_after_ranking', message)
            return message, [], run_id

        digest_title = heading or ('Тематический AI-дайджест' if requested_topic else 'AI-дайджест')
        text, final_items = await self.formatter.render(
            title=digest_title,
            news_items=news_items,
            useful_items=useful_items,
            style_prompt=settings_row['digest_style_prompt'],
            requested_topic=requested_topic,
        )

        await self.db.save_digest_entries(run_id, final_items)
        if mark_published and final_items:
            await self.db.mark_published_stories(
                [item.cluster_key for item in final_items if item.cluster_key],
                run_id=run_id,
                published_at=window_end,
            )
        await self.db.update_digest_run_status(run_id, 'completed', text)
        logger.info('Digest built run_id=%s items=%s window_hours=%s exclude_published=%s', run_id, len(final_items), window_hours, exclude_published)
        return text, final_items, run_id

    async def build_preview(self, run_type: str, requested_topic: str | None = None) -> str:
        text, _, _ = await self.build_digest(
            run_type=run_type,
            requested_topic=requested_topic,
            mark_published=False,
            heading='Черновик AI-дайджеста' if not requested_topic else 'Черновик тематического дайджеста',
            exclude_published=True,
        )
        return text

    def _get_window_hours(self, *, run_type: str, requested_topic: str | None) -> int:
        if requested_topic:
            return 72
        if run_type == 'manual':
            return 72
        if run_type.startswith('scheduled_'):
            return 24
        return 24

    def _should_expand_window(self, raw_items: list, *, run_type: str, requested_topic: str | None) -> bool:
        if requested_topic or run_type == 'manual':
            return False
        if len(raw_items) < 12:
            return True
        unique_sources = {str(row['source_kind']) + ':' + str(row['source_id']) for row in raw_items}
        if len(unique_sources) < 5:
            return True
        if run_type.startswith('scheduled_') and self._has_source_kind_imbalance(raw_items):
            return True
        return False

    def _get_expanded_window_hours(self, raw_items: list, *, run_type: str, requested_topic: str | None) -> int:
        if requested_topic or run_type == 'manual':
            return 72
        if run_type.startswith('scheduled_') and self._has_source_kind_imbalance(raw_items):
            return 72
        return 48

    def _has_source_kind_imbalance(self, rows: list) -> bool:
        counts = Counter((row['source_kind'] or '') for row in rows)
        web_count = counts.get('web', 0)
        telegram_count = counts.get('telegram', 0)
        return (web_count < 3 and telegram_count >= 5) or (telegram_count < 3 and web_count >= 5)

    def _filter_raw_rows(self, rows: list, window_start_dt: datetime) -> list:
        filtered = []
        for row in rows:
            if self._is_excluded_row(row):
                continue
            effective_dt = self._effective_row_datetime(row)
            if effective_dt is None or effective_dt >= window_start_dt:
                filtered.append(row)
        return filtered

    def _is_excluded_row(self, row) -> bool:
        haystack = f"{row['title'] or ''}\n{row['text'] or ''}"
        if (row['source_kind'] or '') == 'telegram':
            return is_excluded_telegram_post(haystack)
        return looks_promotional_text(haystack)

    def _effective_row_datetime(self, row) -> datetime | None:
        published_at = row['published_at']
        if published_at:
            return self._parse_iso(published_at)
        title = (row['title'] or '').strip()
        title_dt = self._extract_title_date(title)
        if title_dt is not None:
            return title_dt
        fetched_at = row['fetched_at']
        return self._parse_iso(fetched_at) if fetched_at else None

    def _parse_iso(self, value: str) -> datetime | None:
        try:
            normalized = value.replace('Z', '+00:00')
            dt = datetime.fromisoformat(normalized)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            return None

    def _extract_title_date(self, text: str) -> datetime | None:
        months = {
            'jan': 1, 'january': 1,
            'feb': 2, 'february': 2,
            'mar': 3, 'march': 3,
            'apr': 4, 'april': 4,
            'may': 5,
            'jun': 6, 'june': 6,
            'jul': 7, 'july': 7,
            'aug': 8, 'august': 8,
            'sep': 9, 'sept': 9, 'september': 9,
            'oct': 10, 'october': 10,
            'nov': 11, 'november': 11,
            'dec': 12, 'december': 12,
        }
        match = re.search(r'\b(?P<month>Jan|January|Feb|February|Mar|March|Apr|April|May|Jun|June|Jul|July|Aug|August|Sep|Sept|September|Oct|October|Nov|November|Dec|December)\s+(?P<day>\d{1,2}),\s+(?P<year>\d{4})\b', text, flags=re.IGNORECASE)
        if not match:
            return None
        month = months.get(match.group('month').lower())
        if month is None:
            return None
        try:
            return datetime(int(match.group('year')), month, int(match.group('day')), tzinfo=timezone.utc)
        except ValueError:
            return None








