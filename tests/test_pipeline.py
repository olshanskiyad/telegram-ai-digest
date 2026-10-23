from ai_news_digest.content_filters import looks_promotional_text
from ai_news_digest.models import RankedStory
from ai_news_digest.pipeline.ranking import StoryClusterer, StoryRanker


def row(item_id, title, url, source="Source A", kind="web", text=""):
    return {
        "id": item_id,
        "title": title,
        "text": text,
        "url": url,
        "source_title": source,
        "source_kind": kind,
        "source_id": item_id,
        "published_at": "2026-10-22T06:00:00+00:00",
        "fetched_at": "2026-10-22T06:05:00+00:00",
    }


def story(key, source, block="news", score=50.0):
    return RankedStory(
        cluster_key=key, title=key, summary_text=key, primary_url=f"https://example.com/{key}",
        primary_source_label=source, primary_source_kind="web", primary_source_id=1,
        source_labels=[source], raw_item_ids=[1], block_type=block, score=score, reason="test",
    )


def test_same_url_from_two_sources_is_one_story():
    clusters = StoryClusterer().cluster([
        row(1, "OpenAI releases a new model", "https://example.com/post", "Source A"),
        row(2, "New OpenAI model is out", "https://example.com/post", "Source B"),
    ])
    assert len(clusters) == 1
    assert set(clusters[0].source_labels) == {"Source A", "Source B"}


def test_different_stories_stay_separate():
    clusters = StoryClusterer().cluster([
        row(1, "OpenAI releases a new model", "https://example.com/a"),
        row(2, "Robot vacuum learns to climb stairs", "https://example.com/b"),
    ])
    assert len(clusters) == 2


def test_source_cap_keeps_digest_diverse():
    items = [story("a1", "A"), story("a2", "A"), story("a3", "A"), story("b1", "B"), story("c1", "C")]
    news, useful = StoryRanker().select_diverse(
        items, news_count=3, useful_count=0, max_per_source=1, relaxed_max_per_source=2,
    )
    assert [s.cluster_key for s in news] == ["a1", "b1", "c1"]
    assert useful == []


def test_source_cap_relaxes_when_not_enough_sources():
    items = [story("a1", "A"), story("a2", "A"), story("a3", "A"), story("b1", "B")]
    news, _ = StoryRanker().select_diverse(
        items, news_count=4, useful_count=0, max_per_source=1, relaxed_max_per_source=2,
    )
    assert [s.cluster_key for s in news] == ["a1", "b1", "a2"]


def test_promo_posts_are_detected():
    assert looks_promotional_text("Скидка 50% на курс по промокоду AI2026, места ограничены")
    assert not looks_promotional_text("Anthropic released a new model with a longer context window")
