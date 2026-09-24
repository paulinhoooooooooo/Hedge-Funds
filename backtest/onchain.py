# -*- coding: utf-8 -*-
"""
onchain.py — Les mouvements des grandes plateformes crypto améliorent-ils le fonds ?
=====================================================================================

Idée (captures Arkham des fondateurs, 24/09/2026) : suivre les bitcoins et ethers qui ENTRENT sur les
plateformes (Binance, Coinbase, Kraken, Bullish…) — souvent pour être vendus — et ceux qui en SORTENT —
souvent pour être gardés (accumulation).

Source gratuite : Coin Metrics Community (https://community-api.coinmetrics.io), séries quotidiennes
agrégées sur toutes les plateformes identifiées, depuis 2015 :
  FlowInExNtv / FlowOutExNtv (entrées / sorties, en jetons), SplyExNtv (réserve des plateformes),
  SplyCur (offre totale), PriceUSD. Arkham, lui, exige une clé payante et détaille chaque transfert.

Traduction dans le moteur (classe CRYPTO, donnée du jour J publiée en J+1) :
  * jambe lente  = « rareté » sur les plateformes = offre totale / réserve des plateformes
                   (+3 % en un trimestre = les grands retirent leurs jetons : accumulation) ;
  * jambe rapide = sorties nettes des plateformes sur 30 jours, en % de leur réserve ;
  * l'empreinte prix / volume reste neutre (pas de volumes fiables toutes plateformes).
Les jetons sont traités aux clôtures de New York (jours de bourse) ; les flux du week-end sont
reportés sur la séance suivante.

Limite importante : Coin Metrics identifie les adresses des plateformes au fil du temps et peut
réattribuer le passé ; l'historique des réserves peut donc contenir un léger biais d'anticipation.

    python backtest/onchain.py        # télécharge, teste, écrit outputs/onchain/
"""

from __future__ import annotations

import json
import shutil
import sys
import urllib.request
from dataclasses import replace
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import flow_backtest as fb  # noqa: E402
import phase1_data as p1  # noqa: E402
import smart_money as sm  # noqa: E402

DATA = p1.ROOT / "data" / "onchain"
OUT = p1.ROOT / "outputs" / "onchain"
API = ("https://community-api.coinmetrics.io/v4/timeseries/asset-metrics?assets={asset}"
       "&metrics=FlowInExNtv,FlowOutExNtv,SplyExNtv,SplyCur,PriceUSD&frequency=1d&page_size=10000"
       "&start_time=2015-01-01")
TOKENS = {"BTC": "btc", "ETH": "eth"}
SPLIT = pd.Timestamp("2022-07-01")
COMMON_START = pd.Timestamp("2018-08-16")  # premier achat du fonds (liste des gérants disponible)


def download(assets: dict[str, str] = TOKENS) -> dict[str, pd.DataFrame]:
    DATA.mkdir(parents=True, exist_ok=True)
    out = {}
    for name, code in assets.items():
        url, rows = API.format(asset=code), []
        while url:
            page = json.load(urllib.request.urlopen(url, timeout=120))
            rows += page["data"]
            url = page.get("next_page_url")
        df = pd.DataFrame(rows)
        df.to_csv(DATA / f"{code}.csv", index=False)
        out[name] = load(code)
    return out


def load(code: str) -> pd.DataFrame:
    df = pd.read_csv(DATA / f"{code}.csv")
    df["date"] = pd.to_datetime(df["time"].str[:10])
    cols = ["FlowInExNtv", "FlowOutExNtv", "SplyExNtv", "SplyCur", "PriceUSD"]
    return df.set_index("date")[cols].apply(pd.to_numeric, errors="coerce").sort_index()


def to_sessions(daily: pd.Series, sessions: pd.DatetimeIndex, how: str) -> pd.Series:
    """Série 7 j / 7 -> séances de bourse : somme (flux) ou dernière valeur (niveaux) jusqu'à la séance."""
    target = sessions[np.minimum(sessions.searchsorted(daily.index), len(sessions) - 1)]
    keep = daily.index <= sessions[-1]
    grouped = daily[keep].groupby(target[keep])
    return (grouped.sum() if how == "sum" else grouped.last()).reindex(sessions)


def crypto_engine_files(src: Path, dst: Path, chain: dict[str, pd.DataFrame]) -> Path:
    """Copie les fichiers du moteur et y ajoute les jetons avec leurs signaux on-chain."""
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    sessions = pd.DatetimeIndex(pd.to_datetime(pd.read_csv(src / "market.csv")["date"])).normalize()
    assets, prices = pd.read_csv(dst / "assets.csv"), [pd.read_csv(dst / "prices.csv")]
    holdings, flows = [pd.read_csv(dst / "holdings.csv")], [pd.read_csv(dst / "flows.csv")]
    for name, d in chain.items():
        vehicle = f"ONCHAIN_{name}"
        close = to_sessions(d["PriceUSD"], sessions, "last")
        prices.append(pd.DataFrame({"date": sessions, "asset": name, "close": close.to_numpy(),
                                    "high": np.nan, "low": np.nan, "volume": np.nan}).dropna(subset=["close"]))
        scarcity = (d["SplyCur"] / d["SplyExNtv"]).dropna()
        holdings.append(pd.DataFrame({"asset": name, "period_end": scarcity.index,
                                      "filing_date": scarcity.index + pd.Timedelta(days=1), "value": scarcity.to_numpy()}))
        net_out = to_sessions((d["FlowOutExNtv"] - d["FlowInExNtv"]) * d["PriceUSD"], sessions, "sum")
        reserve = to_sessions(d["SplyExNtv"] * d["PriceUSD"], sessions, "last")
        flows.append(pd.DataFrame({"date": sessions, "vehicle": vehicle, "net_flow": net_out.to_numpy(),
                                   "aum": reserve.to_numpy()}).dropna())
        assets = pd.concat([assets, pd.DataFrame([{"asset": name, "asset_class": "CRYPTO", "flow_vehicle": vehicle,
                                                   "tv_symbol": f"BITSTAMP:{name}USD"}])], ignore_index=True)
    iso = {"date": "%Y-%m-%d", "period_end": "%Y-%m-%d", "filing_date": "%Y-%m-%d"}
    assets.to_csv(dst / "assets.csv", index=False)
    for name, frames in (("prices", prices), ("holdings", holdings), ("flows", flows)):
        frame = pd.concat(frames, ignore_index=True)
        for col, fmt in iso.items():  # dates homogènes AAAA-MM-JJ (fichiers d'origine et ajouts)
            if col in frame:
                frame[col] = pd.to_datetime(frame[col], format="mixed").dt.strftime(fmt)
        frame.to_csv(dst / f"{name}.csv", index=False)
    return dst


def stats(equity: pd.Series, start: pd.Timestamp, end: Optional[pd.Timestamp] = None) -> dict:
    eq = equity[(equity.index >= start) & ((equity.index < end) if end is not None else True)].dropna()
    m = fb.compute_metrics(eq, risk_free_rate=0.02)
    return {"cagr": m["cagr"], "vol": m["volatility"], "sharpe": m["sharpe"], "max_dd": m["max_drawdown"]}


def main() -> pd.DataFrame:
    OUT.mkdir(parents=True, exist_ok=True)
    chain = download()
    engine = p1.DATA / "engine"
    base_data = fb.load_market_from_csv(engine)
    crypto_dir = crypto_engine_files(engine, OUT / "engine_crypto", chain)
    crypto_data = fb.load_market_from_csv(crypto_dir)
    cfg = sm.phase1_config(base_data)
    runs = {
        "Fonds actuel (actions)": fb.run_backtest(base_data, cfg),
        "Fonds + BTC/ETH pilotés par les flux des plateformes": fb.run_backtest(crypto_data, cfg),
        "Fonds + BTC/ETH, mêmes règles SANS délai de publication (contrôle)":
            fb.run_backtest(crypto_data, replace(cfg, ignore_publication_lags=True)),
    }
    # Stratégie crypto seule : uniquement BTC et ETH, pilotés par les flux, le reste en cash
    only = crypto_data.assets.index[crypto_data.assets["asset_class"] == "CRYPTO"]
    solo_data = replace(crypto_data, assets=crypto_data.assets.loc[only], holdings=crypto_data.holdings[
        crypto_data.holdings["asset"].isin(only)], prices=crypto_data.prices[list(only)], high=None, low=None,
        volume=None, offexchange=None, offexchange_short=None, extras=None, hourly=None, market=None)
    runs["BTC/ETH seuls, pilotés par les flux (reste en cash)"] = fb.run_backtest(
        solo_data, replace(cfg, idle_cash_in_market=False, max_positions=2, sizing="equal", max_weight=0.5))
    curves = {k: r.equity for k, r in runs.items()}
    cal = runs["Fonds actuel (actions)"].equity.index
    for name in chain:
        px = crypto_data.prices[name].reindex(cal).ffill()
        curves[f"{name} acheté et gardé"] = px
    curves["S&P 500 (SPY)"] = base_data.market.reindex(cal).ffill()
    rows = []
    for name, eq in curves.items():
        start = max(COMMON_START, eq.dropna().index[0])
        full, a, b = stats(eq, start), stats(eq, start, SPLIT), stats(eq, SPLIT)
        rows.append({"portefeuille": name, **full, "cagr_1": a["cagr"], "sharpe_1": a["sharpe"],
                     "cagr_2": b["cagr"], "sharpe_2": b["sharpe"]})
    table = pd.DataFrame(rows)
    crypto_trades = runs["Fonds + BTC/ETH pilotés par les flux des plateformes"].trades
    crypto_trades = crypto_trades[crypto_trades["asset_class"] == "CRYPTO"]
    table.to_csv(OUT / "comparaison.csv", index=False)
    crypto_trades.to_csv(OUT / "trades_crypto.csv", index=False)
    pd.DataFrame(curves).to_csv(OUT / "courbes.csv")
    with pd.option_context("display.width", 250, "display.max_colwidth", 70):
        print(table.to_string(index=False, float_format=lambda v: f"{v:.3f}"))
    print(f"\nTransactions crypto dans le fonds : {len(crypto_trades)} ; taux de réussite "
          f"{(crypto_trades['return'] > 0).mean():.0%} ; rendement moyen {crypto_trades['return'].mean():+.1%} ; "
          f"gain total {crypto_trades['pnl'].sum() / 1e6:+.1f} M$ (capital initial 100 M$)")
    return table


if __name__ == "__main__":
    main()
