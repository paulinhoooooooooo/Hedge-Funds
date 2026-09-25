import pandas as pd

import etf_btc


def test_timing_acts_the_day_after_the_flow_is_known():
    idx = pd.bdate_range("2024-01-01", periods=4)
    price = pd.Series([100.0, 110.0, 121.0, 133.1], index=idx)  # +10 % par séance
    signal = pd.Series([True, False, False, False], index=idx)  # entrées connues le soir du 1er jour
    eq = etf_btc.timing(price, signal)
    # investi seulement pendant la 2e séance (+10 %), moins les frais d'achat puis de vente
    assert 110.0 * (1 - 2 * etf_btc.COST) - 0.1 < eq.iloc[-1] < 110.1
