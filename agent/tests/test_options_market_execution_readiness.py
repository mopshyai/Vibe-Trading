from __future__ import annotations

from src.options_market.personal_service import build_execution_readiness


def test_trade_ready_candidate_enters_supervised_paper_runtime_check() -> None:
    result = build_execution_readiness(
        {
            "decision": "TRADE_READY_RESEARCH",
            "candidates": [
                {
                    "decision": "TRADE_READY_RESEARCH",
                    "contract_symbol": "TSLA260918C00400000",
                }
            ],
        }
    )
    assert result["mode"] == "supervised_paper"
    assert result["status"] == "PAPER_RUNTIME_CHECK_REQUIRED"
    assert result["eligible_contracts"] == ["TSLA260918C00400000"]
    assert result["automatic_submission"] is False
    assert result["paper_submit_confirmation_required"] is True


def test_no_ready_candidate_is_explicitly_blocked_not_execution_none() -> None:
    result = build_execution_readiness(
        {
            "decision": "WATCH",
            "candidates": [{"decision": "WATCH", "contract_symbol": "TSLA260918C00400000"}],
        }
    )
    assert result["mode"] == "supervised_paper"
    assert result["status"] == "BLOCKED_BY_RESEARCH_GATES"
    assert result["eligible_candidates"] == 0
    assert result["reasons"] == ["no_TRADE_READY_RESEARCH_candidate"]
