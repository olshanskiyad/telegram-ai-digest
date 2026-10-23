import html
import json
import logging
import re
from collections import Counter, defaultdict
from typing import Any

from ai_news_digest.content_filters import is_excluded_telegram_post
from ai_news_digest.deepseek_client import DeepSeekClient


logger = logging.getLogger(__name__)


THEME_HINTS = {
    'AI и модели': ['gpt', 'claude', 'gemini', 'llm', 'model', 'openai', 'anthropic', 'deepseek', 'mistral'],
    'Автоматизация и агенты': ['agent', 'agents', 'automation', 'workflow', 'zapier', 'make', 'assistant'],
    'Кодинг и инструменты': ['code', 'coding', 'sdk', 'cli', 'github', 'repo', 'cursor', 'ide', 'api', 'python'],
    'Бизнес и рынок': ['startup', 'funding', 'market', 'marketing', 'sales', 'revenue', 'enterprise', 'deal'],
    'Продукты и релизы': ['release', 'launched', 'launch', 'вышел', 'запустил', 'добавили', 'обновил', 'релиз'],
}

SIGNAL_PATTERNS = (
    'выш', 'запуст', 'добав', 'обнов', 'релиз', 'анонс', 'представ', 'купил', 'привл', 'сделк',
    'launch', 'release', 'ship', 'update', 'acquire', 'funding', 'rolled out', 'introduced',
)

STOPWORDS = {
    'this', 'that', 'with', 'from', 'have', 'about', 'there', 'their', 'will', 'your', 'если', 'когда', 'после',
    'потому', 'который', 'сегодня', 'вчера', 'очень', 'просто', 'только', 'когда', 'через', 'между', 'можно',
    'which', 'would', 'could', 'should', 'while', 'where', 'about', 'https', 'http', 'telegram', 'канал',
}


class UnreadChannelsSummarizer:
    def __init__(self, llm_client: DeepSeekClient | None = None) -> None:
        self.llm_client = llm_client

    async def summarize(self, channels: list[dict], requested_topic: str | None = None) -> str:
        prepared = self._prepare_payload(channels, requested_topic=requested_topic)
        if not prepared['highlight_posts'] and not prepared['dense_channels']:
            if requested_topic:
                return f'По теме <b>{html.escape(requested_topic)}</b> среди непрочитанного сейчас не нашлось сильных постов без промо.'
            return 'Сейчас среди непрочитанных каналов не нашлось достаточно сильных постов без промо.'

        if self.llm_client is None:
            return self._fallback(prepared)

        try:
            response = await self.llm_client.json_chat(
                self._system_prompt(),
                json.dumps(prepared, ensure_ascii=False),
                model=self.llm_client.chat_model,
                temperature=0.2,
                max_tokens=5000,
            )
            return self._render_llm_response(response, prepared)
        except Exception as exc:
            logger.warning('Unread channels summary via LLM failed, fallback used: %s', exc)
            return self._fallback(prepared)

    def _system_prompt(self) -> str:
        return (
            'Ты собираешь digest по непрочитанным Telegram-каналам владельца аккаунта. '
            'Вход уже отфильтрован от рекламы и содержит: '
            '1) сильные отдельные посты по темам со всех каналов; '
            '2) каналы с плотным потоком, где нужно дать тезисную сводку. '
            'Верни JSON вида '
            '{"title": str, "sections": [{"name": str, "items": [{"headline": str, "channel": str, "body": str, "url": str}]}], '
            '"dense_channels": [{"channel": str, "body": str, "bullets": [{"text": str, "url": str}]}]}. '
            'Правила: '
            '1. sections - это блоки по темам и сильным постам; внутри можно иметь несколько постов из одного канала. '
            '2. dense_channels - это отдельный блок для каналов, где много непрочитанного; дай короткую вводную и 3-6 тезисов по одному предложению на пост. '
            '3. Пиши по-русски, живо и плотно, без канцелярита. '
            '4. Не вставляй голый URL в body/text; ссылка будет отдельной строкой словом Источник. '
            '5. Не придумывай фактов и не растягивай текст.'
        )

    def _prepare_payload(self, channels: list[dict], requested_topic: str | None = None) -> dict[str, Any]:
        topic_tokens = self._tokenize(requested_topic or '')
        channel_rows = []
        post_candidates: list[dict[str, Any]] = []
        dense_candidates: list[dict[str, Any]] = []

        for channel in channels:
            channel_title = str(channel.get('title') or 'unknown channel').strip()
            unread_count = int(channel.get('unread_count') or 0)
            filtered_posts = []
            for message in channel.get('messages') or []:
                candidate = self._build_post_candidate(channel_title, unread_count, message, topic_tokens)
                if candidate is not None:
                    filtered_posts.append(candidate)
                    post_candidates.append(candidate)

            channel_rows.append(
                {
                    'channel': channel_title,
                    'unread_count': unread_count,
                    'usable_posts': len(filtered_posts),
                }
            )
            dense_channel = self._build_dense_channel(channel_title, unread_count, filtered_posts)
            if dense_channel is not None:
                dense_candidates.append(dense_channel)

        highlight_posts = self._pick_highlight_posts(post_candidates, requested_topic=requested_topic)
        dense_channels = self._pick_dense_channels(dense_candidates)

        return {
            'title_hint': self._title_hint(requested_topic),
            'requested_topic': requested_topic or '',
            'channel_count': len(channel_rows),
            'channels': channel_rows,
            'highlight_posts': highlight_posts,
            'dense_channels': dense_channels,
        }

    def _build_post_candidate(
        self,
        channel_title: str,
        unread_count: int,
        message: dict[str, Any],
        topic_tokens: set[str],
    ) -> dict[str, Any] | None:
        original_text = (message.get('text') or '').strip()
        text = self._normalize_text(original_text)
        if len(text) < 40:
            return None
        if is_excluded_telegram_post(text):
            return None

        topic_score = self._topic_score(text, topic_tokens)
        theme = self._guess_theme(text)
        score = self._score_post(text, topic_score=topic_score, theme=theme)
        if score < 2.4:
            return None

        return {
            'channel': channel_title,
            'unread_count': unread_count,
            'message_id': int(message.get('message_id') or 0),
            'url': (message.get('url') or '').strip(),
            'date': message.get('date') or '',
            'text': text,
            'theme': theme,
            'score': round(score, 2),
            'topic_score': round(topic_score, 2),
            'keywords': self._extract_keywords(text),
            'headline_hint': self._headline_hint(text),
            'body_hint': self._body_hint(text),
        }

    def _score_post(self, text: str, *, topic_score: float, theme: str) -> float:
        lowered = text.casefold()
        score = 0.0
        length = len(text)

        if 70 <= length <= 900:
            score += 2.0
        elif 40 <= length < 70:
            score += 0.9
        else:
            score += 1.1

        signal_hits = sum(1 for pattern in SIGNAL_PATTERNS if pattern in lowered)
        score += min(signal_hits, 3) * 0.8

        if re.search(r'\b\d+[\d\s.,%$€₽]*\b', text):
            score += 0.8
        if re.search(r'[A-Z][a-z]{2,}', text) or re.search(r'[А-ЯЁ][а-яё]{2,}', text):
            score += 0.5
        if ':' in text or '—' in text or '-' in text:
            score += 0.25
        if theme != 'Разное':
            score += 0.4

        score += topic_score * 2.4

        if len(re.findall(r'[!?]', text)) >= 2:
            score += 0.2
        if len(re.findall(r'https?://', text)) >= 2:
            score -= 0.3
        if len(text.split()) < 9:
            score -= 0.7

        return score

    def _pick_highlight_posts(self, post_candidates: list[dict[str, Any]], requested_topic: str | None = None) -> list[dict[str, Any]]:
        topic_mode = bool((requested_topic or '').strip())
        candidates = list(post_candidates)
        if topic_mode:
            filtered = [item for item in candidates if item['topic_score'] > 0]
            if filtered:
                candidates = filtered

        candidates.sort(
            key=lambda item: (
                item['topic_score'],
                item['score'],
                item['unread_count'],
                item['message_id'],
            ),
            reverse=True,
        )

        theme_limits: dict[str, int] = defaultdict(int)
        channel_limits: dict[str, int] = defaultdict(int)
        selected = []
        max_total = 12 if not topic_mode else 10
        max_per_theme = 4 if not topic_mode else 5
        max_per_channel = 2 if not topic_mode else 3

        for candidate in candidates:
            theme = candidate['theme']
            channel = candidate['channel']
            if theme_limits[theme] >= max_per_theme:
                continue
            if channel_limits[channel] >= max_per_channel:
                continue
            selected.append(candidate)
            theme_limits[theme] += 1
            channel_limits[channel] += 1
            if len(selected) >= max_total:
                break
        return selected

    def _build_dense_channel(self, channel_title: str, unread_count: int, posts: list[dict[str, Any]]) -> dict[str, Any] | None:
        if not posts:
            return None

        sorted_posts = sorted(posts, key=lambda item: (item['score'], item['message_id']), reverse=True)
        usable_count = len(sorted_posts)
        theme_count = len({item['theme'] for item in sorted_posts if item['theme'] != 'Разное'})
        density_score = sum(item['score'] for item in sorted_posts[:5])

        if usable_count < 6 and unread_count < 10:
            return None
        if density_score < 15 and usable_count < 8 and theme_count < 2:
            return None

        bullets = []
        for item in sorted_posts[: min(7, usable_count)]:
            bullets.append(
                {
                    'text': self._one_sentence(item['text']),
                    'url': item['url'],
                    'score': item['score'],
                    'theme': item['theme'],
                }
            )

        dominant_themes = self._dominant_themes(sorted_posts)
        body = (
            f'В канале накопился плотный поток: {usable_count} сильных непрочитанных постов без промо. '
            f'Чаще всего всплывали темы: {dominant_themes}.'
        )
        return {
            'channel': channel_title,
            'unread_count': unread_count,
            'usable_posts': usable_count,
            'density_score': round(density_score, 2),
            'body': body,
            'bullets': bullets,
        }

    def _pick_dense_channels(self, dense_candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        dense_candidates.sort(
            key=lambda item: (item['usable_posts'], item['density_score'], item['unread_count']),
            reverse=True,
        )
        return dense_candidates[:4]

    def _render_llm_response(self, response: dict[str, Any], prepared: dict[str, Any]) -> str:
        title = response.get('title') or prepared['title_hint']
        sections = response.get('sections') or []
        dense_channels = response.get('dense_channels') or []

        lines = [f'<b>{html.escape(title)}</b>', '']
        item_index = 1

        for section in sections:
            items = section.get('items') or []
            if not items:
                continue
            section_name = (section.get('name') or '').strip() or 'Разное'
            lines.append(f'<b>{html.escape(section_name)}</b>')
            lines.append('')
            for item in items[:12]:
                channel = (item.get('channel') or '').strip()
                headline = (item.get('headline') or '').strip() or 'Пост'
                body = self._normalize_text(item.get('body') or '')
                url = (item.get('url') or '').strip()
                lines.append(f'{item_index}. <b>{html.escape(headline)}</b>')
                if channel:
                    lines.append(f'<i>{html.escape(channel)}</i>')
                lines.append(html.escape(body))
                if url:
                    lines.append(f'<a href="{html.escape(url, quote=True)}">Источник</a>')
                lines.append('')
                item_index += 1

        if dense_channels:
            lines.append('<b>Каналы с плотным потоком</b>')
            lines.append('')
            for channel_item in dense_channels[:4]:
                channel = (channel_item.get('channel') or '').strip() or 'Канал'
                body = self._normalize_text(channel_item.get('body') or '')
                lines.append(f'• <b>{html.escape(channel)}</b>')
                if body:
                    lines.append(html.escape(body))
                for bullet in (channel_item.get('bullets') or [])[:6]:
                    bullet_text = self._normalize_text(bullet.get('text') or '')
                    bullet_url = (bullet.get('url') or '').strip()
                    if bullet_text:
                        lines.append(f'- {html.escape(bullet_text)}')
                    if bullet_url:
                        lines.append(f'<a href="{html.escape(bullet_url, quote=True)}">Источник</a>')
                lines.append('')

        rendered = '\n'.join(lines).strip()
        return rendered if rendered else self._fallback(prepared)

    def _fallback(self, prepared: dict[str, Any]) -> str:
        lines = [f'<b>{html.escape(prepared["title_hint"])}</b>', '']
        by_theme: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in prepared['highlight_posts']:
            by_theme[item['theme']].append(item)

        for theme, items in by_theme.items():
            lines.append(f'<b>{html.escape(theme)}</b>')
            lines.append('')
            for index, item in enumerate(items, start=1):
                lines.append(f'{index}. <b>{html.escape(item["headline_hint"])}</b>')
                lines.append(f'<i>{html.escape(item["channel"])}</i>')
                lines.append(html.escape(item['body_hint']))
                if item.get('url'):
                    lines.append(f'<a href="{html.escape(item["url"], quote=True)}">Источник</a>')
                lines.append('')

        if prepared['dense_channels']:
            lines.append('<b>Каналы с плотным потоком</b>')
            lines.append('')
            for channel in prepared['dense_channels']:
                lines.append(f'• <b>{html.escape(channel["channel"])}</b>')
                lines.append(html.escape(channel['body']))
                for bullet in channel['bullets'][:6]:
                    lines.append(f'- {html.escape(bullet["text"])}')
                    if bullet.get('url'):
                        lines.append(f'<a href="{html.escape(bullet["url"], quote=True)}">Источник</a>')
                lines.append('')

        return '\n'.join(lines).strip()

    def _guess_theme(self, text: str) -> str:
        lowered = text.casefold()
        best_theme = 'Разное'
        best_hits = 0
        for theme, hints in THEME_HINTS.items():
            hits = sum(1 for hint in hints if hint in lowered)
            if hits > best_hits:
                best_theme = theme
                best_hits = hits
        return best_theme

    def _topic_score(self, text: str, topic_tokens: set[str]) -> float:
        if not topic_tokens:
            return 0.0
        text_tokens = self._tokenize(text)
        if not text_tokens:
            return 0.0
        overlap = len(topic_tokens & text_tokens)
        if overlap == 0:
            compact_topic = ' '.join(sorted(topic_tokens))
            if compact_topic and compact_topic in text.casefold():
                return 1.0
            return 0.0
        return overlap / max(1, len(topic_tokens))

    def _tokenize(self, text: str) -> set[str]:
        tokens = set()
        for token in re.findall(r"[A-Za-zА-Яа-яЁё0-9_\-]{3,}", (text or '').casefold()):
            if token in STOPWORDS:
                continue
            tokens.add(token)
        return tokens

    def _extract_keywords(self, text: str) -> list[str]:
        counts = Counter(self._tokenize(text))
        return [word for word, _ in counts.most_common(5)]

    def _headline_hint(self, text: str) -> str:
        base = self._one_sentence(text)
        if len(base) <= 80:
            return base
        shortened = base[:77].rstrip(' ,;:.')
        return shortened + '...'

    def _body_hint(self, text: str) -> str:
        first = self._one_sentence(text)
        if len(text) <= 220:
            return first
        remainder = self._normalize_text(text[len(first):])
        if remainder:
            second = self._one_sentence(remainder)
            joined = f'{first} {second}'.strip()
            return joined[:260].rstrip() + ('...' if len(joined) > 260 else '')
        return first

    def _one_sentence(self, text: str) -> str:
        normalized = self._normalize_text(text)
        if not normalized:
            return ''
        parts = re.split(r'(?<=[.!?])\s+', normalized)
        sentence = parts[0].strip()
        if len(sentence) < 55 and len(parts) > 1:
            sentence = f'{sentence} {parts[1].strip()}'
        if len(sentence) > 220:
            sentence = sentence[:217].rstrip() + '...'
        return sentence

    def _dominant_themes(self, posts: list[dict[str, Any]]) -> str:
        counts = Counter(item['theme'] for item in posts if item['theme'] != 'Разное')
        if not counts:
            return 'несколько разных тем'
        return ', '.join(theme for theme, _ in counts.most_common(3))

    def _normalize_text(self, text: str) -> str:
        value = re.sub(r'https?://\S+', '', (text or '').strip())
        return re.sub(r'\s+', ' ', value).strip()

    def _title_hint(self, requested_topic: str | None) -> str:
        if requested_topic:
            return f'Непрочитанные каналы по теме: {requested_topic}'
        return 'Непрочитанные каналы: главное по темам'
