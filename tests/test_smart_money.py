# -*- coding: utf-8 -*-
"""Tests de la liste Smart Money point-in-time et du proxy de flux à budget nul."""

import numpy as np
import pandas as pd
import pytest

import flow_backtest as fb
import smart_money as sm

Q = pd.offsets.QuarterEnd(1)


def test_manager_copycat_return():
    prices = pd.DataFrame({"A": [100.0, 110.0, 121.0], "B": [50.0, 50.0, 50.0]},
                          index=pd.to_datetime(["2019-12-31", "2020-03-31", "2020-06-30"]))
    positions = pd.DataFrame({
        "cik": ["1", "1", "1"], "asset": ["A", "B", "A"],
        "period_end": pd.to_datetime(["2019-12-31", "2019-12-31", "2020-03-31"]),
        "filing_date": pd.to_datetime(["2020-02-10", "2020-02-10", "2020-05-10"]),
        "shares": [10.0, 20.0, 10.0],
    })
    out = sm.manager_quarterly_returns(positions, prices).set_index("period_end")
    # T4 2019 : 10 A (1 000) + 20 B (1 000) -> 1 100 + 1 000 = +5 %, connu le 31/03/2020
    assert out.loc["2019-12-31", "ret"] == pytest.approx(0.05)
    assert out.loc["2019-12-31", "known_at"] == pd.Timestamp("2020-03-31")
    assert out.loc["2020-03-31", "ret"] == pytest.approx(0.10)


def _returns_panel(n_quarters=10):
    quarters = pd.date_range("2018-03-31", periods=n_quarters, freq="QE")
    rng = np.random.default_rng(0)
    rows = []
    for q in quarters:
        for cik, mu in (("good", 0.03), ("bad", -0.02), ("noisy", 0.0), ("index", 0.05)):
            rows.append({"cik": cik, "period_end": q, "known_at": q + Q,
                         "ret": mu + (rng.normal(0, 0.05) if cik == "noisy" else rng.normal(0, 0.002)),
                         "n_priced": 50})
    returns = pd.DataFrame(rows)
    positions = []
    for q in quarters:
        for cik, n in (("good", 40), ("bad", 40), ("noisy", 40), ("index", 2000)):
            positions.append(pd.DataFrame({"cik": cik, "asset": [f"S{i}" for i in range(n)], "period_end": q,
                                           "filing_date": q + pd.Timedelta(days=40), "shares": 1.0}))
    return returns, pd.concat(positions, ignore_index=True), quarters


def test_selection_ranks_skill_and_excludes_index_like_managers():
    returns, positions, quarters = _returns_panel()
    sel = sm.select_smart_money(returns, positions, lookback=4, top_n=2, min_positions=20, max_positions=1500)
    # 4 trimestres de rendements connus nécessaires : pas de liste avant
    first = sel["period_end"].min()
    assert first == quarters[4]
    at_first = sel[sel["period_end"] == first].sort_values("rank")
    assert at_first["cik"].iloc[0] == "good"
    assert "index" not in set(sel["cik"])  # quasi-indiciel exclu malgré son rendement


def test_selection_is_point_in_time():
    returns, positions, quarters = _returns_panel()
    q = quarters[6]
    base = sm.select_smart_money(returns, positions, lookback=4, top_n=2)
    future = returns["known_at"] > q
    shocked = returns.copy()
    shocked.loc[future & (shocked["cik"] == "bad"), "ret"] = 10.0  # « bad » devient génial après q
    alt = sm.select_smart_money(shocked, positions, lookback=4, top_n=2)
    pd.testing.assert_frame_equal(base[base["period_end"] <= q].reset_index(drop=True),
                                  alt[alt["period_end"] <= q].reset_index(drop=True))


def test_holdings_index_uses_constant_membership():
    q1, q2, q3 = pd.to_datetime(["2020-03-31", "2020-06-30", "2020-09-30"])

    def pos(cik, asset, q, shares):
        return {"cik": cik, "asset": asset, "period_end": q, "filing_date": q + pd.Timedelta(days=40), "shares": shares}

    positions = pd.DataFrame([
        pos("m1", "A", q1, 100), pos("m1", "A", q2, 150), pos("m1", "A", q3, 165),
        pos("m2", "A", q1, 100), pos("m2", "A", q2, 100), pos("m2", "A", q3, 10),
        pos("m3", "A", q2, 1000),  # hors liste : ignoré
        pos("m1", "B", q2, 50),  # position nouvelle
        pos("m2", "C", q1, 100),  # position soldée au T2
        pos("m2", "D", q2, 5),  # garde m2 présent au T2
    ])
    selection = pd.DataFrame({"period_end": [q2, q2, q3], "cik": ["m1", "m2", "m1"], "score": 1.0, "rank": 1})
    idx = sm.smart_money_holdings_index(positions, selection).set_index(["asset", "period_end"])["value"]
    assert idx[("A", q2)] == pytest.approx(125.0)  # (150 + 100) / (100 + 100)
    assert idx[("B", q2)] == pytest.approx(200.0)  # nouvelle position : +100 % plafonné
    assert idx[("C", q2)] == pytest.approx(5.0)  # position soldée : -95 % plancher
    assert idx[("A", q3)] == pytest.approx(137.5)  # T3 : m1 seul, 150 -> 165 = +10 %, chaîné

    # le moteur retrouve exactement ces variations, à la date de publication légale
    holdings = idx.reset_index().merge(
        sm.smart_money_holdings_index(positions, selection)[["asset", "period_end", "filing_date"]])
    cal = fb._as_ns(pd.bdate_range("2020-06-01", "2021-03-31"))
    assets = pd.DataFrame({"asset_class": ["EQUITY"] * 4, "flow_vehicle": ["V"] * 4},
                          index=pd.Index(["A", "B", "C", "D"], name="asset"))
    io, _ = fb.build_pit_institutional_change(holdings, cal, assets, fb.StrategyConfig())
    assert io.loc["2020-11-16", "A"] == pytest.approx(0.10)  # 30/09 + 45 j = 14/11 (sam.) -> lundi 16/11


def test_flow_proxy_equals_chaikin_money_flow():
    dates = pd.bdate_range("2021-01-01", periods=60)
    rows = []
    for d in dates:
        rows.append({"date": d, "vehicle": "BUY", "high": 11.0, "low": 9.0, "close": 11.0, "volume": 1000})
        rows.append({"date": d, "vehicle": "SELL", "high": 11.0, "low": 9.0, "close": 9.0, "volume": 1000})
        rows.append({"date": d, "vehicle": "MID", "high": 11.0, "low": 9.0, "close": 10.5, "volume": 1000})
    flows, aum = sm.flows_from_ohlcv(pd.DataFrame(rows))
    cmf = flows.rolling("30D").sum() / aum
    last = cmf.iloc[-1]
    assert last["BUY"] == pytest.approx(1.0)
    assert last["SELL"] == pytest.approx(-1.0)
    assert last["MID"] == pytest.approx(0.5)  # ((10.5-9) - (11-10.5)) / 2
