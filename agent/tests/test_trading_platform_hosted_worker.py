from __future__ import annotations

import importlib.util
from pathlib import Path


def _module():
    root = Path(__file__).resolve().parents[2]
    path = root / "scripts" / "run_hosted_trading_platform_worker.py"
    spec = importlib.util.spec_from_file_location("hosted_worker_test_module", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_hosted_cycle_sequences_universe_then_equity_then_main_cycle(tmp_path, monkeypatch) -> None:
    module = _module()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    calls: list[str] = []

    def fake_needs(path, _now):  # noqa: ANN001
        return path.name in {"us-universe.json", "equity-daily-refresh.json"}

    def fake_run(args, *, timeout, acceptable_codes=(0,)):  # noqa: ANN001
        calls.append(args[0])
        if args[0] == "refresh_us_universe.py":
            (data_dir / "us-universe.json").write_text('{"symbols":["AAPL","TSLA"]}\n', encoding="utf-8")
        return {"status": "ok", "returncode": 0}

    def fake_original(**kwargs):  # noqa: ANN003
        calls.append("main_cycle")
        return {"platform_snapshot_published": True, "kwargs": kwargs}

    monkeypatch.setattr(module.base, "_needs_daily_refresh", fake_needs)
    monkeypatch.setattr(module.base, "_run", fake_run)
    monkeypatch.setattr(module, "_ORIGINAL_CYCLE", fake_original)
    monkeypatch.setenv("TRADING_PLATFORM_STOCK_FEED", "sip")

    result = module.hosted_cycle(
        data_dir=data_dir,
        cycle_number=1,
        option_feed="opra",
        force_full=True,
        command_timeout=60,
    )

    assert calls == ["refresh_us_universe.py", "refresh_current_equity_bars.py", "main_cycle"]
    assert result["platform_snapshot_published"] is True
