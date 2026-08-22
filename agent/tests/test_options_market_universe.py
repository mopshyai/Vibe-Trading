from __future__ import annotations

from src.options_market.universe import parse_symbol_directory


NASDAQ = """Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares
AAPL|Apple Inc.|Q|N|N|100|N|N
QQQ|Invesco QQQ Trust|Q|N|N|100|Y|N
ZTEST|Test Security|Q|Y|N|100|N|N
File Creation Time: 2026082218:00|||||||
"""

OTHER = """ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol
IBM|International Business Machines|N|IBM|N|100|N|IBM
SPY|SPDR S&P 500 ETF Trust|P|SPY|Y|100|N|SPY
BRK$B|Berkshire Hathaway Class B|N|BRK.B|N|100|N|BRK-B
File Creation Time: 2026082218:00|||||||
"""


def test_parses_nasdaq_and_other_us_venues() -> None:
    listings = parse_symbol_directory(NASDAQ, OTHER)
    by_symbol = {item.project_symbol: item for item in listings}
    assert "AAPL.US" in by_symbol
    assert by_symbol["IBM.US"].exchange == "NYSE"
    assert by_symbol["SPY.US"].exchange == "NYSE Arca"
    assert "ZTEST.US" not in by_symbol


def test_etfs_can_be_excluded() -> None:
    listings = parse_symbol_directory(NASDAQ, OTHER, include_etfs=False)
    symbols = {item.project_symbol for item in listings}
    assert "QQQ.US" not in symbols
    assert "SPY.US" not in symbols
    assert "AAPL.US" in symbols
