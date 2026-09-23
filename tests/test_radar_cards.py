# -*- coding: utf-8 -*-
"""Tests du radar des grands acteurs, des fiches de trade, de la page « Desk » et des acheteurs 13F."""

import numpy as np
import pandas as pd
import pytest

import dashboard as db
import flow_backtest as fb
import institutional_radar as ir
import phase1_data as p1
import trade_cards as tc


def quiet_market(n=400, seed=0):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2020-01-01", periods=n)
    close = pd.DataFrame({"X": 100 * np.cumprod(1 + rng.normal(0, 0.01, n))}, index=idx)
    volume = pd.DataFrame({"X": rng.uniform(0.9e6, 1.1e6, n)}, index=idx)
    return close * 1.01, close * 0.99, close, volume


def test_day_spike_with_buyers_in_control_is_accumulation():
    high, low, close, volume = quiet_market()
    t = close.index[-1]
    volume.loc[t, "X"] = 4e6
    high.loc[t, "X"] = close.loc[t, "X"] * 1.001  # clôture au plus haut de la séance
    radar = ir.compute_radar(high, low, close, volume)
    assert radar.views["jour"].state.loc[t, "X"] == 1.0
    assert "Jour : volume ×4" in radar.explain("X", len(close) - 1)


def test_several_timeframes_raise_an_alert():
    high, low, close, volume = quiet_market()
    last5 = close.index[-5:]
    volume.loc[last5, "X"] = 3.5e6  # programme d'achat sur une semaine
    high.loc[last5, "X"] = close.loc[last5, "X"] * 1.001
    radar = ir.compute_radar(high, low, close, volume)
    alerts = radar.alerts()
    assert list(alerts["asset"]) == ["X"] and alerts["score"].iloc[0] >= ir.ALERT_THRESHOLD
    assert alerts["sens"].iloc[0] == "accumulation"


def test_radar_does_not_look_ahead():
    high, low, close, volume = quiet_market()
    base = ir.compute_radar(high, low, close, volume).score
    cut = close.index[300]
    v2 = volume.copy()
    v2.loc[v2.index > cut] *= 10
    alt = ir.compute_radar(high, low, close, v2).score
    pd.testing.assert_frame_equal(base.loc[:cut], alt.loc[:cut])


def test_offexchange_data_is_used_the_next_day_only():
    high, low, close, volume = quiet_market()
    offx = volume * 0.4
    short = offx * 0.45
    cut = close.index[-1]
    offx2 = offx.copy()
    offx2.loc[cut] *= 3  # la donnée du jour n'est publiée que le soir
    r1 = ir.compute_radar(high, low, close, volume, offx, short)
    r2 = ir.compute_radar(high, low, close, volume, offx2, short)
    pd.testing.assert_frame_equal(r1.views["semaine"].offx_share, r2.views["semaine"].offx_share)


def test_hourly_view_compares_the_same_hour_of_day():
    rows = []
    for d in pd.bdate_range("2024-01-01", periods=21):
        for hour in range(10, 16):
            vol = 3e5 if hour == 10 else 1e5  # l'ouverture est toujours plus active
            rows.append((d + pd.Timedelta(hours=hour), 100.0, 99.0, 99.9, vol))
    bars = pd.DataFrame(rows, columns=["time", "high", "low", "close", "volume"]).set_index("time")
    view = ir.hourly_view(bars)
    assert view["volume_ratio"] == pytest.approx(1.0)  # dernière heure normale pour ce créneau
    bars.iloc[-1, bars.columns.get_loc("volume")] = 4e5
    assert ir.hourly_view(bars)["state"] == 1


@pytest.fixture(scope="module")
def run():
    data = fb.generate_synthetic_market(seed=4)
    return fb.run_backtest(data, fb.StrategyConfig())


def test_trade_cards_explain_every_decision(run):
    cards = tc.build_trade_cards(run, n=10)
    assert len(cards) == 10
    for c in cards:
        assert c.reasons[0].startswith("Déclencheur")
        assert c.history and c.status
        assert c.date >= cards[-1].date
    assert any(c.title == "ACHAT" for c in tc.build_trade_cards(run, n=60))


def test_history_uses_only_outcomes_known_at_the_date(run):
    book = tc.HistoryBook(run)
    early = run.signals.calendar[400]
    summary = book.summary("buy", "EQUITY", early)
    known = book.book[(book.book["kind"] == "buy") & (book.book["asset_class"] == "EQUITY")
                      & (book.book["horizon"] == 63)]
    assert summary[63]["n"] == int((known["known_at"] <= early).sum())
    assert summary[63]["n"] < len(known)


def test_dashboard_renders_a_complete_page(run):
    review = fb.review_positions(run)
    cards = tc.build_trade_cards(run, n=6)
    page = db.render_dashboard(run, review, cards, None, synthetic=True, label="test")
    assert page.startswith("<title>Desk Smart Money</title>")
    assert page.count('class="ticket"') == 6
    assert "Données simulées" in page and ">nan<" not in page.lower()


def test_smart_money_buyers_counts_and_names():
    q1, q2 = pd.Timestamp("2020-03-31"), pd.Timestamp("2020-06-30")
    pos = pd.DataFrame({
        "cik": ["1", "2", "3", "1", "2", "3"], "asset": ["AAA"] * 6,
        "period_end": [q1, q1, q1, q2, q2, q2], "shares": [100, 100, 100, 150, 80, 100],
        "value": [1, 1, 1, 150, 80, 100],
    })
    sel = pd.DataFrame({"period_end": [q2] * 3, "cik": ["1", "2", "3"], "score": 1.0, "rank": [1, 2, 3]})
    out = p1.smart_money_buyers(pos, sel, {"1": "BERKSHIRE HATHAWAY INC"})
    row = out.iloc[0]
    assert (row["n_buyers"], row["n_sellers"], row["n_managers"]) == (1, 1, 3)
    assert row["top_buyers"] == "Berkshire Hathaway"
    assert row["available_date"] == q2 + pd.Timedelta(days=46)


def test_finra_file_parsing_keeps_tracked_symbols():
    raw = (b"Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market\n"
           b"20260918|AAPL|6955865.9|53338|12748984.1|B,Q,N\n"
           b"20260918|BRK.B|1000|0|2000|B,Q,N\n20260918|NA|5|0|9|Q\n20260918|ZZZ|1|0|2|Q\n")
    df = p1.parse_finra_daily(raw, {"AAPL", "BRK-B", "NA"})
    assert set(df["asset"]) == {"AAPL", "BRK-B", "NA"}
    assert df.loc[df["asset"] == "AAPL", "total_volume"].iloc[0] == pytest.approx(12748984.1)
