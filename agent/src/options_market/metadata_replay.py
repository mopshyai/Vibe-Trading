"""Metadata-aware historical replay helpers.

These functions reuse the existing selection/experiment engines while swapping
only the historical option-quote read path to the point-in-time metadata view.
No selection rule is duplicated here.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence

from .experiments import (
    ExperimentLineage,
    HistoricalExperimentConfig,
    run_historical_research_experiment,
)
from .metadata_view import MetadataAwareResearchStoreView
from .replay import ReplayConfig, replay_selection_at
from .store import OptionsResearchStore


def replay_selection_with_metadata_at(
    store: OptionsResearchStore,
    symbols: Sequence[str],
    *,
    research_time: datetime,
    config: ReplayConfig | None = None,
) -> dict[str, Any]:
    """Replay one timestamp using only metadata knowable by that timestamp."""
    return replay_selection_at(
        MetadataAwareResearchStoreView(store),  # type: ignore[arg-type]
        symbols,
        research_time=research_time,
        config=config,
    )


def run_historical_experiment_with_metadata(
    store: OptionsResearchStore,
    research_times: Iterable[datetime],
    *,
    evaluation_as_of: datetime,
    static_symbols: Sequence[str] | None = None,
    universe_snapshots: Sequence[Mapping[str, Any]] | None = None,
    replay_config: ReplayConfig | None = None,
    experiment_config: HistoricalExperimentConfig | None = None,
    lineage: ExperimentLineage | None = None,
) -> dict[str, Any]:
    """Run the multi-session experiment through the metadata-aware store view."""
    return run_historical_research_experiment(
        MetadataAwareResearchStoreView(store),  # type: ignore[arg-type]
        research_times,
        evaluation_as_of=evaluation_as_of,
        static_symbols=static_symbols,
        universe_snapshots=universe_snapshots,
        replay_config=replay_config,
        experiment_config=experiment_config,
        lineage=lineage,
    )


__all__ = [
    "replay_selection_with_metadata_at",
    "run_historical_experiment_with_metadata",
]
