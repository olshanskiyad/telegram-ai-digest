import html
import json
import logging
import re
from collections import Counter

from ai_news_digest.deepseek_client import DeepSeekClient


logger = logging.getLogger(__name__)


STOPWORDS = {
    'это', 'этот', 'эта', 'эти', 'того', 'того', 'такой', 'такая', 'такие', 'так', 'там', 'тут', 'или', 'если',
    'когда', 'тогда', 'потом', 'просто', 'вообще', 'очень', 'сегодня', 'завтра', 'вчера', 'сейчас', 'только',
    'потому', 'который', 'которая', 'которые', 'которое', 'чтобы', 'чего', 'кого', 'кому', 'комуто', 'него', 'неё',
    'были', 'было', 'будет', 'будто', 'может', 'можно', 'нужно', 'надо', 'есть', 'нет', 'даже', 'тоже', 'лишь',
    'ещё', 'уже', 'после', 'перед', 'между', 'через', 'среди', 'здесь', 'куда', 'оттуда', 'сюда', 'для', 'про',
    'под', 'над', 'при', 'как', 'что', 'чем', 'чтобы', 'из', 'за', 'по', 'на', 'но', 'да', 'не', 'ни', 'а', 'и',
    'to', 'for', 'with', 'from', 'that', 'this', 'have', 'just', 'into', 'your', 'http', 'https'
}

CONFLICT_HINTS = ['скандал', 'конфликт', 'долг', 'деньги', 'верни', 'обман', 'оскорб', 'угроз', 'срач', 'спор']
JOKE_HINTS = ['лол', 'ахах', 'аха', 'ржу', 'мем', 'сериал', 'смешно', 'кек', '))))', '😂', '🤣', '😄', '😍']
MONEY_HINTS = ['руб', 'usd', '$', '€', 'долг', 'деньг', 'донат']


class ChatSummarizer:
    def __init__(self, llm_client: DeepSeekClient | None = None) -> None:
        self.llm_client = llm_client

    async def summarize(self, chat_title: str, messages: list[dict], limit: int) -> str:
        if not messages:
            return 'В чате нет текстовых сообщений для summary.'

        prepared = self._prepare_payload(chat_title, messages, limit)
        if self.llm_client is None:
            return self._fallback(prepared)

        try:
            system_prompt = (
                'Ты анализируешь Telegram-чат и делаешь живой, понятный summary на русском. '
                'Верни JSON вида '
                '{"title": str, "overview": str, "key_points": [str], "hot_messages": [str], "mood": str, "links": [str]}. '
                'Нужен именно пересказ сути, а не набор слов. '
                'В overview дай 1 абзац: что происходило в чате и почему обсуждение зацепило людей. '
                'В key_points перечисли 4-6 самых акцентных моментов простым языком. '
                'В hot_messages приведи 3-5 самых ярких сообщений, поворотов или реплик в пересказе или короткой цитате. '
                'В mood коротко опиши атмосферу обсуждения, можно с лёгкой иронией без перегибов. '
                'Не используй слово "сигналы" и не превращай ответ в сухую аналитику.'
            )
            response = await self.llm_client.json_chat(
                system_prompt,
                json.dumps(prepared, ensure_ascii=False),
                model=self.llm_client.chat_model,
                temperature=0.25,
                max_tokens=3500,
            )
            return self._render_llm_response(response, prepared)
        except Exception as exc:
            logger.warning('Chat summary via LLM failed, fallback used: %s', exc)
            return self._fallback(prepared)

    def _prepare_payload(self, chat_title: str, messages: list[dict], limit: int) -> dict:
        cleaned_messages = []
        senders = Counter()
        links = []
        for message in messages:
            text = re.sub(r'\s+', ' ', (message.get('text') or '').strip())
            if not text:
                continue
            sender = (message.get('sender') or 'unknown').strip() or 'unknown'
            senders[sender] += 1
            links.extend(re.findall(r'https?://\S+', text))
            cleaned_messages.append(
                {
                    'sender': sender,
                    'date': message.get('date'),
                    'text': text[:360],
                }
            )

        return {
            'chat_title': chat_title,
            'requested_limit': limit,
            'read_messages': len(cleaned_messages),
            'top_participants': [f"{sender} ({count})" for sender, count in senders.most_common(8)],
            'top_keywords': self._extract_keywords(cleaned_messages),
            'representative_messages': self._select_representative_messages(cleaned_messages),
            'links': list(dict.fromkeys(links))[:10],
        }

    def _render_llm_response(self, response: dict, prepared: dict) -> str:
        title = response.get('title') or f"Разбор чата: {prepared['chat_title']}"
        overview = (response.get('overview') or '').strip()
        key_points = response.get('key_points') or []
        hot_messages = response.get('hot_messages') or []
        mood = (response.get('mood') or '').strip()
        links = response.get('links') or prepared.get('links') or []

        lines = [f"<b>{html.escape(title)}</b>", '']
        if overview:
            lines.append(html.escape(overview))
            lines.append('')
        if key_points:
            lines.append('<b>Ключевые моменты</b>')
            lines.extend([f"- {html.escape(str(item))}" for item in key_points[:6]])
            lines.append('')
        if hot_messages:
            lines.append('<b>Яркие сообщения и эпизоды</b>')
            lines.extend([f"- {html.escape(str(item))}" for item in hot_messages[:5]])
            lines.append('')
        if mood:
            lines.append('<b>Атмосфера</b>')
            lines.append(html.escape(mood))
            lines.append('')
        if links:
            lines.append('<b>Ссылки</b>')
            lines.extend([str(item) for item in links[:10]])
        return '\n'.join(lines).strip()

    def _fallback(self, prepared: dict) -> str:
        keywords = prepared['top_keywords']
        highlights = prepared['representative_messages']
        participants = prepared['top_participants']
        links = prepared['links']

        overview_parts = []
        if keywords:
            overview_parts.append('Чат в основном крутился вокруг ' + ', '.join(keywords[:4]) + '.')
        if highlights:
            overview_parts.append('Больше всего зацепили эпизоды вроде: ' + self._shorten(highlights[0]['text']) + '.')
        overview_parts.append('По ощущениям это было не просто обсуждение, а почти мини-сериал в реальном времени.')
        overview = ' '.join(overview_parts)

        key_points = []
        for item in highlights[:4]:
            key_points.append(self._message_to_point(item['text']))

        hot_messages = [self._shorten(item['text']) for item in highlights[:4]]
        mood = self._infer_mood([item['text'] for item in highlights], participants)

        lines = [f"<b>Разбор чата: {html.escape(prepared['chat_title'])}</b>", '']
        lines.append(html.escape(overview))
        lines.append('')
        if key_points:
            lines.append('<b>Ключевые моменты</b>')
            lines.extend([f"- {html.escape(point)}" for point in key_points])
            lines.append('')
        if hot_messages:
            lines.append('<b>Яркие сообщения и эпизоды</b>')
            lines.extend([f"- {html.escape(item)}" for item in hot_messages])
            lines.append('')
        lines.append('<b>Атмосфера</b>')
        lines.append(html.escape(mood))
        lines.append('')
        if links:
            lines.append('<b>Ссылки</b>')
            lines.extend(links[:10])
        return '\n'.join(lines).strip()

    def _extract_keywords(self, messages: list[dict]) -> list[str]:
        words = Counter()
        for message in messages:
            text = (message.get('text') or '').lower()
            for token in re.findall(r"[A-Za-zА-Яа-яЁё0-9_\-]{4,}", text):
                if token in STOPWORDS:
                    continue
                if token.isdigit() and len(token) < 4:
                    continue
                words[token] += 1
        return [word for word, _ in words.most_common(10)]

    def _select_representative_messages(self, messages: list[dict]) -> list[dict]:
        if not messages:
            return []

        candidates = []
        for index, message in enumerate(messages):
            score = self._message_score(message['text'])
            candidates.append((score, index, message))

        selected = []
        seen = set()
        for message in messages[:4] + messages[-4:]:
            key = message['text']
            if key in seen:
                continue
            selected.append(message)
            seen.add(key)

        for _, _, message in sorted(candidates, key=lambda item: (item[0], item[1]), reverse=True):
            key = message['text']
            if key in seen:
                continue
            selected.append(message)
            seen.add(key)
            if len(selected) >= 18:
                break

        return selected[:18]

    def _message_score(self, text: str) -> int:
        lowered = text.lower()
        score = 0
        length = len(text)
        if 50 <= length <= 280:
            score += 3
        elif 20 <= length <= 360:
            score += 1
        if any(hint in lowered for hint in CONFLICT_HINTS):
            score += 3
        if any(hint in lowered for hint in JOKE_HINTS):
            score += 2
        if any(hint in lowered for hint in MONEY_HINTS):
            score += 2
        if re.search(r'[!?]{1,}', text):
            score += 1
        if re.search(r'\d{3,}', text):
            score += 1
        return score

    def _message_to_point(self, text: str) -> str:
        short = self._shorten(text)
        return f'Один из заметных поворотов: {short}'

    def _infer_mood(self, texts: list[str], participants: list[str]) -> str:
        joined = ' '.join(texts).lower()
        conflict_score = sum(1 for hint in CONFLICT_HINTS if hint in joined)
        joke_score = sum(1 for hint in JOKE_HINTS if hint in joined)
        active = ', '.join(participants[:4]) if participants else 'несколько активных участников'
        if conflict_score > joke_score + 1:
            return f'Тон был нервный и местами токсичный, но со стороны всё читалось почти как сериал. Больше всего разговор тянули: {active}.'
        if joke_score > conflict_score:
            return f'Обсуждение шло с иронией и подколами: люди явно не только спорили, но и ловили удовольствие от самого шоу. Активнее всего были: {active}.'
        return f'Атмосфера была живая: спор, комментарии, вбросы и реакция зрителей перемешались в один плотный поток. Активнее всего были: {active}.'

    def _shorten(self, text: str, limit: int = 180) -> str:
        text = re.sub(r'\s+', ' ', (text or '').strip())
        if len(text) <= limit:
            return text
        return text[: limit - 3].rstrip() + '...'
