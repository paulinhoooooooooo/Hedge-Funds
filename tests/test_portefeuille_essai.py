# -*- coding: utf-8 -*-
"""Tests du portefeuille d'essai « fonds OU radar » (backtest/portefeuille_essai.py) et de son affichage."""

import pandas as pd
import pytest

import dashboard
import flow_backtest as fb
import portefeuille_essai as pe
import smart_money as sm
import suivi_signaux as ss
from institutional_radar import ALERT_THRESHOLD

START = "2023-06-01"


@pytest.fixture(scope="module")
def fund():
    data = fb.generate_synthetic_market(start="2021-01-04", end="2024-06-28", seed=5)
    return fb.run_backtest(data, sm.phase1_config(data))


def test_trial_starts_in_cash_and_follows_the_rules(fund):
    essai = pe.follow_blend(fund, START)
    eq = essai.equity
    assert (eq[eq.index < START] == 100_000.0).all()  # rien avant le départ
    assert (essai.cash >= -1e-6).all()
    assert int((essai.weights > 0).sum(axis=1).max()) <= ss.LINES
    j = essai.journal
    assert (pd.to_datetime(j["date"]) >= pd.Timestamp(START)).all()
    radar = fund.signals.radar.score
    for m in j[j["action"] == pe.RADAR_BUY].itertuples():  # un achat radar = une alerte d'accumulation ce jour-là
        assert radar.at[pd.Timestamp(m.date), m.asset] >= ALERT_THRESHOLD
    fund_buys = fund.journal[(pd.to_datetime(fund.journal["date"]) >= START) & fund.journal["action"].isin(ss.BUY)]
    assert set(j.loc[j["action"] == "ENTRY", "asset"]) <= set(fund_buys["asset"])  # achats du fonds = décisions publiées
    assert set(j["label"]) <= set(pe.LABELS.values())


def test_trial_section_on_the_desk_page(fund):
    live = ss.follow(fund, START)
    essai = pe.follow_blend(fund, START)
    html = dashboard._essai_html(essai, live, None, START)
    assert "Portefeuille d'essai · fonds OU radar" in html and "portefeuille réel" in html
    assert "live-line" not in html and "lv-summary" not in html  # le logiciel de cours en direct ne la touche pas
    real = dashboard._live_html(live, None, START)
    assert "Démarré à zéro" in real  # le portefeuille réel garde son texte
