from __future__ import annotations

from src.options_market.paper_execution import PaperExecutionConfig, submit_paper_option_order
from src.trading.connectors.alpaca.sdk import AlpacaConfig, PAPER_HOST


class _Response:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {
            "id": "paper-order-1",
            "symbol": "TSLA260918C00400000",
            "side": "buy",
            "type": "limit",
            "time_in_force": "day",
            "qty": "1",
            "limit_price": "2.7200",
            "status": "accepted",
            "filled_qty": "0",
        }


class _Session:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def post(self, url, *, json, headers, timeout):  # noqa: ANN001
        self.calls.append({"url": url, "json": dict(json), "headers": dict(headers), "timeout": timeout})
        return _Response()


def test_actual_paper_order_uses_paper_rest_without_alpaca_sdk(monkeypatch) -> None:
    from src.options_market import paper_execution as module

    # Make the test deterministic and force the direct REST branch.
    monkeypatch.setattr(module.tap_forward, "tap_enabled", lambda: False)
    session = _Session()
    config = AlpacaConfig(api_key="paper-key", secret_key="paper-secret", profile="paper")
    result = submit_paper_option_order(
        {
            "contract_symbol": "TSLA260918C00400000",
            "option_type": "call",
            "bid": 2.69,
            "entry_ask": 2.72,
            "target_profit_pct": 300.0,
        },
        quantity=1,
        market_is_open=True,
        ev_report={"positive_ev": True},
        walk_forward_report={"decision": "WALK_FORWARD_PASS"},
        risk_report={"approved": True},
        alpaca_config=config,
        config=PaperExecutionConfig(dry_run=False),
        session=session,
    )
    assert result["decision"] == "PAPER_ORDER_SUBMITTED"
    assert result["submitted"] is True
    assert result["broker_result"]["is_paper"] is True
    assert result["broker_result"]["via"] == "rest"
    assert len(session.calls) == 1
    call = session.calls[0]
    assert call["url"] == f"{PAPER_HOST}/v2/orders"
    assert call["json"]["symbol"] == "TSLA260918C00400000"
    assert call["json"]["side"] == "buy"
    assert call["json"]["type"] == "limit"
    assert call["json"]["time_in_force"] == "day"
    assert call["json"]["qty"] == "1"
    assert call["json"]["limit_price"] == "2.7200"
    assert call["json"]["client_order_id"].startswith("vibe-paper-")
    assert "APCA-API-KEY-ID" in call["headers"]
