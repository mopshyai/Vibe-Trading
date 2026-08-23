from __future__ import annotations

import duckdb
import pandas as pd

from src.options_market.store import OptionsResearchStore


def test_v1_store_migrates_without_rewriting_existing_quote_rows(tmp_path) -> None:
    path = tmp_path / "research.duckdb"
    connection = duckdb.connect(str(path))
    connection.execute("CREATE TABLE research_store_meta (key VARCHAR PRIMARY KEY, value VARCHAR NOT NULL)")
    connection.execute("INSERT INTO research_store_meta VALUES ('schema_version', '1')")
    connection.execute(
        """
        CREATE TABLE option_quotes (
            contract_symbol VARCHAR NOT NULL,
            underlying VARCHAR NOT NULL,
            event_ts TIMESTAMP NOT NULL,
            expiration TIMESTAMP NOT NULL,
            strike DOUBLE NOT NULL,
            option_type VARCHAR NOT NULL,
            bid DOUBLE NOT NULL,
            ask DOUBLE NOT NULL,
            last DOUBLE,
            volume BIGINT,
            open_interest BIGINT,
            implied_volatility DOUBLE,
            source VARCHAR NOT NULL,
            available_at TIMESTAMP NOT NULL,
            ingested_at TIMESTAMP NOT NULL
        )
        """
    )
    connection.execute(
        """
        INSERT INTO option_quotes VALUES (
            'AAPL260918C00200000', 'AAPL.US',
            TIMESTAMP '2026-08-24 19:30:00', TIMESTAMP '2026-09-18 20:00:00',
            200.0, 'call', 4.9, 5.0, NULL, NULL, NULL, 0.42,
            'legacy:test', TIMESTAMP '2026-08-24 19:30:01', CURRENT_TIMESTAMP
        )
        """
    )
    connection.close()

    with OptionsResearchStore(path) as store:
        counts = store.counts()
        assert counts["option_quotes"] == 1
        assert counts["option_definitions"] == 0
        assert counts["option_statistics"] == 0
        store.ingest_option_statistics(
            pd.DataFrame(
                [
                    {
                        "contract_symbol": "AAPL260918C00200000",
                        "underlying": "AAPL.US",
                        "event_ts": "2026-08-24T12:45:00+00:00",
                        "reference_ts": "2026-08-23T20:00:00+00:00",
                        "metric": "open_interest",
                        "value": 1234,
                        "source": "migration:test",
                        "available_at": "2026-08-24T12:45:01+00:00",
                    }
                ]
            )
        )

    verify = duckdb.connect(str(path), read_only=True)
    version = verify.execute(
        "SELECT value FROM research_store_meta WHERE key = 'schema_version'"
    ).fetchone()[0]
    quote_count = verify.execute("SELECT COUNT(*) FROM option_quotes").fetchone()[0]
    stat_count = verify.execute("SELECT COUNT(*) FROM option_statistics").fetchone()[0]
    verify.close()

    assert version == "2"
    assert quote_count == 1
    assert stat_count == 1
