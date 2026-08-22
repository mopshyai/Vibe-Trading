from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
from types import ModuleType

UTC = timezone.utc
ROOT = Path(__file__).resolve().parents[2]


def _load_script(name: str) -> ModuleType:
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"test_{name}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load script module {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_exit_state = _load_script("refresh_paper_exit_state")
_lifecycle = _load_script("run_trading_platform_lifecycle_worker")
_risk_exit_signal = _exit_state._risk_exit_signal
_fill_context = _exit_state._fill_context
_should_checkpoint_attribution = _lifecycle._should_checkpoint_attribution


def test_concentration_block_does_not_force_liquidation() -> None:
    required, reasons = _risk_exit_signal(
        {
            "blocking_reasons": [
                "underlying_concentration_limit",
                "total_open_premium_risk_limit",
            ]
        }
    )
    assert required is False
    assert reasons == []


def test_true_account_kill_condition_can_propose_exit() -> None:
    required, reasons = _risk_exit_signal(
        {
            "blocking_reasons": [
                "sector_concentration_limit",
                "daily_loss_kill_threshold",
                "broker_account_trading_blocked",
            ]
        }
    )
    assert required is True
    assert reasons == ["broker_account_trading_blocked", "daily_loss_kill_threshold"]


def test_fill_context_prefers_completed_fill_over_partial() -> None:
    rows = [
        {
            "stage": "partial_fill",
            "occurred_at": "2026-08-24T14:01:00+00:00",
            "broker_order_id": "partial",
            "metadata": {},
        },
        {
            "stage": "filled",
            "occurred_at": "2026-08-24T14:02:00+00:00",
            "broker_order_id": "filled",
            "metadata": {"broker_order": {"filled_at": "2026-08-24T14:01:30+00:00"}},
        },
    ]
    context = _fill_context(rows)
    assert context["broker_order_id"] == "filled"
    assert context["filled_at"] == "2026-08-24T14:01:30+00:00"


def test_attribution_waits_until_after_5pm_et_and_runs_once(tmp_path) -> None:
    path = tmp_path / "candidate-attribution.json"
    before = datetime(2026, 8, 24, 20, 30, tzinfo=UTC)  # 16:30 EDT
    after = datetime(2026, 8, 24, 21, 30, tzinfo=UTC)   # 17:30 EDT

    assert _should_checkpoint_attribution(before, path) is False
    assert _should_checkpoint_attribution(after, path) is True

    path.write_text(json.dumps({"as_of": after.isoformat()}), encoding="utf-8")
    assert _should_checkpoint_attribution(after, path) is False
