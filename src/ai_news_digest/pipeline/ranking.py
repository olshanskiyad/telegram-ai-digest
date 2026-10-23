import hashlib
import html
import json
import logging
import re
from collections import Counter
from datetime import datetime, timezone
from difflib import SequenceMatcher
from urllib.parse import urlparse

from ai_news_digest.deepseek_client import DeepSeekClient
from ai_news_digest.models import ClusteredStory, FinalDigestItem, RankedStory


logger = logging.getLogger(__name__)


OFFICIAL_HOST_HINTS = [
    "openai.com",
    "anthropic.com",
    "google.com",
    "deepmind.google",
    "microsoft.com",
    "aws.amazon.com",
    "cloud.google.com",
    "nvidia.com",
    "huggingface.co",
]

USEFUL_HINTS = [
    "guide", "tutorial", "how to", "how-to", "tool", "workflow", "sdk", "framework",
    "open source", "open-source", "repo", "github", "prompt", "playbook", "integration",
    "template", "benchmark", "stack", "skill", "cli", "api example",
]

NEWS_HINTS = [
    "launch", "released", "announced", "announce", "pricing", "funding", "model", "api",
    "agent", "automation", "policy", "enterprise", "partnership", "expands", "ships",
]

TOPIC_TAG_KEYWORDS = {
    "agents": ["agent", "agents", "assistant", "operator"],
    "automation": ["automation", "workflow", "make", "zapier"],
    "models": ["model", "llm", "reasoner", "multimodal"],
    "coding": ["code", "coder", "coding", "devtool", "sdk", "cli"],
    "enterprise": ["enterprise", "business", "team", "workplace", "pricing"],
}


class StoryClusterer:
    def cluster(self, rows: list) -> list[ClusteredStory]:
        clusters: list[dict] = []
        for row in rows:
            candidate = self._row_to_candidate(row)
            matched = None
            for cluster in clusters:
                if self._is_same_story(candidate, cluster):
                    matched = cluster
                    break
            if matched is None:
                clusters.append(candidate)
            else:
                self._merge_candidate(matched, candidate)
        return [self._finalize_cluster(cluster) for cluster in clusters]

    def _row_to_candidate(self, row) -> dict:
        title = self._derive_title(row)
        text = (row["text"] or "").strip()
        url = (row["url"] or "").strip()
        source_label = (row["source_title"] or f"{row['source_kind']}#{row['source_id']}").strip()
        normalized_title = self._normalize_text(title)
        domain = self._extract_domain(url)
        published_at = row["published_at"] or row["fetched_at"]
        block_hint = self._detect_block_hint(f"{title} {text}")
        tags = self._detect_topic_tags(f"{title} {text}")
        return {
            "title": title,
            "summary_text": text or title,
            "primary_url": url,
            "primary_source_kind": row["source_kind"],
            "primary_source_id": row["source_id"],
            "primary_source_label": source_label,
            "source_labels": [source_label],
            "source_domains": [domain] if domain else [],
            "raw_item_ids": [row["id"]],
            "urls": [url] if url else [],
            "published_at": published_at,
            "normalized_title": normalized_title,
            "block_hint": block_hint,
            "official_signal": any(hint in domain for hint in OFFICIAL_HOST_HINTS) if domain else False,
            "topic_tags": tags,
        }

    def _is_same_story(self, candidate: dict, cluster: dict) -> bool:
        if candidate["primary_url"] and candidate["primary_url"] in cluster["urls"]:
            return True
        if candidate["normalized_title"] == cluster["normalized_title"]:
            return True
        if candidate["normalized_title"] and cluster["normalized_title"]:
            similarity = SequenceMatcher(None, candidate["normalized_title"], cluster["normalized_title"]).ratio()
            if similarity >= 0.90:
                return True
            token_overlap = self._token_overlap(candidate["normalized_title"], cluster["normalized_title"])
            if similarity >= 0.78 and token_overlap >= 0.60:
                return True
        return False

    def _merge_candidate(self, cluster: dict, candidate: dict) -> None:
        cluster["source_labels"].extend(candidate["source_labels"])
        cluster["source_domains"].extend(candidate["source_domains"])
        cluster["raw_item_ids"].extend(candidate["raw_item_ids"])
        cluster["urls"].extend(candidate["urls"])
        cluster["topic_tags"] = sorted(set(cluster["topic_tags"]) | set(candidate["topic_tags"]))
        if candidate["official_signal"] and not cluster["official_signal"]:
            cluster["official_signal"] = True
            cluster["primary_url"] = candidate["primary_url"] or cluster["primary_url"]
            cluster["primary_source_kind"] = candidate["primary_source_kind"]
            cluster["primary_source_id"] = candidate["primary_source_id"]
            cluster["primary_source_label"] = candidate["primary_source_label"]
        if len(candidate["title"]) > len(cluster["title"]):
            cluster["title"] = candidate["title"]
        if len(candidate["summary_text"]) > len(cluster["summary_text"]):
            cluster["summary_text"] = candidate["summary_text"]
        if candidate["published_at"] and (not cluster["published_at"] or str(candidate["published_at"]) > str(cluster["published_at"])):
            cluster["published_at"] = candidate["published_at"]
        if cluster["block_hint"] != "useful" and candidate["block_hint"] == "useful":
            cluster["block_hint"] = "useful"

    def _finalize_cluster(self, cluster: dict) -> ClusteredStory:
        source_labels = sorted(set(cluster["source_labels"]))
        source_domains = sorted({domain for domain in cluster["source_domains"] if domain})
        urls = sorted({url for url in cluster["urls"] if url})
        normalized_basis = cluster["primary_url"] or cluster["normalized_title"] or cluster["title"]
        cluster_key = hashlib.sha1(normalized_basis.encode("utf-8", errors="ignore")).hexdigest()
        return ClusteredStory(
            cluster_key=cluster_key,
            title=cluster["title"],
            summary_text=cluster["summary_text"],
            primary_url=cluster["primary_url"],
            primary_source_kind=cluster["primary_source_kind"],
            primary_source_id=int(cluster["primary_source_id"]),
            primary_source_label=cluster["primary_source_label"],
            source_labels=source_labels,
            source_domains=source_domains,
            raw_item_ids=sorted(set(cluster["raw_item_ids"])),
            urls=urls,
            published_at=cluster["published_at"],
            block_hint=cluster["block_hint"],
            official_signal=cluster["official_signal"],
            topic_tags=cluster["topic_tags"],
        )

    def _derive_title(self, row) -> str:
        raw_title = (row["title"] or "").strip()
        raw_text = (row["text"] or "").strip()
        if row["source_kind"] == "telegram":
            basis = raw_text or raw_title
            basis = re.sub(r"\s+", " ", basis).strip()
            basis = re.split(r"[\n\r]+", basis)[0].strip()
            basis = re.split(r"(?<=[.!?])\s+", basis)[0].strip()
            basis = re.sub(r"^[^\wА-Яа-яЁё]+", "", basis)
            if len(basis) > 110:
                basis = basis[:107].rstrip() + "..."
            return basis or raw_title or raw_text[:110]
        return raw_title

    def _normalize_text(self, value: str) -> str:
        value = value.lower().strip()
        value = re.sub(r"https?://\S+", " ", value)
        value = re.sub(r"[^a-z0-9а-яё\s]", " ", value)
        value = re.sub(r"\s+", " ", value)
        return value

    def _extract_domain(self, url: str) -> str:
        try:
            return urlparse(url).netloc.lower().replace("www.", "")
        except Exception:
            return ""

    def _token_overlap(self, left: str, right: str) -> float:
        left_tokens = set(left.split())
        right_tokens = set(right.split())
        if not left_tokens or not right_tokens:
            return 0.0
        return len(left_tokens & right_tokens) / max(len(left_tokens), len(right_tokens))

    def _detect_block_hint(self, text: str) -> str:
        lowered = text.lower()
        if any(hint in lowered for hint in USEFUL_HINTS):
            return "useful"
        if any(hint in lowered for hint in NEWS_HINTS):
            return "news"
        return "news"

    def _detect_topic_tags(self, text: str) -> list[str]:
        lowered = text.lower()
        tags = [tag for tag, words in TOPIC_TAG_KEYWORDS.items() if any(word in lowered for word in words)]
        return sorted(tags)


class StoryRanker:
    def __init__(self, llm_client: DeepSeekClient | None = None) -> None:
        self.llm_client = llm_client

    async def rank(self, clusters: list[ClusteredStory], requested_topic: str | None = None) -> list[RankedStory]:
        heuristic_ranked = [self._heuristic_rank(cluster, requested_topic) for cluster in clusters]
        if not heuristic_ranked:
            return []
        if self.llm_client is None:
            return sorted(heuristic_ranked, key=lambda item: item.score, reverse=True)
        try:
            llm_scores = await self._rank_with_llm(heuristic_ranked, requested_topic)
            merged: list[RankedStory] = []
            for item in heuristic_ranked:
                llm_item = llm_scores.get(item.cluster_key)
                if llm_item is None:
                    merged.append(item)
                    continue
                merged.append(
                    RankedStory(
                        cluster_key=item.cluster_key,
                        title=item.title,
                        summary_text=item.summary_text,
                        primary_url=item.primary_url,
                        primary_source_label=item.primary_source_label,
                        primary_source_kind=item.primary_source_kind,
                        primary_source_id=item.primary_source_id,
                        source_labels=item.source_labels,
                        raw_item_ids=item.raw_item_ids,
                        block_type=llm_item.get("block_type", item.block_type),
                        score=float(llm_item.get("score", item.score)),
                        reason=llm_item.get("reason", item.reason),
                        topic_tags=item.topic_tags,
                    )
                )
            return sorted(merged, key=lambda ranked: ranked.score, reverse=True)
        except Exception as exc:
            logger.warning("LLM ranking failed, fallback to heuristic ranking: %s", exc)
            return sorted(heuristic_ranked, key=lambda item: item.score, reverse=True)

    def select_diverse(
        self,
        ranked_items: list[RankedStory],
        *,
        news_count: int,
        useful_count: int,
        max_per_source: int,
        relaxed_max_per_source: int,
        requested_topic: str | None = None,
    ) -> tuple[list[RankedStory], list[RankedStory]]:
        news = [item for item in ranked_items if item.block_type == "news"]
        useful = [item for item in ranked_items if item.block_type == "useful"]
        selected_news = self._pick_with_source_cap(news, news_count, max_per_source, relaxed_max_per_source)
        selected_useful = self._pick_with_source_cap(useful, useful_count, max_per_source, relaxed_max_per_source)

        if requested_topic and len(selected_useful) < useful_count:
            fallback = [item for item in ranked_items if item not in selected_news and item not in selected_useful]
            selected_useful.extend(self._pick_with_source_cap(fallback, useful_count - len(selected_useful), max_per_source, relaxed_max_per_source))
        return selected_news[:news_count], selected_useful[:useful_count]

    def _pick_with_source_cap(self, items: list[RankedStory], target: int, max_per_source: int, relaxed_max_per_source: int) -> list[RankedStory]:
        picked: list[RankedStory] = []
        counts: Counter[str] = Counter()
        for item in items:
            source = item.primary_source_label
            if counts[source] >= max_per_source:
                continue
            picked.append(item)
            counts[source] += 1
            if len(picked) >= target:
                return picked
        for item in items:
            source = item.primary_source_label
            if item in picked:
                continue
            if counts[source] >= relaxed_max_per_source:
                continue
            picked.append(item)
            counts[source] += 1
            if len(picked) >= target:
                return picked
        return picked

    def _heuristic_rank(self, cluster: ClusteredStory, requested_topic: str | None) -> RankedStory:
        text = f"{cluster.title} {cluster.summary_text}".lower()
        relevance = 35
        novelty = 30
        practical = 30
        source_trust = 90 if cluster.official_signal else 65
        if cluster.primary_source_kind == 'telegram':
            source_trust = max(source_trust, 80)
            relevance += 12
        cross_confirmation = min(100, 45 + (len(cluster.source_labels) - 1) * 15)
        freshness = self._freshness_score(cluster.published_at)
        hype_penalty = 0

        if any(keyword in text for keyword in ["agent", "automation", "workflow", "api", "model", "enterprise", "reasoner"]):
            relevance += 25
        if any(keyword in text for keyword in ["guide", "tutorial", "how to", "workflow", "tool", "sdk", "open source"]):
            practical += 30
        if any(keyword in text for keyword in ["top 10", "best prompts", "ultimate guide", "viral"]):
            hype_penalty += 15
        if any(keyword in text for keyword in ["#промо", "реклама", "sponsored", "discount", "save up to", "ticket", "final 24 hours", "final 2 days"]):
            hype_penalty += 35
            relevance -= 15
            practical -= 10
        if requested_topic:
            topic = requested_topic.lower()
            if topic in text:
                relevance += 25
                practical += 10
            else:
                shared = set(topic.split()) & set(text.split())
                relevance += min(20, len(shared) * 5)

        total = (
            0.30 * relevance
            + 0.20 * novelty
            + 0.20 * practical
            + 0.10 * source_trust
            + 0.10 * cross_confirmation
            + 0.10 * freshness
            - 0.10 * hype_penalty
        )
        return RankedStory(
            cluster_key=cluster.cluster_key,
            title=cluster.title,
            summary_text=cluster.summary_text,
            primary_url=cluster.primary_url,
            primary_source_label=cluster.primary_source_label,
            primary_source_kind=cluster.primary_source_kind,
            primary_source_id=cluster.primary_source_id,
            source_labels=cluster.source_labels,
            raw_item_ids=cluster.raw_item_ids,
            block_type=cluster.block_hint,
            score=round(total, 2),
            reason="heuristic-ranking",
            topic_tags=cluster.topic_tags,
        )

    def _freshness_score(self, published_at: str | None) -> int:
        if not published_at:
            return 20
        try:
            normalized = published_at.replace("Z", "+00:00")
            dt = datetime.fromisoformat(normalized)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            age_hours = max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0)
            if age_hours <= 6:
                return 95
            if age_hours <= 12:
                return 85
            if age_hours <= 24:
                return 70
            if age_hours <= 48:
                return 50
            return 30
        except Exception:
            return 50

    async def _rank_with_llm(self, ranked_items: list[RankedStory], requested_topic: str | None) -> dict[str, dict]:
        assert self.llm_client is not None
        system_prompt = (
            "Ты ранжируешь AI/news/tool clusters для личного русскоязычного дайджеста. "
            "Верни JSON: {\"stories\": [{\"cluster_key\": str, \"block_type\": \"news\"|\"useful\", \"score\": number, \"reason\": str}]}. "
            "Ставь более высокий score реальным AI/automation/agents/business новостям и practical materials. "
            "Избегай пустого хайпа, рерайтов и однотипных материалов из одного источника."
        )
        candidates = []
        for item in ranked_items[:30]:
            candidates.append(
                {
                    "cluster_key": item.cluster_key,
                    "title": item.title,
                    "summary": item.summary_text[:1200],
                    "source": item.primary_source_label,
                    "source_count": len(item.source_labels),
                    "block_hint": item.block_type,
                    "heuristic_score": item.score,
                    "topic_tags": item.topic_tags,
                    "url": item.primary_url,
                }
            )
        user_prompt = json.dumps(
            {
                "requested_topic": requested_topic,
                "candidates": candidates,
            },
            ensure_ascii=False,
        )
        response = await self.llm_client.json_chat(system_prompt, user_prompt, temperature=0.1, max_tokens=6000)
        stories = response.get("stories", [])
        return {story["cluster_key"]: story for story in stories if isinstance(story, dict) and "cluster_key" in story}


class DigestFormatter:
    def __init__(self, llm_client: DeepSeekClient | None = None) -> None:
        self.llm_client = llm_client

    async def render(
        self,
        *,
        title: str,
        news_items: list[RankedStory],
        useful_items: list[RankedStory],
        style_prompt: str,
        requested_topic: str | None = None,
    ) -> tuple[str, list[FinalDigestItem]]:
        if self.llm_client is None:
            return self._fallback_render(title, news_items, useful_items)
        try:
            story_by_key = {item.cluster_key: item for item in news_items + useful_items}
            payload = {
                "title": title,
                "requested_topic": requested_topic,
                "style_prompt": style_prompt,
                "news_items": [self._story_to_payload(item) for item in news_items],
                "useful_items": [self._story_to_payload(item) for item in useful_items],
            }
            system_prompt = (
                "Собери итоговый Telegram-пост на русском и верни только JSON вида "
                "{\"title\": str, \"sections\": [{\"name\": str, \"items\": [{\"cluster_key\": str, \"title\": str, \"body\": str, \"url\": str, \"source_label\": str}]}]}. "
                "Строго сохраняй блоки Новости и Полезное. Каждый пункт: короткий заголовок и ровно один абзац из 2-3 коротких предложений. "
                "В body не пиши слово Источник и не вставляй голый URL. Соблюдай style_prompt из payload."
            )
            response = await self.llm_client.json_chat(
                system_prompt,
                json.dumps(payload, ensure_ascii=False),
                model=self.llm_client.chat_model,
                temperature=0.2,
                max_tokens=7000,
            )
            sections = response.get("sections") or []
            final_items: list[FinalDigestItem] = []
            lines = [f"<b>{html.escape(response.get('title') or title)}</b>", ""]
            rank_order = 1
            for section in sections:
                section_name = (section.get("name") or "Раздел").strip()
                items = section.get("items") or []
                if not items:
                    continue
                lines.append(f"<b>{html.escape(section_name)}</b>")
                lines.append("")
                for item in items:
                    item_title = (item.get("title") or "").strip()
                    body = self._normalize_body(item.get("body") or "")
                    if not body:
                        original = story_by_key.get(item.get("cluster_key") or "")
                        if original and original.summary_text:
                            body = self._compress_summary(original.summary_text)
                    url = (item.get("url") or "").strip()
                    source_label = (item.get("source_label") or "").strip()
                    cluster_key = item.get("cluster_key") or ""
                    lines.append(f"{rank_order}. <b>{html.escape(item_title)}</b>")
                    lines.append(html.escape(body))
                    link_line = self._source_line(url)
                    if link_line:
                        lines.append(link_line)
                    lines.append("")
                    final_items.append(
                        FinalDigestItem(
                            cluster_key=cluster_key,
                            section_name=section_name,
                            title=item_title,
                            body=body,
                            url=url,
                            source_label=source_label,
                            rank_order=rank_order,
                        )
                    )
                    rank_order += 1
            if not final_items:
                return self._fallback_render(title, news_items, useful_items)
            return "\n".join(lines).strip(), final_items
        except Exception as exc:
            logger.warning("Digest formatting via LLM failed, fallback render used: %s", exc)
            return self._fallback_render(title, news_items, useful_items)

    def _fallback_render(self, title: str, news_items: list[RankedStory], useful_items: list[RankedStory]) -> tuple[str, list[FinalDigestItem]]:
        lines = [f"<b>{html.escape(title)}</b>", ""]
        final_items: list[FinalDigestItem] = []
        rank_order = 1
        for section_name, items in [("Новости", news_items), ("Полезное", useful_items)]:
            if not items:
                continue
            lines.append(f"<b>{section_name}</b>")
            lines.append("")
            for item in items:
                body = self._compress_summary(item.summary_text)
                display_title = self._compact_title(item.title)
                lines.append(f"{rank_order}. <b>{html.escape(display_title)}</b>")
                lines.append(html.escape(body))
                link_line = self._source_line(item.primary_url)
                if link_line:
                    lines.append(link_line)
                lines.append("")
                final_items.append(
                    FinalDigestItem(
                        cluster_key=item.cluster_key,
                        section_name=section_name,
                        title=item.title,
                        body=body,
                        url=item.primary_url,
                        source_label=item.primary_source_label,
                        rank_order=rank_order,
                    )
                )
                rank_order += 1
        return "\n".join(lines).strip(), final_items

    def _story_to_payload(self, item: RankedStory) -> dict:
        return {
            "cluster_key": item.cluster_key,
            "title": self._compact_title(item.title),
            "summary": item.summary_text[:900],
            "url": item.primary_url,
            "source_label": item.primary_source_label,
            "score": item.score,
            "reason": item.reason,
            "topic_tags": item.topic_tags,
        }

    def _compact_title(self, text: str, limit: int = 90) -> str:
        text = re.sub(r"\s+", " ", (text or "").strip())
        text = re.sub(r"^[^\wА-Яа-яЁё]+", "", text)
        if len(text) <= limit:
            return text
        sentence = re.split(r"(?<=[.!?])\s+", text)[0].strip()
        if sentence and len(sentence) <= limit:
            return sentence
        return text[: limit - 3].rstrip() + "..."

    def _compress_summary(self, text: str) -> str:
        normalized = self._normalize_body(text)
        if len(normalized) <= 260:
            return normalized
        sentences = re.split(r"(?<=[.!?])\s+", normalized)
        candidate = " ".join(sentences[:2]).strip()
        if candidate and len(candidate) <= 260:
            return candidate
        return normalized[:257].rstrip() + "..."

    def _normalize_body(self, text: str) -> str:
        text = re.sub(r"\s+", " ", (text or "").strip())
        text = re.sub(r"https?://\S+", "", text).strip()
        return text

    def _source_line(self, url: str) -> str:
        if not url:
            return ""
        return f'<a href="{html.escape(url, quote=True)}">Источник</a>'







