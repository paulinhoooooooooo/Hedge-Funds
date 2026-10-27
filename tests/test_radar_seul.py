# -*- coding: utf-8 -*-
"""Tests de l'analyse « radar seul » (backtest/radar_seul.py)."""

import flow_backtest as fb
import radar_seul as rs
import smart_money as sm
from institutional_radar import ALERT_THRESHOLD


def test_radar_only_signals_follow_radar_alerts():
    data = fb.generate_synthetic_market(start="2018-01-02", end="2021-12-31")
    sig = fb.compute_signals(data, sm.phase1_config(data))
    rad = rs.radar_only_signals(sig)
    score = sig.radar.score.reindex(index=sig.calendar, columns=sig.prices.columns)
    assert not (rad.entry & ~(score >= ALERT_THRESHOLD)).to_numpy().any()  # n'achète que sur alerte
    assert rad.entry.to_numpy().sum() > 0
    dist = (score <= -ALERT_THRESHOLD).fillna(False)
    assert (rad.dist_slow == dist).all().all() and (rad.dist_fast == dist).all().all()  # vente complète
    assert not rad.dist_hard.to_numpy().any() and not rad.dist_foot.to_numpy().any()


def test_main_on_synthetic_market(capsys):
    res = rs.main(["--data-dir", "/inexistant"])
    assert res["source"].startswith("DONNÉES SYNTHÉTIQUES")
    for side in ("fonds", "radar_seul"):
        assert -1 < res["backtest"][side]["rendement_annuel"] < 1
        assert "rendement" in res["reel"][side]
    assert "Radar seul face au fonds" in capsys.readouterr().out
