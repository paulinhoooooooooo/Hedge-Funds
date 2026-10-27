# -*- coding: utf-8 -*-
"""
melange_radar.py — Mélanger le fonds et le Radar des grands acteurs
===================================================================

Suite de radar_seul.py (demande des fondateurs, 27/10/2026). Même moteur et mêmes règles de gestion
pour toutes les variantes (12 lignes de même poids, frais, gestion du risque, trésorerie placée dans
le S&P 500) :
  * fonds actuel : signaux du fonds ; le radar classe les candidats et bloque les achats pendant une
    distribution ;
  * radar seul : achat sur alerte d'accumulation, vente sur alerte de distribution ;
  * fonds OU radar : achat sur un signal du fonds, ou sur une alerte d'accumulation du radar s'il reste
    de la place (les signaux du fonds passent en premier) ; ventes selon les règles du fonds ;
  * fonds ET radar : signal du fonds retenu seulement si le radar voit aussi au moins une trace
    d'accumulation (score >= +1) ;
  * deux poches : moitié du capital dans le fonds, moitié dans le radar seul, rééquilibrées chaque mois.

Usage : python backtest/melange_radar.py [--data-dir data/phase1/engine] [--json sortie.json]
Sans fichiers moteur, l'analyse tourne sur le marché synthétique (démonstration, aucune valeur réelle).
"""

from __future__ import annotations

import argparse
import json
from dataclasses import replace
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

import flow_backtest as fb
import radar_seul as rs
import smart_money as sm
from institutional_radar import ALERT_THRESHOLD

CONFIRMATION = 1.0  # fonds ET radar : au moins une trace d'accumulation (le jour vaut 1 point)
FUND_FIRST = 10.0  # fonds OU radar : bonus de classement des signaux du fonds


def radar_score(sig: fb.Signals) -> pd.DataFrame:
    return sig.radar.score.reindex(index=sig.calendar, columns=sig.prices.columns)


def union_signals(sig: fb.Signals) -> fb.Signals:
    """Fonds OU radar : les alertes d'accumulation s'ajoutent aux signaux du fonds, qui restent prioritaires."""
    acc = (radar_score(sig) >= ALERT_THRESHOLD).fillna(False) & sig.price_valid
    return replace(sig, entry=sig.entry | acc, score=sig.score.fillna(0.0) + FUND_FIRST * sig.entry)


def intersection_signals(sig: fb.Signals, threshold: float = CONFIRMATION) -> fb.Signals:
    """Fonds ET radar : un signal du fonds n'est retenu que si le radar voit une accumulation."""
    return replace(sig, entry=sig.entry & (radar_score(sig) >= threshold).fillna(False))


def two_pockets(a: pd.Series, b: pd.Series, weight: float = 0.5) -> pd.Series:
    """Deux poches rééquilibrées le premier jour de chaque mois (sans frais de rééquilibrage)."""
    idx = a.index.intersection(b.index)
    ra, rb = a.reindex(idx).pct_change().fillna(0.0), b.reindex(idx).pct_change().fillna(0.0)
    nav, va, vb, out, month = 1.0, weight, 1.0 - weight, np.empty(len(idx)), None
    for k, t in enumerate(idx):
        if month is not None and t.month != month:
            va, vb = nav * weight, nav * (1.0 - weight)
        month = t.month
        va *= 1.0 + ra.iat[k]
        vb *= 1.0 + rb.iat[k]
        nav = va + vb
        out[k] = nav
    return pd.Series(out * float(a.reindex(idx).iloc[0]), index=idx, name="deux poches")


def curve_summary(eq: pd.Series) -> dict:
    m = fb.compute_metrics(eq, fb.StrategyConfig().risk_free_rate)
    return {"rendement_annuel": m["cagr"], "rendement_total": m["total_return"], "sharpe": m["sharpe"],
            "pire_perte": m["max_drawdown"], "volatilite": m["volatility"]}


def activity(result: fb.BacktestResult, sig: fb.Signals) -> dict:
    """Activité du portefeuille : combien de décisions, combien de temps on garde une ligne, et pourquoi on sort."""
    tr = result.trades
    end = result.equity.index[-1]
    closed = tr[tr["exit_reason"] != "EN_COURS"]
    held_days = closed["holding_days"].astype(float).tolist() + [
        float((end - pd.Timestamp(t)).days) for t in tr.loc[tr["exit_reason"] == "EN_COURS", "first_fill"]]
    j = result.journal["action"].value_counts()
    held = result.weights > 0
    alerts = (radar_score(sig) >= ALERT_THRESHOLD).fillna(False) & sig.price_valid
    return {"allers_retours_clos": int(len(closed)), "lignes_encore_ouvertes": int(len(tr) - len(closed)),
            "duree_mediane_jours": float(np.median(held_days)) if held_days else float("nan"),
            "lignes_moyennes": float(result.n_positions.mean()),
            "jours_portefeuille_plein": float((result.n_positions >= result.config.max_positions).mean()),
            "achats": int(j.get("ENTRY", 0)), "renforcements": int(j.get("ADD_REACCUMULATION", 0)),
            "allegements": int(j.get("REDUCE_DISTRIBUTION_ALERT", 0) + j.get("RISK_DRAWDOWN", 0) + j.get("RISK_TRIM", 0)),
            "ventes": int(sum(j.get(a, 0) for a in ("EXIT_DISTRIBUTION_CONFIRMED", "EXIT_INSTITUTIONAL_LIQUIDATION", "RISK_STOP"))),
            "raisons_de_sortie": {fb.ACTION_LABELS.get(k, k): int(v) for k, v in closed["exit_reason"].value_counts().items()},
            "alertes_accumulation": int(alerts.to_numpy().sum()),
            "alertes_sur_titre_non_detenu": int((alerts & ~held.reindex_like(alerts).fillna(False)).to_numpy().sum())}


def pct(x: float, d: int = 1) -> str:
    return rs.pct(x, d)


NAMES = {"fonds": "Fonds actuel", "radar_seul": "Radar seul", "fonds_ou_radar": "Fonds OU radar",
         "fonds_et_radar": "Fonds ET radar", "deux_poches": "Deux poches 50/50", "spy": "S&P 500"}


def report(res: dict) -> str:
    v = res["variantes"]
    lines = [f"# Mélanges du fonds et du radar — {res['source']}", "",
             f"## 1. Backtest du {res['debut']} au {res['fin']} (mêmes règles, 12 lignes, frais compris)", "",
             "| | Rendement annuel | Pire perte | Sharpe | Volatilité | Part investie | Allers-retours | Réussite |",
             "|---|---|---|---|---|---|---|---|"]
    for k, x in v.items():
        inv = f"{x['part_investie_moyenne']:.0%}" if "part_investie_moyenne" in x else ""
        n = str(x["allers_retours"]) if "allers_retours" in x else ""
        win = pct(x["taux_de_reussite"], 0) if "taux_de_reussite" in x else ""
        lines.append(f"| {NAMES[k]} | {pct(x['rendement_annuel'])} | {pct(x['pire_perte'])} | {x['sharpe']:.2f} | "
                     f"{x['volatilite']:.0%} | {inv} | {n} | {win} |")
    h = res["horizons"]
    keys = list(h["fenetres"]["1 an"].keys())
    lines += ["", f"## 2. Par période (jusqu'au {h['fin']})", "",
              "| Période | " + " | ".join(NAMES[k] for k in keys) + " |", "|---" * (len(keys) + 1) + "|"]
    for label, row in list(h["fenetres"].items()) + [(f"année {y}", row) for y, row in h["annees"].items()]:
        lines.append(f"| {label} | " + " | ".join(pct(row.get(k, np.nan)) for k in keys) + " |")
    lines += ["", f"Années où chaque variante bat le fonds actuel : " + " ; ".join(
        f"{NAMES[k]} {n}/{len(h['annees'])}" for k, n in res["annees_gagnees"].items())]
    a = res["activite"]
    lines += ["", "## 3. Activité (vérification du nombre d'allers-retours)", "",
              "| | Achats | Renforcements | Allègements | Ventes complètes | Allers-retours clos | Lignes encore ouvertes | "
              "Durée médiane d'une ligne | Lignes en moyenne | Jours à 12 lignes (plein) |", "|---" * 10 + "|"]
    for k, x in a.items():
        lines.append(f"| {NAMES[k]} | {x['achats']} | {x['renforcements']} | {x['allegements']} | {x['ventes']} | "
                     f"{x['allers_retours_clos']} | {x['lignes_encore_ouvertes']} | {x['duree_mediane_jours']:.0f} jours | "
                     f"{x['lignes_moyennes']:.1f} | {x['jours_portefeuille_plein']:.0%} |")
    for k, x in a.items():
        lines.append(f"- {NAMES[k]} : sorties — " + (", ".join(f"{r} : {n}" for r, n in x["raisons_de_sortie"].items()) or "aucune"))
    x = a["radar_seul"]
    lines.append(f"- Alertes d'accumulation du radar sur la période : {x['alertes_accumulation']} (titre × séance), "
                 f"dont {x['alertes_sur_titre_non_detenu']} sur un titre que le radar seul ne détenait pas ; "
                 f"il n'en a acheté que {x['achats']}.")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> dict:
    parser = argparse.ArgumentParser(description="Mélanges du fonds et du Radar des grands acteurs")
    parser.add_argument("--data-dir", default=str(rs.ROOT / "data" / "phase1" / "engine"))
    parser.add_argument("--json", help="écrire aussi les résultats dans ce fichier JSON")
    args = parser.parse_args(argv)
    engine = Path(args.data_dir)
    synthetic = not (engine / "prices.csv").exists()
    data = fb.generate_synthetic_market() if synthetic else fb.load_market_from_csv(engine)
    cfg = sm.phase1_config(data)
    fund = fb.run_backtest(data, cfg)
    sig = fund.signals
    runs = {"fonds": fund,
            "radar_seul": fb.run_backtest(data, cfg, signals=rs.radar_only_signals(sig)),
            "fonds_ou_radar": fb.run_backtest(data, cfg, signals=union_signals(sig)),
            "fonds_et_radar": fb.run_backtest(data, cfg, signals=intersection_signals(sig))}
    spy = rs.spy_series(data, synthetic)
    variants = {k: rs.summary(r, spy) for k, r in runs.items()}
    for x in variants.values():
        x.pop("spy", None)
    pockets = two_pockets(runs["fonds"].equity, runs["radar_seul"].equity)
    variants["deux_poches"] = curve_summary(pockets)
    variants["deux_poches"]["part_investie_moyenne"] = float(
        (runs["fonds"].gross.mean() + runs["radar_seul"].gross.mean()) / 2)
    curves = {k: r.equity for k, r in runs.items()}
    curves["deux_poches"] = pockets
    if spy is not None:
        s = spy.reindex(fund.equity.index).ffill().dropna()
        variants["spy"] = curve_summary(s)
        curves["spy"] = s
    hz = rs.horizons(curves)
    wins = {k: sum(row[k] > row["fonds"] for row in hz["annees"].values())
            for k in curves if k not in ("fonds", "spy")}
    res = {"source": "DONNÉES SYNTHÉTIQUES de démonstration" if synthetic else "données réelles",
           "debut": f"{fund.equity.index[0]:%Y-%m-%d}", "fin": f"{fund.equity.index[-1]:%Y-%m-%d}",
           "variantes": variants, "horizons": hz, "annees_gagnees": wins,
           "activite": {k: activity(r, sig) for k, r in runs.items()}}
    print(report(res))
    if args.json:
        Path(args.json).write_text(json.dumps(res, ensure_ascii=False, indent=1, default=float), encoding="utf-8")
    return res


if __name__ == "__main__":
    main()
