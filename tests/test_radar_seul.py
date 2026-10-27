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
    for side in ("fonds", "radar_seul", "alertes_radar"):
        assert "rendement" in res["reel"][side]
    # portefeuille vide au départ : il n'achète que des titres en alerte d'accumulation
    assert all(m["action"].startswith(("Achat", "Sortie")) for m in res["reel"]["alertes_radar"]["mouvements"])
    years = res["horizons"]["annees"]
    assert set(years) == {str(y) for y in range(2012, 2026)} and "1 an" in res["horizons"]["fenetres"]
    assert "Radar seul face au fonds" in capsys.readouterr().out


def test_blends_on_synthetic_market(capsys):
    import melange_radar as mr
    res = mr.main(["--data-dir", "/inexistant"])
    assert set(res["variantes"]) == {"fonds", "radar_seul", "fonds_ou_radar", "fonds_et_radar", "deux_poches"}
    act = res["activite"]
    for x in act.values():  # chaque aller-retour clos a commencé par un achat
        assert x["allers_retours_clos"] <= x["achats"]
        assert 0 <= x["jours_portefeuille_plein"] <= 1
    assert "Activité" in capsys.readouterr().out


def test_two_pockets_rebalance_monthly():
    import pandas as pd
    import melange_radar as mr
    idx = pd.bdate_range("2024-01-01", "2024-03-29")
    a = pd.Series(100.0, index=idx)
    b = pd.Series(range(len(idx)), index=idx, dtype=float) + 100.0
    mix = mr.two_pockets(a, b)
    assert mix.iloc[0] == 100.0 and mix.iloc[-1] > 100.0 and mix.iloc[-1] < b.iloc[-1]
