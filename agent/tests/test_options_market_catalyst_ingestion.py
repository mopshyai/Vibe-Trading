from __future__ import annotations

from datetime import datetime, timezone

from src.options_market.alpaca_news import article_to_catalyst_rows, classify_headline
from src.options_market.catalyst_store import CatalystEventStore

UTC = timezone.utc


def test_classifier_is_directional_only_for_clear_language() -> None:
    bullish = classify_headline("FDA Approves ABC Company's New Therapy")
    assert bullish["event_type"] == "regulatory"
    assert bullish["direction"] == "bullish"

    bearish = classify_headline("XYZ Cuts Guidance After Quarterly Results")
    assert bearish["event_type"] in {"earnings", "guidance"}
    assert bearish["direction"] == "bearish"

    neutral = classify_headline("Company Discusses Product Strategy at Conference")
    assert neutral["direction"] == "neutral"


def test_article_conversion_preserves_provider_timestamps_and_tags() -> None:
    rows = article_to_catalyst_rows(
        {
            "id": 123,
            "headline": "Analyst Upgrades ABC After Product Launch",
            "created_at": "2026-08-22T15:00:00Z",
            "updated_at": "2026-08-22T15:05:00Z",
            "symbols": ["ABC", "OTHER"],
            "author": "Reporter",
            "summary": "Summary",
            "url": "https://example.test/article",
        },
        requested_symbols=["ABC"],
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["symbol"] == "ABC"
    assert row["external_id"] == "123"
    assert row["published_at"] == datetime(2026, 8, 22, 15, 0, tzinfo=UTC)
    assert row["updated_at"] == datetime(2026, 8, 22, 15, 5, tzinfo=UTC)
    assert row["direction"] == "bullish"


def test_store_does_not_leak_later_article_revision_into_earlier_replay(tmp_path) -> None:
    store_path = tmp_path / "catalysts.duckdb"
    original = {
        "external_id": "article-1",
        "symbol": "ABC",
        "event_type": "news",
        "published_at": "2026-08-22T14:00:00Z",
        "updated_at": "2026-08-22T14:00:00Z",
        "direction": "neutral",
        "magnitude": 0.4,
        "source_quality": 0.8,
        "novelty": 0.8,
        "volatility_risk": 0.1,
        "headline": "ABC Announces Strategic Update",
        "source": "alpaca_news",
    }
    revised = {
        **original,
        "updated_at": "2026-08-22T16:00:00Z",
        "direction": "bearish",
        "headline": "ABC Strategic Update Includes Guidance Cut",
    }

    with CatalystEventStore(store_path) as store:
        assert store.ingest([original, revised]) == 2

        before_revision = store.events_asof(
            as_of="2026-08-22T15:00:00Z",
            symbols=["ABC"],
        )
        assert len(before_revision) == 1
        assert before_revision[0]["headline"] == original["headline"]
        assert before_revision[0]["direction"] == "neutral"

        after_revision = store.events_asof(
            as_of="2026-08-22T17:00:00Z",
            symbols=["ABC"],
        )
        assert len(after_revision) == 1
        assert after_revision[0]["headline"] == revised["headline"]
        assert after_revision[0]["direction"] == "bearish"
