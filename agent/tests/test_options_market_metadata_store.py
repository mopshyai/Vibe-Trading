from __future__ import annotations

import duckdb
import pandas as pd

from src.options_market import OptionsResearchStore

CONTRACT = "AAPL260918C00200000"
UNDERLYING = "AAPL.US"


def _definition(*, event_ts: str, available_at: str, multiplier: float = 100.0) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "contract_symbol": CONTRACT,
                "underlying": UNDERLYING,
                "event_ts": event_ts,
                "expiration": "2026-09-18T20:00:00+00:00",
                "strike": 200.0,
                "option_type": "call",
                "activation": "2026-01-02T14:30:00+00:00",
                "min_price_increment": 0.01,
                "contract_multiplier": multiplier,
                "update_action": "add",
                "raw_symbol": "AAPL  260918C00200000",
                "source": "databento:OPRA.PILLAR:definition",
                "available_at": available_at,
            }
        ]
    )


def _stat(metric: str, value: float, *, event_ts: str, available_at: str) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "contract_symbol": CONTRACT,
                "underlying": UNDERLYING,
                "event_ts": event_ts,
                "reference_ts": event_ts,
                "metric": metric,
                "value": value,
                "source": f"test:{metric}",
                "available_at": available_at,
            }
        ]
    )


def _quote() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "contract_symbol": CONTRACT,
                "underlying": UNDERLYING,
                "event_ts": "2026-08-24T19:30:00+00:00",
                "expiration": "2026-09-18T20:00:00+00:00",
                "strike": 200.0,
                "option_type": "call",
                "bid": 4.90,
                "ask": 5.00,
                "volume": None,
                "open_interest": None,
                "implied_volatility": 0.42,
                "source": "test:cbbo",
                "available_at": "2026-08-24T19:30:01+00:00",
            }
        ]
    )


def test_definition_revision_is_point_in_time() -> None:
    with OptionsResearchStore(":memory:") as store:
        store.ingest_option_definitions(
            _definition(
                event_ts="2026-01-02T14:00:00+00:00",
                available_at="2026-01-02T14:00:01+00:00",
                multiplier=100.0,
            )
        )
        store.ingest_option_definitions(
            _definition(
                event_ts="2026-06-01T14:00:00+00:00",
                available_at="2026-06-01T14:00:01+00:00",
                multiplier=150.0,
            )
        )

        before = store.option_definitions_asof(
            as_of="2026-05-01T15:00:00+00:00",
            contracts=[CONTRACT],
        )
        after = store.option_definitions_asof(
            as_of="2026-06-02T15:00:00+00:00",
            contracts=[CONTRACT],
        )

        assert before.iloc[0]["contract_multiplier"] == 100.0
        assert after.iloc[0]["contract_multiplier"] == 150.0


def test_open_interest_can_enrich_intraday_but_daily_volume_waits_until_available() -> None:
    with OptionsResearchStore(":memory:") as store:
        store.ingest_option_quotes(_quote())
        store.ingest_option_definitions(
            _definition(
                event_ts="2026-01-02T14:00:00+00:00",
                available_at="2026-01-02T14:00:01+00:00",
            )
        )
        # OI is published before the regular session and can be used intraday.
        store.ingest_option_statistics(
            _stat(
                "open_interest",
                1234,
                event_ts="2026-08-24T12:45:00+00:00",
                available_at="2026-08-24T12:45:01+00:00",
            )
        )
        # Full daily volume is not knowable until the session has completed.
        store.ingest_option_statistics(
            _stat(
                "daily_volume",
                9876,
                event_ts="2026-08-24T20:00:00+00:00",
                available_at="2026-08-24T20:00:01+00:00",
            )
        )

        intraday = store.option_quotes_with_metadata_asof(
            as_of="2026-08-24T19:45:00+00:00",
            start="2026-08-24T19:00:00+00:00",
            contracts=[CONTRACT],
        )
        post_close = store.option_quotes_with_metadata_asof(
            as_of="2026-08-24T20:30:00+00:00",
            start="2026-08-24T19:00:00+00:00",
            contracts=[CONTRACT],
        )

        assert int(intraday.iloc[0]["open_interest"]) == 1234
        assert pd.isna(intraday.iloc[0]["volume"])
        assert intraday.iloc[0]["contract_multiplier"] == 100.0
        assert int(post_close.iloc[0]["open_interest"]) == 1234
        assert int(post_close.iloc[0]["volume"]) == 9876


def test_quote_native_metadata_wins_over_published_fallback() -> None:
    quote = _quote()
    quote.loc[0, "open_interest"] = 2000
    quote.loc[0, "volume"] = 300
    with OptionsResearchStore(":memory:") as store:
        store.ingest_option_quotes(quote)
        store.ingest_option_statistics(
            _stat(
                "open_interest",
                1234,
                event_ts="2026-08-24T12:45:00+00:00",
                available_at="2026-08-24T12:45:01+00:00",
            )
        )
        store.ingest_option_statistics(
            _stat(
                "daily_volume",
                9876,
                event_ts="2026-08-24T20:00:00+00:00",
                available_at="2026-08-24T20:00:01+00:00",
            )
        )
        enriched = store.option_quotes_with_metadata_asof(
            as_of="2026-08-24T20:30:00+00:00",
            start="2026-08-24T19:00:00+00:00",
            contracts=[CONTRACT],
        )
        assert int(enriched.iloc[0]["open_interest"]) == 2000
        assert int(enriched.iloc[0]["volume"]) == 300


def test_v1_store_migrates_additively_to_v2(tmp_path) -> None:
    path = tmp_path / "research.duckdb"
    connection = duckdb.connect(str(path))
    connection.execute("CREATE TABLE research_store_meta (key VARCHAR PRIMARY KEY, value VARCHAR NOT NULL)")
    connection.execute("INSERT INTO research_store_meta VALUES ('schema_version', '1')")
    connection.close()

    with OptionsResearchStore(path) as store:
        counts = store.counts()
        assert counts["option_definitions"] == 0
        assert counts["option_statistics"] == 0
        store.ingest_option_statistics(
            _stat(
                "open_interest",
                10,
                event_ts="2026-08-24T12:45:00+00:00",
                available_at="2026-08-24T12:45:01+00:00",
            )
        )

    verify = duckdb.connect(str(path), read_only=True)
    version = verify.execute(
        "SELECT value FROM research_store_meta WHERE key = 'schema_version'"
    ).fetchone()[0]
    stats = verify.execute("SELECT COUNT(*) FROM option_statistics").fetchone()[0]
    verify.close()
    assert version == "2"
    assert stats == 1
