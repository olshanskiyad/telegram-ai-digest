from dataclasses import dataclass, field
from typing import Optional


@dataclass(slots=True)
class RawItemPayload:
    source_kind: str
    source_id: int
    external_id: str
    title: str
    text: str
    url: str
    published_at: Optional[str]
    raw_hash: str
    language: Optional[str] = None


@dataclass(slots=True)
class ClusteredStory:
    cluster_key: str
    title: str
    summary_text: str
    primary_url: str
    primary_source_kind: str
    primary_source_id: int
    primary_source_label: str
    source_labels: list[str] = field(default_factory=list)
    source_domains: list[str] = field(default_factory=list)
    raw_item_ids: list[int] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)
    published_at: Optional[str] = None
    block_hint: str = "news"
    official_signal: bool = False
    topic_tags: list[str] = field(default_factory=list)


@dataclass(slots=True)
class RankedStory:
    cluster_key: str
    title: str
    summary_text: str
    primary_url: str
    primary_source_label: str
    primary_source_kind: str
    primary_source_id: int
    source_labels: list[str]
    raw_item_ids: list[int]
    block_type: str
    score: float
    reason: str
    topic_tags: list[str] = field(default_factory=list)


@dataclass(slots=True)
class FinalDigestItem:
    cluster_key: str
    section_name: str
    title: str
    body: str
    url: str
    source_label: str
    rank_order: int
