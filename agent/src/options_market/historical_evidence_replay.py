"""Compose point-in-time option metadata and historical IV/Greeks for replay.

The composition order is intentional:
1. raw research store quote;
2. point-in-time OPRA definition/OI/daily-volume enrichment;
3. point-in-time licensed/calculated IV/Greeks overlay;
4. existing replay/experiment engine.

No strategy rule is copied or changed here.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence

from .experiments import ExperimentLineage, HistoricalExperimentConfig, run_historical_research_experiment
from .historical_volatility_store import HistoricalOptionVolatilityStore
from .historical_volatility_view import HistoricalVolatilityResearchStoreView
from .metadata_view import MetadataAwareResearchStoreView
from .replay import ReplayConfig, replay_selection_at
from .store import OptionsResearchStore


def historical_evidence_store_view(
    store: OptionsResearchStore,
    volatility_store: HistoricalOptionVolatilityStore,
) -> HistoricalVolatilityResearchStoreView:
    """Build the read-only metadata + IV/Greeks evidence view."""
    metadata = MetadataAwareResearchStoreView(store)
    return HistoricalVolatilityResearchStoreView(metadata, volatility_store)


def replay_selection_with_historical_evidence(
    store: OptionsResearchStore,
    volatility_store: HistoricalOptionVolatilityStore,
    symbols: Sequence[str],
    *,
    research_time: datetime,
    config: ReplayConfig | None = None,
) -> dict[str, Any]:
    return replay_selection_at(
        historical_evidence_store_view(store, volatility_store),  # type: ignore[arg-type]
        symbols,
        research_time=research_time,
        config=config,
    )


def run_historical_experiment_with_historical_evidence(
    store: OptionsResearchStore,
    volatility_store: HistoricalOptionVolatilityStore,
    research_times: Iterable[datetime],
    *,
    evaluation_as_of: datetime,
    static_symbols: Sequence[str] | None = None,
    universe_snapshots: Sequence[Mapping[str, Any]] | None = None,
    replay_config: ReplayConfig | None = None,
    experiment_config: HistoricalExperimentConfig | None = None,
    lineage: ExperimentLineage | None = None,
) -> dict[str, Any]:
    return run_historical_research_experiment(
        historical_evidence_store_view(store, volatility_store),  # type: ignore[arg-type]
        research_times,
        evaluation_as_of=evaluation_as_of,
        static_symbols=static_symbols,
        universe_snapshots=universe_snapshots,
        replay_config=replay_config,
        experiment_config=experiment_config,
        lineage=lineage,
    )


__all__ = [
    "historical_evidence_store_view",
    "replay_selection_with_historical_evidence",
    "run_historical_experiment_with_historical_evidence",
]
