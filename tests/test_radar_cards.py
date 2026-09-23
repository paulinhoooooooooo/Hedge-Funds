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


# ---------------------------------------------------------------------------
# Indices complémentaires du radar
# ---------------------------------------------------------------------------

def test_event_days_are_not_counted_in_the_day_timeframe():
    high, low, close, volume = quiet_market()
    t = close.index[-2]
    volume.loc[t, "X"] = 5e6
    high.loc[t, "X"] = close.loc[t, "X"] * 1.001
    assert ir.compute_radar(high, low, close, volume).views["jour"].state.loc[t, "X"] == 1.0
    extras = ir.RadarExtras(earnings=pd.DataFrame({"asset": ["X"], "date": [close.index[-1]]}))
    radar = ir.compute_radar(high, low, close, volume, extras=extras)
    assert radar.views["jour"].state.loc[t, "X"] == 0.0  # veille des résultats
    assert "Jour d'événement" in radar.explain("X", len(close) - 2)
    third_friday = pd.Timestamp("2021-03-19")
    assert ir.event_days(pd.bdate_range("2021-03-15", periods=5), ["X"]).loc[third_friday, "X"]


def test_insider_purchases_count_from_the_next_session_for_three_months():
    high, low, close, volume = quiet_market()
    filed = close.index[200]
    ins = pd.DataFrame({"asset": ["X", "X"], "filing_date": [filed, filed], "owner": ["A", "B"],
                        "role": ["directeur", "administrateur"], "value": [3e5, 2e5]})
    radar = ir.compute_radar(high, low, close, volume, extras=ir.RadarExtras(insiders=ins))
    pts = radar.evidence["dirigeants"]["X"]
    assert pts.iloc[200] == 0.0 and pts.iloc[201] == 1.0  # connu le lendemain du dépôt
    assert pts.loc[filed + pd.Timedelta(days=91):].eq(0.0).all()
    assert "2 dirigeant(s)" in radar.explain("X", 201) and "A (directeur)" in radar.explain("X", 201)


def test_private_venues_use_the_publication_date():
    high, low, close, volume = quiet_market()
    weeks = pd.date_range(close.index[0], close.index[-1], freq="W-MON")
    ats = pd.DataFrame({"asset": "X", "week_start": weeks, "published": weeks + pd.Timedelta(days=21),
                        "ats_volume": 5e5, "block_volume": 1e4, "bank_volume": 2e5, "banks": "UBS"})
    hot_week = weeks[-8]
    ats.loc[ats["week_start"] == hot_week, "block_volume"] = 1e5  # blocs x10
    up = close.index[(close.index >= hot_week) & (close.index < hot_week + pd.Timedelta(days=7))]
    close.loc[up, "X"] = np.linspace(100, 104, len(up))  # semaine de hausse
    radar = ir.compute_radar(high, low, close, volume, extras=ir.RadarExtras(ats=ats))
    pts = radar.evidence["bourses_privees"]["X"]
    published = hot_week + pd.Timedelta(days=21)
    assert pts.loc[:published].eq(0.0).all()
    assert pts.loc[published + pd.Timedelta(days=1):].iloc[0] == 0.5
    i = int(close.index.searchsorted(published, side="right"))
    assert "plateformes de blocs ×10" in radar.explain("X", i)


def test_short_interest_drop_and_five_percent_filing_are_explained():
    high, low, close, volume = quiet_market()
    settle = close.index[[100, 110, 120]]
    si = pd.DataFrame({"asset": "X", "settlement": settle, "available": settle + pd.Timedelta(days=11),
                       "short_qty": [1e6, 1e6, 7e5]})
    f5 = pd.DataFrame({"asset": ["X", "X"], "filing_date": [close.index[130], close.index[131]],
                       "form": ["SC 13D", "SC 13D/A"], "filer": ["Fonds Exemple", ""]})
    radar = ir.compute_radar(high, low, close, volume, extras=ir.RadarExtras(short_interest=si, filings_5pct=f5))
    i = int(close.index.searchsorted(settle[2] + pd.Timedelta(days=11), side="right"))
    assert radar.evidence["ventes_a_decouvert"]["X"].iloc[i] == 0.5
    assert radar.evidence["ventes_a_decouvert"]["X"].iloc[i - 1] == 0.0
    assert "-30%" in radar.explain("X", i)
    assert radar.evidence["cinq_pourcent"]["X"].iloc[131] == 1.0
    assert "Fonds Exemple a déclaré plus de 5 %" in radar.explain("X", 132)
    assert radar.explain("X", 132).count("a déclaré") == 1  # l'amendement n'est pas compté


def test_hidden_buying_divergence():
    high, low, close, volume = quiet_market()
    n = len(close)
    ret = np.tile([0.004, -0.005], n // 2)[:n]  # petites hausses sur gros volume, baisses sur faible volume
    close["X"] = 100 * np.cumprod(1 + ret)
    volume["X"] = np.where(ret > 0, 2e6, 1e6)
    radar = ir.compute_radar(close * 1.01, close * 0.99, close, volume)
    assert radar.evidence["divergence"]["X"].iloc[-1] == 0.5
    assert "achats cachés" in radar.explain("X", n - 1)


def test_radar_extras_round_trip_through_csv(tmp_path):
    data = fb.generate_synthetic_market(start="2020-01-01", end="2021-12-31", seed=3)
    fb.export_market_to_csv(data, tmp_path)
    loaded = fb.load_market_from_csv(tmp_path)
    for table in ir.RadarExtras.TABLES:
        a, b = getattr(data.extras, table), getattr(loaded.extras, table)
        assert len(a) == len(b) and list(a.columns) == list(b.columns)
    s1 = fb.compute_signals(data, fb.StrategyConfig()).radar.score
    s2 = fb.compute_signals(loaded, fb.StrategyConfig()).radar.score
    np.testing.assert_allclose(s1.to_numpy(), s2.reindex_like(s1).to_numpy(), atol=1e-9)


def test_five_percent_wave_from_one_manager_is_ignored():
    high, low, close, volume = quiet_market()
    cols = ["X", "Y", "Z", "U", "V"]
    close5 = pd.concat([close["X"]] * 5, axis=1, keys=cols)
    vol5 = pd.concat([volume["X"]] * 5, axis=1, keys=cols)
    day = close.index[200]
    f5 = pd.DataFrame({"asset": cols, "filing_date": day, "form": "SC 13G", "filer": "Gérant réorganisé"})
    radar = ir.compute_radar(close5 * 1.01, close5 * 0.99, close5, vol5, extras=ir.RadarExtras(filings_5pct=f5))
    assert "cinq_pourcent" not in radar.evidence
    radar = ir.compute_radar(close5 * 1.01, close5 * 0.99, close5, vol5,
                             extras=ir.RadarExtras(filings_5pct=f5.iloc[:2]))
    assert radar.evidence["cinq_pourcent"]["X"].iloc[201] == 0.5
