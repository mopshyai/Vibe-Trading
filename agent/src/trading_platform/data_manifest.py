"""Atomic source-freshness manifest for the personal trading data plane.

This manifest answers "when did this component last successfully refresh?". It is
separate from the market-data database so historical row count cannot masquerade
as current freshness.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping

from .health import DataQualitySummary, evaluate_data_freshness

UTC = timezone.utc


class DataPlaneManifest:
    """Atomic JSON-backed component health observations."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def read(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        components = payload.get("components") if isinstance(payload, Mapping) else None
        if not isinstance(components, Mapping):
            return {}
        return {
            str(name): dict(value)
            for name, value in components.items()
            if isinstance(value, Mapping)
        }

    def mark_success(
        self,
        component: str,
        *,
        observed_at: datetime | None = None,
        source: str | None = None,
        detail: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        now = observed_at or datetime.now(UTC)
        if now.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")
        components = self.read()
        components[_component(component)] = {
            "observed_at": now.astimezone(UTC).isoformat(),
            "source": str(source or "").strip() or None,
            "detail": str(detail or "").strip() or "fresh",
            "metadata": dict(metadata or {}),
        }
        self._write(components)

    def mark_error(
        self,
        component: str,
        error: str,
        *,
        source: str | None = None,
        observed_at: datetime | None = None,
    ) -> None:
        now = observed_at or datetime.now(UTC)
        if now.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")
        components = self.read()
        components[_component(component)] = {
            "observed_at": now.astimezone(UTC).isoformat(),
            "source": str(source or "").strip() or None,
            "error": str(error).strip() or "unknown error",
        }
        self._write(components)

    def health(self, *, now: datetime | None = None) -> DataQualitySummary:
        return evaluate_data_freshness(self.read(), now=now)

    def _write(self, components: Mapping[str, Mapping[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "updated_at": datetime.now(UTC).isoformat(),
            "components": dict(components),
        }
        rendered = json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False, default=str) + "\n"
        fd, temp_name = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(rendered)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)


def _component(value: str) -> str:
    clean = str(value or "").strip().lower()
    if not clean or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for character in clean):
        raise ValueError("component must use lowercase letters, numbers, '_' or '-'")
    return clean


__all__ = ["DataPlaneManifest"]
