# -*- coding: utf-8 -*-
"""
etf_btc.py — Suivre l'argent qui entre dans les ETF bitcoin (BlackRock IBIT…) prédit-il le bitcoin ?
====================================================================================================

Source : TFTC, https://www.tftc.io/bitcoin-etf-flows (licence CC BY 4.0 ; agrégats SoSoValue et
déclarations des émetteurs compilées par Farside ; cours du bitcoin relevé vers 21 h UTC). Flux nets
quotidiens (créations moins rachats de parts) de chaque ETF bitcoin américain depuis le 11/01/2024.

Règle d'honnêteté : le flux d'une séance n'est connu qu'après la clôture ; la copie agit à la séance
SUIVANTE (cours du lendemain), frais 0,20 % par achat ou vente.

    python backtest/etf_btc.py        # télécharge, teste, écrit outputs/etf_btc/
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import flow_backtest as fb  # noqa: E402
import phase1_data as p1  # noqa: E402

URL = "https://www.tftc.io/bitcoin-etf-flows/data.json"
DATA = p1.ROOT / "data" / "etf" / "btc_etf_flows.json"
OUT = p1.ROOT / "outputs" / "etf_btc"
COST = 0.002
CASH_RATE = 0.02


def download() -> pd.DataFrame:
    DATA.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(URL, headers={"User-Agent": "FlowFund-Research/1.0"})
    DATA.write_bytes(urllib.request.urlopen(req, timeout=120).read())
    return load()


def load() -> pd.DataFrame:
    days = json.loads(DATA.read_text())["days"]
    df = pd.DataFrame([{"date": d["date"], "total": d["netFlowUsd"], "btc": d["btcCloseUsd"],
                        "ibit": (d.get("perEtfUsd") or {}).get("IBIT", np.nan)} for d in days])
    df["date"] = pd.to_datetime(df["date"])
    return df.set_index("date").sort_index().astype(float)


def timing(price: pd.Series, invested: pd.Series) -> pd.Series:
    """Valeur (base 100) d'un portefeuille investi en bitcoin les jours où `invested` (décidé la veille)."""
    pos = invested.shift(1).fillna(False).astype(float)  # décision connue le soir, appliquée le lendemain
    ret = price.pct_change().fillna(0.0)
    gaps = price.index.to_series().diff().dt.days.fillna(0)
    cash = (1 + CASH_RATE) ** (gaps / 365.0) - 1
    trade = pos.diff().abs().fillna(pos)
    return 100 * (1 + pos * ret + (1 - pos) * cash - trade * COST).cumprod()


def forward_table(df: pd.DataFrame) -> pd.DataFrame:
    """Rendement moyen du bitcoin APRÈS des entrées fortes / des sorties fortes (séance suivante incluse)."""
    rows = []
    price = df["btc"]
    for col, label in (("ibit", "BlackRock (IBIT)"), ("total", "Tous les ETF")):
        for k in (1, 5, 20):
            flow = df[col].rolling(k).sum()
            for h in (5, 20, 60):
                fwd = price.shift(-(h + 1)) / price.shift(-1) - 1  # achat le lendemain, revente h séances après
                x = pd.DataFrame({"f": flow, "r": fwd}).dropna()
                hi, lo = x["f"].quantile(0.8), x["f"].quantile(0.2)
                rows.append({"source": label, "fenetre_flux_j": k, "horizon_j": h,
                             "apres_fortes_entrees": x.loc[x["f"] >= hi, "r"].mean(),
                             "apres_fortes_sorties": x.loc[x["f"] <= lo, "r"].mean(),
                             "moyenne": x["r"].mean(), "correlation": x["f"].corr(x["r"]),
                             "n": len(x)})
    return pd.DataFrame(rows)


def main() -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    df = download()
    price = df["btc"]
    split = df.index[len(df) // 2]
    table = forward_table(df)
    chase = pd.DataFrame({"flux": df["total"], "hausse_5j_avant": price.pct_change(5)}).corr().iloc[0, 1]
    curves = {"Bitcoin acheté et gardé": 100 * price / price.iloc[0]}
    for col, label in (("ibit", "BlackRock"), ("total", "tous les ETF")):
        for k in (5, 20):
            curves[f"Bitcoin seulement si entrées {label} > 0 sur {k} séances"] = timing(price, df[col].rolling(k).sum() > 0)
    # Contrôle : la même règle avec le seul PRIX (tendance). Si elle fait aussi bien, les flux n'apportent rien.
    for k in (5, 20):
        curves[f"Contrôle : bitcoin seulement s'il a monté sur {k} séances"] = timing(price, price.pct_change(k) > 0)
    # Les flux ET la tendance d'accord
    curves["Bitcoin si entrées BlackRock > 0 ET hausse sur 5 séances"] = timing(
        price, (df["ibit"].rolling(5).sum() > 0) & (price.pct_change(5) > 0))
    curves["Bitcoin si entrées BlackRock > 0 MAIS baisse sur 5 séances"] = timing(
        price, (df["ibit"].rolling(5).sum() > 0) & (price.pct_change(5) <= 0))
    rows = []
    for name, eq in curves.items():
        m = fb.compute_metrics(eq, CASH_RATE)
        a = eq[eq.index < split]
        b = eq[eq.index >= split]
        rows.append({"strategie": name, "par_an": m["cagr"], "pire_perte": m["max_drawdown"], "sharpe": m["sharpe"],
                     "1re_moitie": a.iloc[-1] / a.iloc[0] - 1, "2e_moitie": b.iloc[-1] / b.iloc[0] - 1})
    strat = pd.DataFrame(rows)
    table.to_csv(OUT / "rendements_apres_flux.csv", index=False)
    strat.to_csv(OUT / "strategies.csv", index=False)
    pd.DataFrame(curves).to_csv(OUT / "courbes.csv")
    with pd.option_context("display.width", 250):
        print(f"Période : {df.index[0]:%d/%m/%Y} → {df.index[-1]:%d/%m/%Y} ({len(df)} séances) ; moitiés coupées le {split:%d/%m/%Y}")
        print(f"Les flux suivent-ils la hausse passée ? corrélation flux du jour / hausse des 5 séances d'avant : {chase:+.2f}")
        print(table.to_string(index=False, float_format=lambda v: f"{v:+.3f}"))
        print(strat.to_string(index=False, float_format=lambda v: f"{v:+.3f}"))
    return {"table": table, "strategies": strat, "chase": chase, "split": split}


if __name__ == "__main__":
    main()
