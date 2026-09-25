# -*- coding: utf-8 -*-
"""
cot_btc.py — Suivre les institutions sur les contrats à terme bitcoin (CME, CFTC) depuis 2018
============================================================================================

Les flux des ETF bitcoin n'existent que depuis janvier 2024 (etf_btc.py). Pour tester la même idée —
« l'argent des grandes institutions entre-t-il ? » — sur une période plus longue, on utilise le rapport
hebdomadaire officiel de la CFTC (Traders in Financial Futures) sur le contrat bitcoin du CME, depuis
avril 2018 : positions des GESTIONNAIRES D'ACTIFS (fonds, assureurs, BlackRock…) et des FONDS
SPÉCULATIFS (« leveraged funds »). Un contrat = 5 bitcoins.

Règle d'honnêteté : positions du mardi publiées le vendredi soir ; la copie n'agit qu'à partir du
lundi suivant (date du rapport + 6 jours), frais 0,20 % par achat ou vente, trésorerie à 2 %.
Cours du bitcoin : Coin Metrics (quotidien, onchain.py).

    python backtest/cot_btc.py        # télécharge, teste, écrit outputs/cot_btc/
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import etf_btc  # noqa: E402
import flow_backtest as fb  # noqa: E402
import onchain  # noqa: E402
import phase1_data as p1  # noqa: E402

CME_BITCOIN = "133741"
OUT = p1.ROOT / "outputs" / "cot_btc"
DATA = p1.ROOT / "data" / "onchain" / "cot_bitcoin.csv"
PUBLICATION_DELAY = pd.Timedelta(days=6)  # mardi -> lundi suivant


def download() -> pd.DataFrame:
    http = p1.Http(headers={"User-Agent": "FlowFund-Research/1.0"}, min_interval=0.5)
    import json
    rows = json.loads(http.get(p1.CFTC_TFF, params={
        "$where": f"cftc_contract_market_code='{CME_BITCOIN}'", "$limit": 50000,
        "$order": "report_date_as_yyyy_mm_dd"}))
    df = pd.DataFrame(rows)
    cols = ["open_interest_all", "asset_mgr_positions_long", "asset_mgr_positions_short",
            "lev_money_positions_long", "lev_money_positions_short"]
    out = df[["report_date_as_yyyy_mm_dd", *cols]].rename(columns={"report_date_as_yyyy_mm_dd": "report_date"})
    out[cols] = out[cols].apply(pd.to_numeric, errors="coerce")
    out["report_date"] = pd.to_datetime(out["report_date"].str[:10])
    DATA.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(DATA, index=False)
    return out


def daily_signal(weekly: pd.Series, index: pd.DatetimeIndex) -> pd.Series:
    """Valeur hebdomadaire -> série quotidienne, connue seulement à partir de sa date de publication."""
    known = weekly.copy()
    known.index = known.index + PUBLICATION_DELAY
    return known.reindex(known.index.union(index)).sort_index().ffill().reindex(index)


def main() -> pd.DataFrame:
    OUT.mkdir(parents=True, exist_ok=True)
    cot = download().set_index("report_date").sort_index()
    btc = onchain.load("btc")["PriceUSD"].dropna()
    price = btc[btc.index >= cot.index[0] + PUBLICATION_DELAY]
    # Positions nettes en bitcoins (1 contrat = 5 BTC)
    groups = {"Gestionnaires d'actifs": 5 * (cot["asset_mgr_positions_long"] - cot["asset_mgr_positions_short"]),
              "Fonds spéculatifs": 5 * (cot["lev_money_positions_long"] - cot["lev_money_positions_short"])}
    curves = {"Bitcoin acheté et gardé": 100 * price / price.iloc[0]}
    for name, net in groups.items():
        for weeks in (1, 4, 13):
            rising = daily_signal(net.diff(weeks), price.index) > 0
            curves[f"Bitcoin si {name.lower()} augmentent leurs achats ({weeks} sem.)"] = etf_btc.timing(price, rising)
    for weeks in (1, 4, 13):  # contrôle : même rythme hebdomadaire, prix seul
        weekly_price = price.resample("W-TUE").last()
        rising = daily_signal(weekly_price.pct_change(weeks), price.index) > 0
        curves[f"Contrôle : bitcoin s'il a monté ({weeks} sem.)"] = etf_btc.timing(price, rising)
    split = price.index[len(price) // 2]
    rows = []
    for name, eq in curves.items():
        m = fb.compute_metrics(eq, etf_btc.CASH_RATE)
        a, b = eq[eq.index < split], eq[eq.index >= split]
        a_m, b_m = fb.compute_metrics(a, 0.02), fb.compute_metrics(b, 0.02)
        rows.append({"strategie": name, "par_an": m["cagr"], "pire_perte": m["max_drawdown"], "sharpe": m["sharpe"],
                     "par_an_1re_moitie": a_m["cagr"], "par_an_2e_moitie": b_m["cagr"]})
    table = pd.DataFrame(rows)
    table.to_csv(OUT / "strategies.csv", index=False)
    pd.DataFrame(curves).to_csv(OUT / "courbes.csv")
    with pd.option_context("display.width", 250, "display.max_colwidth", 80):
        print(f"Période : {price.index[0]:%d/%m/%Y} → {price.index[-1]:%d/%m/%Y} ; moitiés coupées le {split:%d/%m/%Y}")
        print(table.to_string(index=False, float_format=lambda v: f"{v:+.3f}"))
    return table


if __name__ == "__main__":
    main()
