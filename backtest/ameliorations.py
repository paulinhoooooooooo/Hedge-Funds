# -*- coding: utf-8 -*-
"""
ameliorations.py — Les améliorations de l'analyse, testées une par une puis ensemble
====================================================================================

Chaque amélioration se branche sur les signaux du moteur (flow_backtest.Signals) ou sur ses données,
sans rien changer au programme actuel quand elle est désactivée :

  1  calibrage   ce que chaque indice a vraiment prédit, mesuré chaque année sur le passé seul
                 (corrélation de rang avec le rendement des 3 mois suivants) ; les indices sans
                 pouvoir prédictif pèsent zéro ; achat seulement si la note pondérée est positive
  2  vrais flux  jambe rapide mesurée par les parts en circulation des ETF sectoriels (State Street)
                 au lieu du proxy prix / volume
  4  météo       marché sous tension (VIX >= 30 ou stress financier de la Fed >= 1) : aucun achat,
                 exposition ramenée à 50 %
  5  comptes     pas d'achat d'une entreprise en perte ou valorisée plus de 60 fois ses bénéfices
  6  risques     volatilité cible du fonds 12 % et refus d'une ligne corrélée à plus de 0,85 avec
                 le portefeuille

Non testables sur l'historique avec des données gratuites :
  3  options     pas d'historique gratuit de l'activité sur les options action par action
  7  actualités  historique disponible chez Alpaca, dont les clés ne sont pas dans cette session ;
                 les nouvelles servent surtout à expliquer, pas à décider
  8  emprunt     le fichier gratuit du coût d'emprunt des actions n'a pas d'historique

Usage :
    python backtest/ameliorations.py --data-dir data/phase1/engine --n-actions 3 --seed 2026
"""

from __future__ import annotations

import argparse
import math
from dataclasses import replace
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import pandas as pd

import flow_backtest as fb

HORIZON = 63  # rendement des 3 mois suivants
VIX_STRESS = 30.0
FSI_STRESS = 1.0
REGIME_GROSS = 0.5
MAX_PE = 60.0
PROXY_THRESHOLDS = {"entry_flow_threshold": 0.0, "exit_flow_threshold": -0.05}  # proxy prix / volume
REAL_FLOW_THRESHOLDS = {"entry_flow_threshold": 0.0, "exit_flow_threshold": -0.02}  # vrais flux


# ---------------------------------------------------------------------------
# 1. Calibrage : ce que chaque indice a vraiment prédit (walk-forward annuel)
# ---------------------------------------------------------------------------

def components(sig: fb.Signals) -> dict[str, pd.DataFrame]:
    comp = {"détention des gérants": sig.io_change, "argent des fonds": sig.flow_ratio}
    if sig.footprint is not None:
        comp["empreinte prix / volume"] = (sig.footprint.accumulation.astype(float)
                                           - sig.footprint.distribution.astype(float))
    if sig.radar is not None:
        for name, view in sig.radar.views.items():
            comp[f"radar {name}"] = view.state
        for family, points in sig.radar.evidence.items():
            comp[f"radar {family.replace('_', ' ')}"] = points
    return {k: v.reindex_like(sig.prices).astype(float) for k, v in comp.items()}


def _rank_ic(x: np.ndarray, y: np.ndarray) -> float:
    ok = np.isfinite(x) & np.isfinite(y)
    if ok.sum() < 200 or np.nanstd(x[ok]) == 0:
        return np.nan
    return float(pd.Series(x[ok]).rank().corr(pd.Series(y[ok]).rank()))


def calibrate(sig: fb.Signals, horizon: int = HORIZON, step: int = 5, min_ic: float = 0.02,
              min_years: int = 2) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Note pondérée [date x actif] et table des poids par année.

    Au 1er janvier de chaque année, on mesure sur le passé seul (issues à 3 mois déjà connues) la
    corrélation de rang entre chaque indice et le rendement des 3 mois suivants, en excès de la
    moyenne des actifs quand il y en a au moins trois. Poids = cette corrélation si elle dépasse
    0,02, sinon zéro. La note sert jusqu'au 1er janvier suivant."""
    comp = components(sig)
    px = sig.prices
    fwd = px.shift(-horizon) / px - 1.0
    if px.shape[1] >= 3:
        fwd = fwd.sub(fwd.mean(axis=1), axis=0)
    cal = sig.calendar
    composite = pd.DataFrame(np.nan, index=cal, columns=px.columns)
    years = sorted(set(cal.year))
    rows = []
    for year in years:
        start = cal.searchsorted(pd.Timestamp(f"{year}-01-01"))
        end = cal.searchsorted(pd.Timestamp(f"{year + 1}-01-01"))
        known_until = start - horizon  # issues connues au 1er janvier
        if start >= len(cal) or known_until < 252 * min_years:
            continue
        sample = np.arange(0, known_until, step)
        y = fwd.to_numpy()[sample].ravel()
        weights, scales = {}, {}
        for name, frame in comp.items():
            x = frame.to_numpy()[sample].ravel()
            ic = _rank_ic(x, y)
            sd = np.nanstd(x[np.isfinite(x)]) if np.isfinite(x).any() else 0.0
            weights[name] = ic if np.isfinite(ic) and ic >= min_ic and sd > 0 else 0.0
            scales[name] = sd if sd > 0 else 1.0
            rows.append({"année": year, "indice": name, "corrélation": ic, "poids": weights[name]})
        if not any(weights.values()):
            continue
        block = sum(weights[k] * comp[k].iloc[start:end].fillna(0.0) / scales[k] for k in comp if weights[k])
        composite.iloc[start:end] = block.to_numpy()
    return composite, pd.DataFrame(rows)


def apply_calibration(sig: fb.Signals) -> tuple[fb.Signals, pd.DataFrame]:
    composite, table = calibrate(sig)
    calibrated = composite.notna()
    entry = sig.entry & (~calibrated | (composite > 0))
    score = sig.score.where(~calibrated, composite)
    return replace(sig, entry=entry, score=score), table


# ---------------------------------------------------------------------------
# 4. Météo du marché
# ---------------------------------------------------------------------------

def load_macro(path: Path) -> Optional[pd.DataFrame]:
    if not path.exists():
        return None
    m = pd.read_csv(path, parse_dates=["date", "available"])
    return m


def market_stress(macro: pd.DataFrame, calendar: pd.DatetimeIndex) -> pd.Series:
    """Vrai les séances où la dernière valeur PUBLIÉE du VIX ou du stress financier dépasse le seuil."""
    out = pd.Series(False, index=calendar)
    for series, limit in (("VIXCLS", VIX_STRESS), ("STLFSI4", FSI_STRESS)):
        m = macro[macro["series"] == series].sort_values("available")
        if m.empty:
            continue
        # connue le jour de sa publication au soir : utilisée à partir de la séance suivante
        pos = calendar.searchsorted(m["available"].to_numpy(), side="right")
        vals = pd.Series(np.nan, index=calendar)
        ok = pos < len(calendar)
        vals.iloc[pos[ok]] = m["value"].to_numpy()[ok]
        out |= vals.ffill() >= limit
    return out


def apply_regime(sig: fb.Signals, macro: pd.DataFrame) -> fb.Signals:
    stress = market_stress(macro, sig.calendar)
    blocked = pd.DataFrame(np.repeat(stress.to_numpy()[:, None], sig.entry.shape[1], axis=1),
                           index=sig.entry.index, columns=sig.entry.columns)
    entry = sig.entry & ~blocked
    cap = pd.Series(np.where(stress, REGIME_GROSS, np.inf), index=sig.calendar)
    return replace(sig, entry=entry, gross_cap=cap)


# ---------------------------------------------------------------------------
# 5. Comptes des entreprises
# ---------------------------------------------------------------------------

def load_fundamentals(path: Path, sig: fb.Signals) -> Optional[pd.DataFrame]:
    if not path.exists():
        return None
    f = pd.read_csv(path, parse_dates=["date"])
    ey = f.pivot_table(index="date", columns="asset", values="earnings_yield", aggfunc="last")
    return ey.reindex(index=sig.calendar, columns=sig.prices.columns).ffill(limit=5)


def apply_fundamentals(sig: fb.Signals, earnings_yield: pd.DataFrame) -> fb.Signals:
    """Achat refusé si l'entreprise perd de l'argent ou vaut plus de 60 fois ses bénéfices ;
    sans comptes disponibles (fonds, données manquantes), pas de filtre."""
    ok = earnings_yield.isna() | (earnings_yield >= 1.0 / MAX_PE)
    return replace(sig, entry=sig.entry & ok)


# ---------------------------------------------------------------------------
# 2. Vrais flux des ETF
# ---------------------------------------------------------------------------

def with_real_flows(data: fb.MarketData, path: Path) -> Optional[fb.MarketData]:
    if not path.exists():
        return None
    fl = pd.read_csv(path, parse_dates=["date"])
    flows = fl.pivot_table(index="date", columns="vehicle", values="net_flow", aggfunc="sum").sort_index()
    aum = fl.pivot_table(index="date", columns="vehicle", values="aum", aggfunc="last").sort_index()
    missing = set(data.assets["flow_vehicle"]) - set(flows.columns)
    for v in missing:  # véhicule sans vrais flux : on garde le proxy
        flows[v] = data.flows[v]
        aum[v] = data.aum[v]
    return replace(data, flows=flows.sort_index(), aum=aum.sort_index())


# ---------------------------------------------------------------------------
# Banc d'essai
# ---------------------------------------------------------------------------

def subset(data: fb.MarketData, assets: list[str]) -> fb.MarketData:
    """Les mêmes données, réduites à quelques actions (et à leurs véhicules de flux)."""
    meta = data.assets.loc[assets]
    vehicles = list(dict.fromkeys(meta["flow_vehicle"]))
    cut = lambda f: f.reindex(columns=assets) if f is not None else None
    extras = data.extras
    if extras is not None:
        extras = fb.RadarExtras(**{t: (getattr(extras, t)[getattr(extras, t)["asset"].isin(assets)]
                                       if getattr(extras, t) is not None else None) for t in extras.TABLES})
    return fb.MarketData(
        prices=cut(data.prices), holdings=data.holdings[data.holdings["asset"].isin(assets)],
        flows=data.flows[vehicles], aum=data.aum[vehicles], assets=meta, high=cut(data.high), low=cut(data.low),
        volume=cut(data.volume), offexchange=cut(data.offexchange), offexchange_short=cut(data.offexchange_short),
        extras=extras, hourly=None)


def summarize(result: fb.BacktestResult, label: str, start: pd.Timestamp) -> dict:
    eq = result.equity[result.equity.index >= start]
    m = fb.compute_metrics(eq, result.config.risk_free_rate)
    trades = result.trades[result.trades["first_fill"] >= start]
    return {"variante": label, "rendement annuel": m["cagr"], "Sharpe": m["sharpe"], "pire perte": m["max_drawdown"],
            "achats": int(len(trades)), "gagnants": float((trades["return"] > 0).mean()) if len(trades) else np.nan,
            "exposition moyenne": float(result.weights[result.weights.index >= start].sum(axis=1).mean())}


def run_variants(data: fb.MarketData, engine_dir: Path, base_cfg: Optional[fb.StrategyConfig] = None,
                 start: Optional[pd.Timestamp] = None) -> tuple[pd.DataFrame, dict]:
    """Programme actuel, chaque amélioration seule, puis 1 + 4 + 6 + 2 ensemble."""
    base_cfg = base_cfg or fb.StrategyConfig(**PROXY_THRESHOLDS)
    risk_cfg = replace(base_cfg, target_vol=0.12, max_entry_correlation=0.85)
    sig = fb.compute_signals(data, base_cfg)
    start = start or sig.calendar[min(len(sig.calendar) - 1, 3 * 252)]
    macro = load_macro(engine_dir / "macro.csv")
    ey = load_fundamentals(engine_dir / "fundamentals.csv", sig)
    real = with_real_flows(data, engine_dir / "etf_flows_real.csv")
    real_cfg = replace(base_cfg, **REAL_FLOW_THRESHOLDS)
    real_sig = fb.compute_signals(real, real_cfg) if real is not None else None
    details = {}

    def run(label: str, d: fb.MarketData, cfg: fb.StrategyConfig, s: Optional[fb.Signals]) -> dict:
        if s is None:
            return {"variante": label, "note": "données absentes"}
        return summarize(fb.run_backtest(d, cfg, signals=s), label, start)

    baseline = fb.run_backtest(data, base_cfg, signals=sig)
    bench = baseline.benchmark[baseline.benchmark.index >= start]
    bm = fb.compute_metrics(bench, base_cfg.risk_free_rate)
    rows = [{"variante": "Référence : acheter et garder les mêmes actions", "rendement annuel": bm["cagr"],
             "Sharpe": bm["sharpe"], "pire perte": bm["max_drawdown"], "achats": len(data.assets),
             "gagnants": np.nan, "exposition moyenne": 1.0},
            summarize(baseline, "Programme actuel", start)]
    cal_sig, table = apply_calibration(sig)
    details["calibrage"] = table
    rows.append(run("1 · Calibrage des indices", data, base_cfg, cal_sig))
    rows.append(run("2 · Vrais flux des ETF", real, real_cfg, real_sig))
    rows.append({"variante": "3 · Options", "note": "pas d'historique gratuit"})
    rows.append(run("4 · Météo du marché", data, base_cfg, apply_regime(sig, macro) if macro is not None else None))
    rows.append(run("5 · Comptes des entreprises", data, base_cfg, apply_fundamentals(sig, ey) if ey is not None else None))
    rows.append(run("6 · Contrôle des risques", data, risk_cfg, sig))
    rows.append({"variante": "7 · Actualités", "note": "clés Alpaca absentes de cette session"})
    rows.append({"variante": "8 · Coût d'emprunt", "note": "pas d'historique gratuit"})
    combo = None
    if real_sig is not None and macro is not None:
        combo, _ = apply_calibration(real_sig)
        combo = apply_regime(combo, macro)
    rows.append(run("1 + 4 + 6 + 2 ensemble", real, replace(real_cfg, target_vol=0.12, max_entry_correlation=0.85),
                    combo))
    details["début"], details["fin"] = start, sig.calendar[-1]
    return pd.DataFrame(rows), details


def format_table(df: pd.DataFrame) -> str:
    lines = ["| Variante | Rendement annuel | Sharpe | Pire perte | Achats | Gagnants | Exposition |",
             "|---|---|---|---|---|---|---|"]
    for r in df.to_dict("records"):
        if isinstance(r.get("note"), str):
            lines.append(f"| {r['variante']} | non testable : {r['note']} | | | | | |")
            continue
        wins = f"{r['gagnants']:.0%}" if np.isfinite(r["gagnants"]) else "—"
        lines.append(f"| {r['variante']} | {r['rendement annuel']:+.1%} | {r['Sharpe']:.2f} | {r['pire perte']:.1%} | "
                     f"{r['achats']} | {wins} | {r['exposition moyenne']:.0%} |")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="Améliorations de l'analyse : banc d'essai")
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--n-actions", type=int, default=3, help="actions tirées au hasard (0 = tout l'univers)")
    parser.add_argument("--seed", type=int, default=2026)
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)
    engine_dir = Path(args.data_dir)
    data = fb.load_market_from_csv(engine_dir)
    equities = [a for a in data.assets.index if data.assets.at[a, "asset_class"] == "EQUITY"]
    if args.n_actions:
        rng = np.random.default_rng(args.seed)
        chosen = sorted(rng.choice(equities, size=args.n_actions, replace=False).tolist())
        data = subset(data, chosen)
        title = f"{args.n_actions} actions tirées au hasard (graine {args.seed}) : {', '.join(chosen)}"
    else:
        title = f"Tout l'univers : {len(equities)} actions"
    table, details = run_variants(data, engine_dir)
    text = f"## {title}\n\nPériode mesurée : {details['début']:%d/%m/%Y} → {details['fin']:%d/%m/%Y}\n\n{format_table(table)}\n"
    print(text)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        with open(args.out, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")
        cal = details.get("calibrage")
        if cal is not None and len(cal):
            last = cal[cal["année"] == cal["année"].max()].sort_values("poids", ascending=False)
            with open(Path(args.out).with_suffix(".calibrage.csv"), "w", encoding="utf-8") as fh:
                last.to_csv(fh, index=False)


if __name__ == "__main__":
    main()
