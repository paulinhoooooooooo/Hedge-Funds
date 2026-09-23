# -*- coding: utf-8 -*-
"""Tests du moteur de flux : point-in-time, règles de sortie, métriques, données réelles."""

from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

import flow_backtest as fb


# ---------------------------------------------------------------------------
# Jeux de données
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def synthetic():
    return fb.generate_synthetic_market(seed=3)


def ladder_market(extra_flow_shock=None) -> fb.MarketData:
    """Un actif, trajectoire contrôlée : accumulation 2019, flux sortants juin 2020,
    baisse de la détention au 13F du 30/06/2020 (publié mi-août)."""
    cal = fb._as_ns(pd.bdate_range("2018-06-01", "2021-06-30"))
    t = np.arange(len(cal))
    prices = pd.DataFrame({"EQ_A": 100 * 1.0002 ** t * (1 + 0.002 * np.sin(t))}, index=cal)
    quarter_ends = pd.to_datetime([
        "2018-06-30", "2018-09-30", "2018-12-31", "2019-03-31", "2019-06-30", "2019-09-30",
        "2019-12-31", "2020-03-31", "2020-06-30", "2020-09-30", "2020-12-31", "2021-03-31"])
    values = [100, 100, 100, 110, 120, 130, 140, 150, 140, 130, 120, 110]
    holdings = pd.DataFrame({"asset": "EQ_A", "period_end": quarter_ends,
                             "filing_date": quarter_ends + pd.Timedelta(days=30), "value": values})
    aum = pd.DataFrame({"ETF_A": 1e9}, index=cal)
    ratio = np.where(cal < pd.Timestamp("2020-06-01"), 0.001, -0.002)
    flows = pd.DataFrame({"ETF_A": ratio * 1e9}, index=cal)
    if extra_flow_shock is not None:
        flows.loc[pd.Timestamp(extra_flow_shock), "ETF_A"] = -1e5
    assets = pd.DataFrame({"asset_class": ["EQUITY"], "flow_vehicle": ["ETF_A"]}, index=pd.Index(["EQ_A"], name="asset"))
    return fb.MarketData(prices=prices, holdings=holdings, flows=flows, aum=aum, assets=assets)


# ---------------------------------------------------------------------------
# Point-in-time
# ---------------------------------------------------------------------------

def test_13f_invisible_before_legal_deadline():
    data = ladder_market()
    cal = data.prices.index
    io, pe = fb.build_pit_institutional_change(data.holdings, cal, data.assets, fb.StrategyConfig())
    # Rapport du 31/03/2019 déposé le 30/04 : visible seulement après l'échéance (15/05) + 1 jour
    assert io.loc["2019-05-15", "EQ_A"] == pytest.approx(0.0)  # encore le rapport de décembre
    assert pe.loc["2019-05-15", "EQ_A"] == pd.Timestamp("2018-12-31")
    assert io.loc["2019-05-16", "EQ_A"] == pytest.approx(0.10)
    assert pe.loc["2019-05-16", "EQ_A"] == pd.Timestamp("2019-03-31")


def test_biased_mode_reveals_report_at_period_end():
    data = ladder_market()
    cfg = fb.StrategyConfig(ignore_publication_lags=True)
    io, _ = fb.build_pit_institutional_change(data.holdings, data.prices.index, data.assets, cfg)
    assert io.loc["2019-04-01", "EQ_A"] == pytest.approx(0.10)


def test_late_filing_for_older_period_is_ignored_and_stale_data_masked():
    data = ladder_market()
    h = data.holdings.copy()
    # un rapport ancien (30/09/2018) re-déposé très tard ne doit pas écraser une donnée plus récente
    late = pd.DataFrame({"asset": ["EQ_A"], "period_end": [pd.Timestamp("2018-09-30")],
                         "filing_date": [pd.Timestamp("2019-07-01")], "value": [50.0]})
    h = pd.concat([h[h["period_end"] <= "2019-03-31"], late], ignore_index=True)
    io, pe = fb.build_pit_institutional_change(h, data.prices.index, data.assets, fb.StrategyConfig())
    assert pe.loc["2019-07-02", "EQ_A"] == pd.Timestamp("2019-03-31")
    # plus aucun rapport après le T1 2019 : la donnée devient périmée
    assert np.isnan(io.loc["2020-06-01", "EQ_A"])


def test_no_lookahead_by_perturbing_the_future(synthetic):
    """Modifier tout ce qui n'était pas publié à la date T ne doit rien changer jusqu'à T."""
    cfg = fb.StrategyConfig()
    base = fb.run_backtest(synthetic, cfg)
    cal = base.equity.index
    cutoff = cal[2000]
    rng = np.random.default_rng(0)

    prices = synthetic.prices.copy()
    future = prices.index > cutoff
    shock = np.exp(np.cumsum(rng.normal(0, 0.03, size=(future.sum(), prices.shape[1])), axis=0))
    prices.loc[future] *= shock
    high, low, volume = synthetic.high.copy(), synthetic.low.copy(), synthetic.volume.copy()
    high.loc[future] *= shock * 1.05
    low.loc[future] *= shock * 0.95
    volume.loc[future] *= rng.uniform(0.1, 10.0, size=(future.sum(), volume.shape[1]))
    offx, offx_short = synthetic.offexchange.copy(), synthetic.offexchange_short.copy()
    offx.loc[offx.index >= cutoff] *= 5.0  # la donnée hors bourse du jour J n'est connue que le soir
    offx_short.loc[offx_short.index >= cutoff] *= 0.1
    flows = synthetic.flows.copy()
    aum = synthetic.aum.copy()
    flows.loc[flows.index >= cutoff] = rng.normal(0, 1e8, size=flows.loc[flows.index >= cutoff].shape)
    aum.loc[aum.index >= cutoff] *= 3.0
    h = synthetic.holdings.copy()
    lags = synthetic.assets.loc[h["asset"], "asset_class"].map(lambda c: fb.ASSET_CLASS_SPECS[c].statutory_lag_days).to_numpy()
    legal = h["period_end"] + pd.to_timedelta(lags, unit="D")
    available = legal.where(legal > h["filing_date"], h["filing_date"]) + pd.Timedelta(days=cfg.processing_lag_days)
    unpublished = available > cutoff
    h.loc[unpublished, "value"] *= rng.uniform(0.5, 1.5, size=unpublished.sum())

    # Indices du radar : tout ce qui est publié à partir de T est modifié (utilisé au plus tôt en T+1)
    ex = synthetic.extras
    ats = ex.ats.copy()
    late = ats["published"] >= cutoff
    ats.loc[late, ["ats_volume", "block_volume", "bank_volume"]] *= rng.uniform(0.1, 10.0, size=(late.sum(), 1))
    short = ex.short_interest.copy()
    late = short["available"] >= cutoff
    short.loc[late, "short_qty"] *= rng.uniform(0.1, 10.0, size=late.sum())
    insiders = ex.insiders.copy()
    insiders.loc[insiders["filing_date"] >= cutoff, "value"] *= 100.0
    filings = ex.filings_5pct.copy()
    filings.loc[filings["filing_date"] >= cutoff, "form"] = "SC 13D"
    blocks = ex.blocks.copy()
    late = blocks["date"] > cutoff  # blocs d'une séance connus 15 minutes après sa clôture
    blocks.loc[late, "side"] = -blocks.loc[late, "side"]
    blocks.loc[late, "notional"] *= 20.0
    extras = fb.RadarExtras(ats=ats, short_interest=short, insiders=insiders, filings_5pct=filings,
                            earnings=ex.earnings, blocks=blocks)  # dates de résultats : annoncées à l'avance

    perturbed = fb.MarketData(prices=prices, holdings=h, flows=flows, aum=aum, assets=synthetic.assets,
                              high=high, low=low, volume=volume, offexchange=offx, offexchange_short=offx_short,
                              extras=extras)
    alt = fb.run_backtest(perturbed, cfg)
    pd.testing.assert_series_equal(base.equity.loc[:cutoff], alt.equity.loc[:cutoff])
    j1 = base.journal[base.journal["date"] <= cutoff].reset_index(drop=True)
    j2 = alt.journal[alt.journal["date"] <= cutoff].reset_index(drop=True)
    pd.testing.assert_frame_equal(j1, j2)
    # sanity : la perturbation change bien l'après-T
    assert not np.allclose(base.equity.loc[cutoff:].to_numpy(), alt.equity.loc[cutoff:].to_numpy())

    r1 = fb.review_positions(base, date=cutoff, include_all=True)
    r2 = fb.review_positions(alt, date=cutoff, include_all=True)
    pd.testing.assert_frame_equal(r1, r2)


# ---------------------------------------------------------------------------
# Règles d'entrée / de sortie
# ---------------------------------------------------------------------------

def test_exit_ladder_entry_then_reduce_then_confirmed_exit():
    result = fb.run_backtest(ladder_market(), fb.StrategyConfig())
    j = result.journal
    assert list(j["action"]) == ["ENTRY", "REDUCE_DISTRIBUTION_ALERT", "EXIT_DISTRIBUTION_CONFIRMED"]
    assert j["date"].iloc[0] == pd.Timestamp("2019-05-16")  # jour de disponibilité du 13F
    assert pd.Timestamp("2020-06-10") <= j["date"].iloc[1] <= pd.Timestamp("2020-07-10")
    assert j["date"].iloc[2] == pd.Timestamp("2020-08-17")  # 13F du 30/06 publié le 14/08 + 1 j
    trades = result.trades
    assert len(trades) == 1
    assert trades.loc[0, "exit_reason"] == "EXIT_DISTRIBUTION_CONFIRMED"
    assert trades.loc[0, "first_fill"] == pd.Timestamp("2019-05-17")  # exécution le lendemain du signal
    assert result.weights["EQ_A"].iloc[-1] == 0.0
    # exécution fractionnée : l'entrée est achevée après execution_days séances
    w = result.weights["EQ_A"]
    assert 0 < w.loc["2019-05-17"] < w.loc["2019-05-23"]
    assert w.loc["2019-05-23"] == pytest.approx(w.loc["2019-05-24"], rel=0.01)


def test_min_holding_period_blocks_early_flow_exit():
    cfg = fb.StrategyConfig(min_holding_days=500)
    j = fb.run_backtest(ladder_market(), cfg).journal
    # aucune sortie « flux » moins de 500 jours après l'entrée
    assert (j["date"].iloc[1:] - j["date"].iloc[0]).dt.days.min() >= 500


def test_consecutive_rule_is_stricter_than_cumulative():
    data = ladder_market(extra_flow_shock="2019-06-03")
    cum = fb.compute_signals(data, fb.StrategyConfig(flow_rule="cumulative"))
    con = fb.compute_signals(data, fb.StrategyConfig(flow_rule="consecutive"))
    assert cum.entry.loc["2019-06-10", "EQ_A"]
    assert not con.entry.loc["2019-06-10", "EQ_A"]


def test_risk_stop_fires_independently_of_flows():
    data = ladder_market()
    crash = data.prices.index >= pd.Timestamp("2019-09-02")
    data.prices.loc[crash, "EQ_A"] *= 0.6
    j = fb.run_backtest(data, fb.StrategyConfig()).journal
    assert "RISK_STOP" in set(j["action"])
    assert j.loc[j["action"] == "RISK_STOP", "date"].iloc[0] == pd.Timestamp("2019-09-02")


def test_long_only_no_leverage(synthetic):
    r = fb.run_backtest(synthetic, fb.StrategyConfig())
    assert (r.weights >= -1e-12).all().all()
    assert r.gross.max() <= 1.01  # tolérance : coûts et variations de prix pendant l'exécution


def test_config_validation():
    with pytest.raises(ValueError):
        fb.StrategyConfig(flow_rule="daily")
    with pytest.raises(ValueError):
        fb.StrategyConfig(exit_io_change=0.01)


# ---------------------------------------------------------------------------
# Métriques
# ---------------------------------------------------------------------------

def test_drawdown_and_recovery_time():
    idx = pd.bdate_range("2020-01-01", periods=6)
    eq = pd.Series([100, 120, 90, 95, 130, 125], index=idx, dtype=float)
    m = fb.compute_metrics(eq)
    assert m["max_drawdown"] == pytest.approx(-0.25)
    assert m["mdd_peak_date"] == idx[1] and m["mdd_trough_date"] == idx[2]
    assert m["mdd_recovery_date"] == idx[4]
    assert m["recovery_time_days"] == (idx[4] - idx[2]).days
    assert m["mdd_duration_days"] == (idx[4] - idx[1]).days
    assert m["longest_underwater_days"] == (idx[4] - idx[1]).days


def test_sharpe_sortino_definitions():
    idx = pd.date_range("2020-01-01", periods=366, freq="D")
    rng = np.random.default_rng(1)
    r = rng.normal(0.0005, 0.01, size=365)
    eq = pd.Series(100 * np.cumprod(np.r_[1.0, 1 + r]), index=idx)
    m = fb.compute_metrics(eq, risk_free_rate=0.0)
    ppy = 365 / (365 / 365.25)
    assert m["sharpe"] == pytest.approx(r.mean() / r.std(ddof=1) * np.sqrt(ppy), rel=1e-9)
    downside = np.sqrt(np.mean(np.minimum(r, 0) ** 2))
    assert m["sortino"] == pytest.approx(r.mean() / downside * np.sqrt(ppy), rel=1e-9)


def test_unrecovered_drawdown_reported_as_none():
    idx = pd.bdate_range("2020-01-01", periods=4)
    m = fb.compute_metrics(pd.Series([100.0, 110.0, 80.0, 90.0], index=idx))
    assert m["recovery_time_days"] is None
    assert m["current_underwater_days"] == (idx[3] - idx[1]).days


# ---------------------------------------------------------------------------
# Données réelles
# ---------------------------------------------------------------------------

def test_csv_roundtrip_reproduces_backtest(synthetic, tmp_path):
    fb.export_market_to_csv(synthetic, tmp_path)
    loaded = fb.load_market_from_csv(tmp_path)
    a = fb.run_backtest(synthetic, fb.StrategyConfig())
    b = fb.run_backtest(loaded, fb.StrategyConfig())
    np.testing.assert_allclose(a.equity.to_numpy(), b.equity.to_numpy(), rtol=1e-9)


def test_sec_13f_aggregation_is_point_in_time(tmp_path):
    (tmp_path / "SUBMISSION.tsv").write_text(
        "ACCESSION_NUMBER\tFILING_DATE\tSUBMISSIONTYPE\tCIK\tPERIODOFREPORT\n"
        "A1\t10-MAY-2024\t13F-HR\t0000001\t31-MAR-2024\n"
        "A2\t14-MAY-2024\t13F-HR\t0000002\t31-MAR-2024\n"
        "A3\t20-JUN-2024\t13F-HR\t0000003\t31-MAR-2024\n"  # dépôt tardif : exclu
        "A4\t01-JUN-2024\t13F-HR/A\t0000001\t31-MAR-2024\n"  # amendement : exclu
    )
    (tmp_path / "INFOTABLE.tsv").write_text(
        "ACCESSION_NUMBER\tNAMEOFISSUER\tCUSIP\tVALUE\tSSHPRNAMT\tSSHPRNAMTTYPE\tPUTCALL\n"
        "A1\tAPPLE INC\t037833100\t1000\t100\tSH\t\n"
        "A1\tAPPLE INC\t037833100\t500\t50\tSH\tCall\n"  # option : exclue
        "A2\tAPPLE INC\t037833100\t2000\t200\tSH\t\n"
        "A2\tSOME BOND\t999999999\t10\t10\tPRN\t\n"
        "A3\tAPPLE INC\t037833100\t9999\t999\tSH\t\n"
        "A4\tAPPLE INC\t037833100\t9999\t999\tSH\t\n"
    )
    out = fb.build_13f_holdings_from_sec([tmp_path], {"037833100": "AAPL"})
    assert len(out) == 1
    row = out.iloc[0]
    assert row["asset"] == "AAPL" and row["value"] == 300 and row["n_filers"] == 2
    assert row["period_end"] == pd.Timestamp("2024-03-31")
    assert row["filing_date"] == pd.Timestamp("2024-05-14")
    only_ref = fb.build_13f_holdings_from_sec([tmp_path], {"037833100": "AAPL"}, reference_ciks=[1])
    assert only_ref.iloc[0]["value"] == 100


# ---------------------------------------------------------------------------
# Revue des positions
# ---------------------------------------------------------------------------

def test_review_matrix_covers_all_active_lines(synthetic):
    r = fb.run_backtest(synthetic, fb.StrategyConfig())
    review = fb.review_positions(r)
    active = set(r.scales.columns[r.scales.iloc[-1] > 0])
    assert set(review["asset"]) == active
    for lbl in ("J+5", "M+3", "A+1"):
        p = review[f"p_up_{lbl}"].dropna()
        assert ((p >= 0) & (p <= 1)).all()
    assert review["exit_signal"].str.match(r"^(CONSERVER|ALLÈGEMENT|VENTE|RENFORCER)").all()
    assert (review["justification"].str.len() > 50).all()


def test_bias_mode_runs(synthetic):
    cfg = fb.StrategyConfig()
    biased = fb.run_backtest(synthetic, replace(cfg, ignore_publication_lags=True))
    assert np.isfinite(biased.metrics["sharpe"])
