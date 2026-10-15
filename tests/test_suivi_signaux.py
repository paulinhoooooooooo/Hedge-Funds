from types import SimpleNamespace

import pandas as pd
import pytest

import suivi_signaux as ss


def fake_backtest(events):
    days = pd.bdate_range("2026-10-12", periods=6)
    prices = pd.DataFrame({"AAA": [10, 10, 9, 8, 8, 15.0], "BBB": [20, 20, 20, 22, 22, 22.0],
                           "CCC": [5, 5, 5, 5, 4, 4.0]}, index=days)
    journal = pd.DataFrame([{"date": days[i], "asset": a, "action": act, "price": prices.iloc[i][a]}
                            for i, a, act in events])
    return SimpleNamespace(signals=SimpleNamespace(prices=prices), journal=journal), days


def test_starts_at_zero_in_cash_and_ignores_signals_before_the_start():
    bt, days = fake_backtest([(0, "AAA", "ENTRY")])
    live = ss.follow(bt, f"{days[1]:%Y-%m-%d}", capital=1200.0, fee=0.0)
    assert live.journal.empty and live.trades.empty
    assert (live.equity == 1200.0).all()  # espèces seules : aucun rendement
    assert live.gross.iloc[-1] == 0


def test_buy_reinforce_lighten_and_sell_follow_the_founder_rules():
    bt, days = fake_backtest([
        (1, "AAA", "ENTRY"),                       # achat : un douzième du portefeuille
        (2, "AAA", "REDUCE_DISTRIBUTION_ALERT"),   # allègement : rien
        (2, "BBB", "ADD_REACCUMULATION"),          # renforcement d'une ligne non détenue : achat
        (3, "AAA", "ADD_REACCUMULATION"),          # renforcement : ligne complétée jusqu'au douzième
        (4, "CCC", "RISK_DRAWDOWN"),               # réduction du gestionnaire de risque : rien
        (5, "BBB", "EXIT_DISTRIBUTION_CONFIRMED"),  # vente : toute la ligne
    ])
    live = ss.follow(bt, f"{days[1]:%Y-%m-%d}", capital=1200.0, fee=0.0)
    moves = list(zip(live.journal["asset"], live.journal["action"]))
    assert moves == [("AAA", "ENTRY"), ("BBB", "ENTRY"), ("AAA", "ADD_REACCUMULATION"),
                     ("BBB", "EXIT_DISTRIBUTION_CONFIRMED")]
    first = live.journal.iloc[0]
    assert first["amount"] == pytest.approx(100.0) and first["price"] == 10  # 1 200 / 12 au cours du jour
    top_up = live.journal.iloc[2]
    assert top_up["amount"] < 100.0  # seulement le complément jusqu'au douzième
    held = live.trades[live.trades["exit_reason"] == "EN_COURS"]
    assert list(held["asset"]) == ["AAA"]
    sold = live.trades[live.trades["asset"] == "BBB"].iloc[0]
    assert sold["exit_reason"] == "EXIT_DISTRIBUTION_CONFIRMED" and sold["return"] == pytest.approx(0.1)
    assert live.scales.iloc[-1]["AAA"] == 1 and live.scales.iloc[-1]["BBB"] == 0


def test_a_full_line_is_not_bought_again_and_cash_limits_purchases():
    bt, days = fake_backtest([(1, "AAA", "ENTRY"), (1, "AAA", "ADD_REACCUMULATION")])
    live = ss.follow(bt, f"{days[1]:%Y-%m-%d}", capital=1200.0, fee=0.0)
    assert len(live.journal) == 1  # ligne déjà pleine : rien à acheter
    bt, days = fake_backtest([(1, a, "ENTRY") for a in ("AAA", "BBB", "CCC")])
    small = ss.follow(bt, f"{days[1]:%Y-%m-%d}", capital=1200.0, fee=0.0, lines=2)
    assert len(small.journal) == 2 and small.cash.iloc[-1] == pytest.approx(0.0)  # plus d'espèces
