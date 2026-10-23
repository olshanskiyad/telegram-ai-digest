import hashlib
import html
import logging
import re
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import feedparser
import httpx

from ai_news_digest.database import Database
from ai_news_digest.models import RawItemPayload


logger = logging.getLogger(__name__)


class WebCollector:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def run_once(self) -> int:
        added = 0
        sources = await self.db.list_active_web_sources()
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            for source in sources:
                try:
                    response = await client.get(source["feed_url"])
                    response.raise_for_status()
                    entries = self._extract_entries(source["feed_url"], response.text)
                    for entry in entries:
                        title = entry["title"]
                        link = entry["link"]
                        summary = self._strip_html(entry["summary"])
                        external_id = entry["external_id"]
                        raw_hash = hashlib.sha256(
                            f"{source['id']}|{external_id}|{title}".encode("utf-8", errors="ignore")
                        ).hexdigest()

                        stored = await self.db.store_raw_item(
                            RawItemPayload(
                                source_kind="web",
                                source_id=int(source["id"]),
                                external_id=str(external_id),
                                title=title[:500] or link[:500],
                                text=summary[:5000],
                                url=link,
                                published_at=entry.get("published_at"),
                                raw_hash=raw_hash,
                                language=None,
                            ),
                            extra={"feed_url": source["feed_url"]},
                        )
                        added += 1 if stored else 0
                    await self.db.touch_web_source(int(source["id"]))
                except Exception as exc:
                    logger.warning("Web source fetch failed for %s: %s", source["feed_url"], exc)
        logger.info("Web collector finished: %s new items", added)
        return added

    def _extract_entries(self, base_url: str, text: str) -> list[dict]:
        feed = feedparser.parse(text)
        if getattr(feed, "entries", None):
            parsed_entries = []
            for entry in feed.entries:
                title = (entry.get("title") or "").strip()
                link = (entry.get("link") or "").strip()
                summary = (entry.get("summary") or entry.get("description") or "").strip()
                external_id = entry.get("id") or entry.get("guid") or link or title
                if not title and not link:
                    continue
                published_at = None
                if entry.get("published_parsed"):
                    published_at = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc).isoformat()
                elif entry.get("updated_parsed"):
                    published_at = datetime(*entry.updated_parsed[:6], tzinfo=timezone.utc).isoformat()
                parsed_entries.append(
                    {
                        "title": title,
                        "link": link,
                        "summary": summary,
                        "external_id": external_id,
                        "published_at": published_at,
                    }
                )
            if parsed_entries:
                return parsed_entries
        return self._extract_html_articles(base_url, text)

    def _extract_html_articles(self, base_url: str, text: str) -> list[dict]:
        link_matches = re.finditer(
            r'<a[^>]+href=["\'](?P<href>[^"\']+)["\'][^>]*>(?P<label>.*?)</a>',
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        parsed_base = urlparse(base_url)
        host = parsed_base.netloc.lower()
        base_path = parsed_base.path.rstrip("/")
        seen = set()
        items: list[dict] = []
        for match in link_matches:
            href = html.unescape(match.group("href")).strip()
            label = re.sub(r"<[^>]+>", " ", match.group("label"))
            label = html.unescape(re.sub(r"\s+", " ", label)).strip()
            if len(label) < 25:
                continue
            lowered_label = label.lower()
            if any(bad in lowered_label for bad in ["privacy", "cookies", "press inquiries", "support@", "search", "subscribe"]):
                continue

            absolute_url = urljoin(base_url, href)
            parsed = urlparse(absolute_url)
            abs_host = parsed.netloc.lower()
            if abs_host and host and abs_host != host:
                continue
            if absolute_url.rstrip("/") == base_url.rstrip("/"):
                continue
            if not any(token in absolute_url.lower() for token in ["/news", "/blog", "/discover", "/article", "/posts", "/source/"]):
                continue

            path_parts = [part for part in parsed.path.split("/") if part]
            if not self._looks_like_article(host, base_path, path_parts):
                continue
            if absolute_url in seen:
                continue
            seen.add(absolute_url)
            items.append(
                {
                    "title": label[:300],
                    "link": absolute_url,
                    "summary": label[:500],
                    "external_id": absolute_url,
                    "published_at": self._extract_published_at_from_text(label),
                }
            )
            if len(items) >= 30:
                break
        return items

    def _strip_html(self, text: str) -> str:
        text = re.sub(r'<[^>]+>', ' ', text)
        text = html.unescape(text)
        text = re.sub(r'\s+', ' ', text).strip()
        return text

    def _extract_published_at_from_text(self, text: str) -> str | None:
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
            return datetime(int(match.group('year')), month, int(match.group('day')), tzinfo=timezone.utc).isoformat()
        except ValueError:
            return None

    def _looks_like_article(self, host: str, base_path: str, path_parts: list[str]) -> bool:
        if len(path_parts) < 2:
            return False

        joined_path = "/" + "/".join(path_parts)
        if joined_path.rstrip("/") == base_path.rstrip("/"):
            return False

        generic_leafs = {
            "news",
            "blog",
            "discover",
            "posts",
            "post",
            "rss",
            "feed",
            "tag",
            "topics",
            "products",
            "categories",
            "page",
        }
        if path_parts[-1].lower() in generic_leafs:
            return False

        if "news.microsoft.com" in host:
            return len(path_parts) >= 4 and path_parts[0] == "source" and path_parts[1].isdigit()
        if "blogs.microsoft.com" in host:
            return len(path_parts) >= 5 and path_parts[0] == "blog"
        if "cloud.google.com" in host:
            return len(path_parts) >= 4 and path_parts[0] == "blog"
        if "deepmind.google" in host:
            return len(path_parts) >= 3 and path_parts[:2] == ["discover", "blog"]
        if "anthropic.com" in host:
            return len(path_parts) >= 2 and path_parts[0] == "news"

        return True

