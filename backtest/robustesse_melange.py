# -*- coding: utf-8 -*-
"""
robustesse_melange.py — Le mélange « fonds OU radar » tient-il avec d'autres réglages ?
=======================================================================================

Suite de melange_radar.py (demande des fondateurs, 27/10/2026). Avant d'envisager de changer le fonds,
on vérifie que l'avantage du mélange ne tient pas à un seul réglage ou à une seule période :
  1. seuil d'alerte du radar : 2,0 · 2,5 (réglage du fonds) · 3,0 · 3,5 ;
  2. nombre de lignes : 8 · 12 · 16 (le fonds et le mélange avec le même nombre) ;
  3. frais doublés ;
  4. sous-périodes : 2016-2019, 2020-2022, 2023 à aujourd'hui ;
  5. test du hasard : mêmes places libres remplies par des titres tirés au sort (autant de titres
     chaque jour que d'alertes du radar). Si le hasard fait aussi bien, le gain vient du fait d'être
     plus investi en actions, pas du radar.

Usage : python backtest/robustesse_melange.py [--data-dir data/phase1/engine] [--json sortie.json] [--tirages 5]
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
import melange_radar as mr
import radar_seul as rs
import smart_money as sm
from institutional_radar import ALERT_THRESHOLD

THRESHOLDS = (2.0, 2.5, 3.0, 3.5)
LINES = (8, 12, 16)
PERIODS = (("2016-2019", "2016-01-01", "2019-12-31"), ("2020-2022", "2020-01-01", "2022-12-31"),
           ("2023 à aujourd'hui", "2023-01-01", None))


def union_with(sig: fb.Signals, alerts: pd.DataFrame) -> fb.Signals:
    """Fonds OU `alerts` : les signaux du fonds passent en premier, les alertes comblent les places libres."""
    return replace(sig, entry=sig.entry | (alerts.fillna(False) & sig.price_valid),
                   score=sig.score.fillna(0.0) + mr.FUND_FIRST * sig.entry)


def random_alerts(sig: fb.Signals, alerts: pd.DataFrame, seed: int) -> pd.DataFrame:
    """Autant de titres tirés au sort chaque jour que d'alertes du radar, parmi les titres cotés ce jour-là."""
    rng = np.random.default_rng(seed)
    valid = sig.price_valid.to_numpy(bool)
    counts = alerts.fillna(False).to_numpy(bool).sum(axis=1)
    out = np.zeros_like(valid)
    for i, n in enumerate(counts):
        pool = np.flatnonzero(valid[i])
        if n and len(pool):
            out[i, rng.choice(pool, size=min(n, len(pool)), replace=False)] = True
    return pd.DataFrame(out, index=alerts.index, columns=alerts.columns)


def metrics(eq: pd.Series, start: Optional[str] = None, end: Optional[str] = None) -> dict:
    s = eq[(eq.index >= pd.Timestamp(start)) if start else slice(None)]
    if end:
        s = s[s.index <= pd.Timestamp(end)]
    m = fb.compute_metrics(s, fb.StrategyConfig().risk_free_rate)
    return {"rendement_annuel": m["cagr"], "pire_perte": m["max_drawdown"], "sharpe": m["sharpe"]}


def main(argv: Optional[list[str]] = None) -> dict:
    parser = argparse.ArgumentParser(description="Robustesse du mélange fonds OU radar")
    parser.add_argument("--data-dir", default=str(rs.ROOT / "data" / "phase1" / "engine"))
    parser.add_argument("--json", help="écrire aussi les résultats dans ce fichier JSON")
    parser.add_argument("--tirages", type=int, default=5, help="nombre de tirages au sort du test du hasard")
    args = parser.parse_args(argv)
    engine = Path(args.data_dir)
    synthetic = not (engine / "prices.csv").exists()
    data = fb.generate_synthetic_market() if synthetic else fb.load_market_from_csv(engine)
    cfg = sm.phase1_config(data)
    fund = fb.run_backtest(data, cfg)
    sig = fund.signals
    score = mr.radar_score(sig)
    alerts = score >= ALERT_THRESHOLD

    rows = []  # (famille, réglage, fonds, mélange)
    base = fb.run_backtest(data, cfg, signals=union_with(sig, alerts))
    for th in THRESHOLDS:
        run = base if th == ALERT_THRESHOLD else fb.run_backtest(data, cfg, signals=union_with(sig, score >= th))
        rows.append(("Seuil d'alerte du radar", f"{th:.1f}".replace(".", ","), metrics(fund.equity), metrics(run.equity)))
    for n in LINES:
        c = replace(cfg, max_positions=n)
        f = fund if n == cfg.max_positions else fb.run_backtest(data, c, signals=sig)
        u = base if n == cfg.max_positions else fb.run_backtest(data, c, signals=union_with(sig, alerts))
        rows.append(("Nombre de lignes", str(n), metrics(f.equity), metrics(u.equity)))
    c2 = replace(cfg, cost_multiplier=cfg.cost_multiplier * 2)
    rows.append(("Frais", "doublés", metrics(fb.run_backtest(data, c2, signals=sig).equity),
                 metrics(fb.run_backtest(data, c2, signals=union_with(sig, alerts)).equity)))
    periods = [(label, metrics(fund.equity, a, b), metrics(base.equity, a, b)) for label, a, b in PERIODS
               if pd.Timestamp(a) <= fund.equity.index[-1] and (b is None or pd.Timestamp(b) >= fund.equity.index[0])]
    draws = [metrics(fb.run_backtest(data, cfg, signals=union_with(sig, random_alerts(sig, alerts, seed))).equity)
             for seed in range(args.tirages)]

    res = {"source": "DONNÉES SYNTHÉTIQUES de démonstration" if synthetic else "données réelles",
           "debut": f"{fund.equity.index[0]:%Y-%m-%d}", "fin": f"{fund.equity.index[-1]:%Y-%m-%d}",
           "reglages": [{"famille": a, "reglage": b, "fonds": f, "fonds_ou_radar": u} for a, b, f, u in rows],
           "periodes": [{"periode": a, "fonds": f, "fonds_ou_radar": u} for a, f, u in periods],
           "hasard": {"tirages": draws, "fonds": metrics(fund.equity), "fonds_ou_radar": metrics(base.equity)}}
    print(report(res))
    if args.json:
        Path(args.json).write_text(json.dumps(res, ensure_ascii=False, indent=1, default=float), encoding="utf-8")
    return res


def cell(m: dict) -> str:
    return f"{rs.pct(m['rendement_annuel'])} · {rs.pct(m['pire_perte'])} · {m['sharpe']:.2f}".replace(".", ",")


def report(res: dict) -> str:
    lines = [f"# Robustesse du mélange fonds OU radar — {res['source']}", "",
             f"Backtest du {res['debut']} au {res['fin']}. Chaque case : rendement par an · pire chute · Sharpe.", "",
             "## 1. Autres réglages", "", "| Réglage | Fonds | Fonds OU radar | Écart de rendement par an |",
             "|---|---|---|---|"]
    for r in res["reglages"]:
        gap = r["fonds_ou_radar"]["rendement_annuel"] - r["fonds"]["rendement_annuel"]
        lines.append(f"| {r['famille']} : {r['reglage']} | {cell(r['fonds'])} | {cell(r['fonds_ou_radar'])} | {rs.pct(gap)} |")
    lines += ["", "## 2. Sous-périodes", "", "| Période | Fonds | Fonds OU radar |", "|---|---|---|"]
    for r in res["periodes"]:
        lines.append(f"| {r['periode']} | {cell(r['fonds'])} | {cell(r['fonds_ou_radar'])} |")
    h = res["hasard"]
    ann = [d["rendement_annuel"] for d in h["tirages"]]
    shp = [d["sharpe"] for d in h["tirages"]]
    lines += ["", "## 3. Test du hasard (places libres remplies par des titres tirés au sort)", "",
              f"- Fonds : {cell(h['fonds'])}",
              f"- Fonds OU radar : {cell(h['fonds_ou_radar'])}",
              f"- Fonds OU hasard ({len(ann)} tirages) : rendement par an {rs.pct(float(np.mean(ann)))} en moyenne "
              f"(de {rs.pct(min(ann))} à {rs.pct(max(ann))}), Sharpe {np.mean(shp):.2f} (de {min(shp):.2f} à {max(shp):.2f})"
              .replace(".", ","),
              f"- Tirages qui battent le radar : {sum(a >= h['fonds_ou_radar']['rendement_annuel'] for a in ann)} sur {len(ann)}"]
    return "\n".join(lines)


if __name__ == "__main__":
    main()
