# -*- coding: utf-8 -*-
"""Tests de l'empreinte de marché des grands acteurs (prix / volume)."""

import numpy as np
import pandas as pd
import pytest

import flow_backtest as fb
import market_footprint as mf
from test_flow_backtest import ladder_market


def _frame(values, index, name="X"):
    return pd.DataFrame({name: values}, index=index)


def test_volume_profile_finds_where_big_volume_traded():
    idx = pd.bdate_range("2022-01-03", periods=130)
    rng = np.random.default_rng(0)
    close = 100 + rng.normal(0, 5, size=len(idx))
    volume = np.full(len(idx), 1_000.0)
    heavy = np.arange(len(idx)) % 4 == 0
    close[heavy] = 92.0 + rng.normal(0, 0.2, size=heavy.sum())  # les grands acheteurs à ~92
    volume[heavy] = 50_000.0
    c = _frame(close, idx)
    poc, lo, hi = mf.rolling_volume_profile(c, _frame(volume, idx), lookback=126, bins=40)
    assert poc["X"].iloc[-1] == pytest.approx(92.0, abs=1.0)
    assert lo["X"].iloc[-1] <= 92.0 <= hi["X"].iloc[-1]
    assert np.isnan(poc["X"].iloc[100])  # pas assez d'historique avant 126 séances


def test_updown_ratio_and_distribution_days():
    idx = pd.bdate_range("2022-01-03", periods=80)
    close, volume = [100.0], [1_000.0]
    for k in range(1, len(idx)):
        down = k % 2 == 0
        close.append(close[-1] * (0.996 if down else 1.001))
        volume.append(2_000.0 if down else 1_000.0)  # les baisses se font sur fort volume
    c, v = _frame(close, idx), _frame(volume, idx)
    fp = mf.compute_footprint(c * 1.002, c * 0.998, c, v)
    assert fp.updown_ratio["X"].iloc[-1] == pytest.approx(0.5, rel=0.05)
    assert fp.distribution_days["X"].iloc[-1] >= 12
    assert bool(fp.distribution["X"].iloc[-1])
    assert not bool(fp.accumulation["X"].iloc[-1])


def test_footprint_is_a_third_exit_leg():
    """Flux sortants + empreinte vendeuse = sortie confirmée, sans attendre le 13F de mi-août."""
    data = ladder_market()
    cal = data.prices.index
    close = data.prices["EQ_A"].to_numpy().copy()
    volume = np.full(len(cal), 1e6)
    start = int(cal.searchsorted(pd.Timestamp("2020-06-01")))
    for k in range(start, len(cal)):
        down = (k - start) % 2 == 0
        close[k] = close[k - 1] * (0.996 if down else 1.001)
        volume[k] = 2e6 if down else 1e6
    data.prices["EQ_A"] = close
    data.high = pd.DataFrame({"EQ_A": close * 1.002}, index=cal)
    data.low = pd.DataFrame({"EQ_A": close * 0.998}, index=cal)
    data.volume = pd.DataFrame({"EQ_A": volume}, index=cal)

    j = fb.run_backtest(data, fb.StrategyConfig()).journal
    assert list(j["action"]) == ["ENTRY", "REDUCE_DISTRIBUTION_ALERT", "EXIT_DISTRIBUTION_CONFIRMED"]
    assert "empreinte prix / volume" in j["justification"].iloc[1]
    assert j["date"].iloc[2] < pd.Timestamp("2020-08-01")  # avant la publication du 13F du 30/06
    assert "2 jambes sur 3" in j["justification"].iloc[2]

    without = fb.run_backtest(data, fb.StrategyConfig(use_footprint=False)).journal
    assert without["date"].iloc[-1] == pd.Timestamp("2020-08-17")  # sans empreinte : on attend le 13F


def test_review_explains_the_footprint():
    data = fb.generate_synthetic_market(seed=2)
    review = fb.review_positions(fb.run_backtest(data, fb.StrategyConfig()))
    assert review["justification"].str.contains("Empreinte de marché").all()
