import numpy as np
import pandas as pd

import pelosi

PTR = """SP Bloom Energy Corporation Class A
Common Stock (BE) [ST]
P 07/24/2026 07/24/2026 $1,000,001 -
$5,000,000
F S : New D : Purchased 10,000 shares.
SP alphabet Inc. - Cl ass a (googl)[oP]
P
02/27/2020 02/27/2020 $500,001 - $1,000,000
SP Walt Disney Company (DIS) [OP] S 09/16/2022 09/16/2022 $1.00 F S : New
SP Apple Inc. (AAPL) [ST] S (partial) 12/31/2024 12/31/2024 $1,000,001 - $5,000,000"""


def test_parse_ptr_reads_both_layouts():
    rows = pelosi.parse_ptr(PTR)
    assert [(r["ticker"], r["kind"], r["type"]) for r in rows] == [
        ("BE", "ST", "BUY"), ("GOOGL", "OP", "BUY"), ("DIS", "OP", "SELL"), ("AAPL", "ST", "SELL_PARTIAL")]
    assert rows[0]["amount"] == 3_000_000.5 and rows[2]["amount"] == 1.0
    assert rows[1]["tx_date"] == pd.Timestamp("2020-02-27")


def test_copy_portfolio_waits_for_publication_and_follows_sales():
    cal = pd.bdate_range("2024-01-01", periods=10)
    prices = pd.DataFrame({"AAA": 100 * 1.01 ** np.arange(10)}, index=cal)
    tx = pd.DataFrame({"ticker": ["AAA", "AAA"], "kind": ["ST", "ST"], "type": ["BUY", "SELL"],
                       "tx_date": [cal[0], cal[5]], "amount": [1e6, 1e6],
                       "filing_date": [cal[2], cal[6]]})
    copy = pelosi.copy_portfolio(tx, prices, "filing")
    own = pelosi.copy_portfolio(tx, prices, "transaction")
    assert copy.positions.iloc[2] == 0 and copy.positions.iloc[3] == 1  # achat le lendemain du dépôt
    assert copy.positions.iloc[-1] == 0  # vente totale après le second dépôt
    assert own.equity.iloc[-1] > copy.equity.iloc[-1]  # elle achète plus tôt : la copie gagne moins
    live = pelosi.copy_portfolio(tx, prices, "filing", start=cal[5])
    assert live.trades.empty or (live.trades["action"] != "ACHAT").all()
