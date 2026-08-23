"""Read-only point-in-time metadata view for historical replay.

The underlying OptionsResearchStore remains the source of truth. This view only
changes option-quote reads so missing quote-native open interest/volume/reference
fields may be filled from metadata that was already knowable at the same as-of
timestamp.

Historical implied volatility remains untouched. If the raw historical quote did
not carry IV, replay must continue to report that limitation instead of deriving
one implicitly.
"""

from __future__ import annotations

from typing import Any, Iterable

import pandas as pd

from .store import OptionsResearchStore


class MetadataAwareResearchStoreView:
    """Delegate all store operations except option quote reads.

    The wrapper is intentionally tiny so the existing replay and experiment
    engines can be exercised without duplicating their selection logic.
    """

    def __init__(self, store: OptionsResearchStore) -> None:
        self.store = store

    def equity_bars_asof(self, *args: Any, **kwargs: Any) -> pd.DataFrame:
        return self.store.equity_bars_asof(*args, **kwargs)

    def equity_frames_asof(self, *args: Any, **kwargs: Any) -> dict[str, pd.DataFrame]:
        return self.store.equity_frames_asof(*args, **kwargs)

    def option_quotes_asof(
        self,
        *,
        as_of: object,
        start: object | None = None,
        end: object | None = None,
        underlyings: Iterable[str] | None = None,
        contracts: Iterable[str] | None = None,
    ) -> pd.DataFrame:
        return self.store.option_quotes_with_metadata_asof(
            as_of=as_of,
            start=start,
            end=end,
            underlyings=underlyings,
            contracts=contracts,
        )

    def option_definitions_asof(self, *args: Any, **kwargs: Any) -> pd.DataFrame:
        return self.store.option_definitions_asof(*args, **kwargs)

    def option_statistics_asof(self, *args: Any, **kwargs: Any) -> pd.DataFrame:
        return self.store.option_statistics_asof(*args, **kwargs)

    def counts(self) -> dict[str, int]:
        return self.store.counts()


__all__ = ["MetadataAwareResearchStoreView"]
