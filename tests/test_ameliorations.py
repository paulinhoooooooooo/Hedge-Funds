# -*- coding: utf-8 -*-
"""Tests des améliorations de l'analyse (calibrage, météo du marché, comptes, vrais flux, risques)."""

import numpy as np
import pandas as pd
import pytest

import ameliorations as am
import flow_backtest as fb


@pytest.fixture(scope="module")
def market():
    return fb.generate_synthetic_market(start="2014-01-01", end="2021-12-31", seed=5)


@pytest.fixture(scope="module")
def engine_dir(tmp_path_factory, market):
    d = tmp_path_factory.mktemp("engine")
    fb.export_market_to_csv(market, d)
    cal = market.prices.index
    vix = pd.Series(15.0, index=cal)
    vix.loc["2018-02-01":"2018-03-31"] = 35.0
    macro = pd.DataFrame({"series": "VIXCLS", "date": cal, "available": cal + pd.Timedelta(days=1), "value": vix.values})
    macro.to_csv(d / "macro.csv", index=False)
    eq = [a for a in market.assets.index if market.assets.at[a, "asset_class"] == "EQUITY"]
    rows = [(a, t, 0.001 if a.endswith("_1") else 0.05) for a in eq for t in cal[::5]]
    pd.DataFrame(rows, columns=["asset", "date", "earnings_yield"]).to_csv(d / "fundamentals.csv", index=False)
    fl = market.flows.rename_axis("date").reset_index().melt(id_vars="date", var_name="vehicle", value_name="net_flow")
    au = market.aum.rename_axis("date").reset_index().melt(id_vars="date", var_name="vehicle", value_name="aum")
    fl.merge(au, on=["date", "vehicle"]).to_csv(d / "etf_flows_real.csv", index=False)
    return d


def test_calibration_uses_only_known_outcomes(market):
    sig = fb.compute_signals(market, fb.StrategyConfig())
    base, table = am.calibrate(sig)
    assert table["année"].min() >= sig.calendar[0].year + 2
    cutoff = sig.calendar[1200]
    prices = market.prices.copy()
    prices.loc[prices.index > cutoff] *= np.exp(np.cumsum(np.random.default_rng(1).normal(0, 0.05, (
        (prices.index > cutoff).sum(), prices.shape[1])), axis=0))
    alt_sig = fb.compute_signals(fb.MarketData(**{**market.__dict__, "prices": prices}), fb.StrategyConfig())
    alt, _ = am.calibrate(alt_sig)
    pd.testing.assert_frame_equal(base.loc[:cutoff], alt.loc[:cutoff])


def test_market_stress_blocks_entries_and_caps_exposure(market, engine_dir, monkeypatch):
    monkeypatch.setattr(am, "REGIME_GROSS", 0.3)  # exposition du moment : ~55 %
    sig = fb.compute_signals(market, fb.StrategyConfig())
    macro = am.load_macro(engine_dir / "macro.csv")
    stressed = am.apply_regime(sig, macro)
    day = pd.Timestamp("2018-03-01")
    assert not stressed.entry.loc[day].any() and stressed.gross_cap.loc[day] == am.REGIME_GROSS
    assert stressed.gross_cap.loc["2018-01-10"] == np.inf
    result = fb.run_backtest(market, fb.StrategyConfig(), signals=stressed)
    assert result.weights.loc["2018-03-01":"2018-03-30"].sum(axis=1).max() <= am.REGIME_GROSS * 1.15 + 0.02
    assert (result.journal["action"] == "RISK_REGIME").any()


def test_fundamental_filter_refuses_expensive_companies(market, engine_dir):
    sig = fb.compute_signals(market, fb.StrategyConfig())
    ey = am.load_fundamentals(engine_dir / "fundamentals.csv", sig)
    filtered = am.apply_fundamentals(sig, ey)
    expensive = [a for a in sig.entry.columns if a.startswith("EQ_") and a.endswith("_1")]
    assert not filtered.entry[expensive].iloc[10:].any().any()
    assert filtered.entry["FX_EURUSD"].equals(sig.entry["FX_EURUSD"])  # pas de comptes : pas de filtre


def test_volatility_target_and_correlation_limit(market):
    base = fb.run_backtest(market, fb.StrategyConfig())
    risk = fb.run_backtest(market, fb.StrategyConfig(target_vol=0.05, max_entry_correlation=0.3))
    assert (risk.journal["action"] == "RISK_VOL").any()
    assert risk.equity.pct_change().std() < base.equity.pct_change().std()


def test_all_variants_run_on_three_stocks(market, engine_dir):
    data = fb.load_market_from_csv(engine_dir)
    small = am.subset(data, ["EQ_TECH_1", "EQ_SANTE_2", "EQ_ENERGIE_3"])
    table, details = am.run_variants(small, engine_dir, fb.StrategyConfig())
    assert len(table) == 11
    tested = table[table["note"].isna()] if "note" in table else table
    assert len(tested) == 8 and np.isfinite(tested["Sharpe"]).all()
    assert "| 3 · Options | non testable" in am.format_table(table)
