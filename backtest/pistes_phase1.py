# -*- coding: utf-8 -*-
"""
pistes_phase1.py — Test des pistes d'amélioration sur les données réelles de la phase 1
=========================================================================================

Pistes demandées par les fondateurs (24/09/2026) :
  1. investir davantage (budget de risque par ligne, nombre de lignes, équipondération) ;
  2. placer la trésorerie inutilisée dans le S&P 500 (SPY) au lieu de la laisser en cash ;
  3. mieux choisir les « meilleurs gérants » (fenêtre de classement, taille de la liste) ;
  4. utiliser la position des banques sur les contrats S&P 500 (CFTC) comme signal.

Chaque piste est mesurée sur la même période que la référence (depuis le premier achat du
fonds) et sur deux moitiés séparées : une piste qui ne marche que sur une moitié est suspecte
(sur-ajustement). Aucune information future n'est utilisée : la position des banques n'est lue
qu'à sa date de publication (available_date), l'exposition au SPY suit l'exposition de la veille.

    python backtest/pistes_phase1.py                       # toutes les pistes (≈ 30 min)
    python backtest/pistes_phase1.py --skip-managers       # sans reconstruire les listes de gérants
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import flow_backtest as fb  # noqa: E402
import phase1_data as p1  # noqa: E402

ENGINE = p1.DATA / "engine"
OUT = p1.ROOT / "outputs" / "pistes"
SPLIT = pd.Timestamp("2022-07-01")
SPY_COST = 0.0002  # 2 points de base par unité d'exposition SPY achetée ou vendue
COT_MARKET = "E-mini S&P 500"
COT_WINDOW = 156  # habitude des banques : médiane des 3 dernières années (rapports hebdomadaires)


def spy_prices() -> pd.Series:
    df = pd.read_csv(p1.DATA / "prices" / f"{p1.MARKET_ETF}.csv", parse_dates=["date"])
    return df.set_index("date")["adjClose"].sort_index()


def daily_cash_return(index: pd.DatetimeIndex, rate: float) -> pd.Series:
    gaps = pd.Series(index, index=index).diff().dt.days.fillna(0)
    return (1.0 + rate) ** (gaps / 365.0) - 1.0


def overlay_spy(equity: pd.Series, gross: pd.Series, spy: pd.Series, rate: float,
                active: pd.Series | None = None) -> pd.Series:
    """Remplace le cash inutilisé par du SPY (exposition décidée sur la position de la veille).
    active : booléen par jour ; False => la part inutilisée reste en cash ce jour-là."""
    r = equity.pct_change().fillna(0.0)
    rs = spy.reindex(equity.index).ffill().pct_change().fillna(0.0)
    idle = (1.0 - gross.shift(1).fillna(0.0)).clip(0.0, 1.0)
    if active is not None:
        idle = idle * active.reindex(equity.index).shift(1).fillna(False).astype(float)
    r2 = r + idle * (rs - daily_cash_return(equity.index, rate)) - idle.diff().abs().fillna(idle) * SPY_COST
    return equity.iloc[0] * (1.0 + r2).cumprod()


def banks_regime(index: pd.DatetimeIndex) -> pd.Series:
    """+1 quand les banques sont MOINS vendeuses que d'habitude sur le S&P 500, -1 sinon,
    connu seulement à partir de la date de publication du rapport."""
    cot = pd.read_csv(p1.DATA / "cot_dealers.csv", parse_dates=["report_date", "available_date"])
    cot = cot[cot["market"] == COT_MARKET].sort_values("report_date")
    habit = cot["dealer_net_pct_oi"].rolling(COT_WINDOW, min_periods=52).median()
    state = pd.Series(np.where(cot["dealer_net_pct_oi"] > habit, 1.0, -1.0), index=cot["available_date"])
    state = state[habit.notna().to_numpy()]
    state = state[~state.index.duplicated(keep="last")]
    return state.reindex(index, method="ffill").fillna(0.0)


def stats(equity: pd.Series, start: pd.Timestamp, end: pd.Timestamp | None = None) -> dict:
    eq = equity[(equity.index >= start) & ((equity.index < end) if end is not None else True)]
    m = fb.compute_metrics(eq, risk_free_rate=0.02)
    return {"cagr": m["cagr"], "vol": m["volatility"], "sharpe": m["sharpe"], "max_dd": m["max_drawdown"]}


def row(name: str, equity: pd.Series, start: pd.Timestamp, exposure: float | None, note: str = "") -> dict:
    full, first, second = stats(equity, start), stats(equity, start, SPLIT), stats(equity, SPLIT)
    return {"piste": name, **{f"{k}": v for k, v in full.items()},
            "cagr_1": first["cagr"], "sharpe_1": first["sharpe"],
            "cagr_2": second["cagr"], "sharpe_2": second["sharpe"],
            "exposition": exposure, "note": note}


def run(data: fb.MarketData, cfg: fb.StrategyConfig) -> fb.BacktestResult:
    return fb.run_backtest(data, cfg)


def main(argv: list[str] | None = None) -> pd.DataFrame:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--skip-managers", action="store_true", help="ne pas tester la piste 3 (reconstructions)")
    args = parser.parse_args(argv)
    OUT.mkdir(parents=True, exist_ok=True)
    spy = spy_prices()
    base_cfg = fb.StrategyConfig(**p1.sm.PROXY_FLOW_SETTINGS)
    data = fb.load_market_from_csv(ENGINE)
    base = run(data, base_cfg)
    start = base.gross[base.gross > 0].index[0]  # premier jour investi : période commune à toutes les pistes
    spy_eq = spy.reindex(base.equity.index).ffill()
    rows = [row("Référence (réglages actuels)", base.equity, start, base.metrics["avg_gross_exposure"]),
            row("S&P 500 (SPY)", spy_eq, start, 1.0)]
    curves = {"reference": base.equity, "spy": spy_eq}

    # --- Piste 1 : investir davantage
    variants = {
        "1a. Budget de risque 3 % par ligne": dict(risk_budget_per_position=0.03),
        "1b. Budget de risque 4 % par ligne": dict(risk_budget_per_position=0.04),
        "1c. 20 lignes, budget 3 %": dict(max_positions=20, risk_budget_per_position=0.03),
        "1d. 12 lignes équipondérées": dict(sizing="equal"),
        "1e. 20 lignes équipondérées": dict(sizing="equal", max_positions=20),
    }
    results = {}
    for name, kw in variants.items():
        res = run(data, replace(base_cfg, **kw))
        results[name] = res
        rows.append(row(name, res.equity, start, res.metrics["avg_gross_exposure"]))
        print(f"{name} : fait", flush=True)

    # --- Piste 2 : trésorerie inutilisée placée dans le SPY
    sp_base = overlay_spy(base.equity, base.gross, spy, base_cfg.cash_rate)
    rows.append(row("2a. Réglages actuels + cash placé dans le SPY", sp_base, start, 1.0))
    curves["spy_overlay"] = sp_base
    best1 = max(variants, key=lambda n: stats(results[n].equity, start)["sharpe"])
    sp_best = overlay_spy(results[best1].equity, results[best1].gross, spy, base_cfg.cash_rate)
    rows.append(row(f"2b. {best1[4:]} + cash placé dans le SPY", sp_best, start, 1.0))

    # --- Piste 4 : position des banques (CFTC, contrats E-mini S&P 500)
    regime = banks_regime(base.equity.index)
    for label, on in (("moins vendeuses", regime > 0), ("plus vendeuses", regime < 0)):
        eq = overlay_spy(base.equity, base.gross, spy, base_cfg.cash_rate, active=on)
        rows.append(row(f"4. Cash dans le SPY seulement si banques {label} que d'habitude", eq, start, None))
        timing = overlay_spy(pd.Series(1.0, index=base.equity.index), pd.Series(0.0, index=base.equity.index),
                             spy, base_cfg.cash_rate, active=on)
        rows.append(row(f"4. SPY seul, investi si banques {label} que d'habitude", timing, start, None,
                        f"investi {on[on.index >= start].mean():.0%} du temps"))

    # --- Piste 3 : liste des meilleurs gérants
    if not args.skip_managers:
        for lookback, top_n in ((4, 50), (8, 20), (8, 100), (4, 20), (4, 100)):
            folder = OUT / f"engine_l{lookback}_n{top_n}"
            p1.build_engine_files(out_dir=folder, lookback=lookback, top_n=top_n)
            res = run(fb.load_market_from_csv(folder), base_cfg)
            first_buy = res.gross[res.gross > 0].index[0]
            name = f"3. Classement sur {lookback} trimestres, {top_n} gérants"
            rows.append(row(name, res.equity, start, res.metrics["avg_gross_exposure"],
                            f"premier achat {first_buy:%d/%m/%Y} ; depuis : "
                            f"{stats(res.equity, first_buy)['cagr']:+.1%} par an contre "
                            f"{stats(spy_eq, first_buy)['cagr']:+.1%} pour le SPY"))
            curves[f"gerants_l{lookback}_n{top_n}"] = res.equity
            print(f"{name} : fait", flush=True)
        p1.build_engine_files()  # remet la liste de référence (8 trimestres, 50 gérants)

    table = pd.DataFrame(rows)
    table.to_csv(OUT / "pistes.csv", index=False)
    pd.DataFrame(curves).to_csv(OUT / "courbes.csv")
    with pd.option_context("display.width", 250, "display.max_colwidth", 70):
        fmt = table.copy()
        for c in ("cagr", "vol", "max_dd", "cagr_1", "cagr_2", "exposition"):
            fmt[c] = fmt[c].map(lambda v: "" if pd.isna(v) else f"{v:+.1%}" if c.startswith("cagr") else f"{v:.0%}")
        for c in ("sharpe", "sharpe_1", "sharpe_2"):
            fmt[c] = fmt[c].map(lambda v: f"{v:.2f}")
        print(f"Période : {start:%d/%m/%Y} → {base.equity.index[-1]:%d/%m/%Y} ; moitiés séparées le {SPLIT:%d/%m/%Y}")
        print(fmt.to_string(index=False))
    return table


if __name__ == "__main__":
    main()
