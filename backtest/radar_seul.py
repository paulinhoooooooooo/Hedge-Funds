# -*- coding: utf-8 -*-
"""
radar_seul.py — Et si le fonds ne suivait que le Radar des grands acteurs ?
==========================================================================

Question des fondateurs (27/10/2026) : quel serait le rendement d'un portefeuille qui achète les
alertes d'accumulation du radar, comparé au portefeuille du fonds ?

Même moteur et mêmes règles de gestion que le fonds (smart_money.phase1_config : 12 lignes de même
poids, frais, durée minimale de détention, délai avant un ré-achat, gestion du risque, trésorerie
placée dans le S&P 500). Seuls les signaux changent :
  * achat : alerte d'accumulation du radar (score >= +2,5), candidats classés par score ;
  * vente : alerte de distribution du radar (score <= -2,5) sur une ligne détenue.

Trois mesures :
  1. backtest depuis 2016 : le fonds, le radar seul et le S&P 500 ;
  2. depuis le départ du portefeuille réel (smart_money.LIVE_START), avec les règles du portefeuille
     réel (suivi_signaux.follow) appliquées d'un côté aux signaux du fonds, de l'autre à ceux du radar ;
  3. ce qui suit une alerte : écart moyen avec le S&P 500, 20 et 60 séances après.

Usage : python backtest/radar_seul.py [--data-dir data/phase1/engine] [--json sortie.json]
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
import smart_money as sm
import suivi_signaux as ss
from institutional_radar import ALERT_THRESHOLD

ROOT = Path(__file__).resolve().parents[1]
HORIZONS = (20, 60)


def radar_only_signals(sig: fb.Signals) -> fb.Signals:
    """Signaux du fonds remplacés par ceux du radar seul (achat = accumulation, vente = distribution)."""
    if sig.radar is None:
        raise ValueError("Radar indisponible : les données n'ont pas de plus haut, plus bas et volume.")
    r = sig.radar.score.reindex(index=sig.calendar, columns=sig.prices.columns)
    dist = (r <= -ALERT_THRESHOLD).fillna(False)
    none = pd.DataFrame(False, index=r.index, columns=r.columns)
    # Deux jambes de sortie à True = sortie complète (échelle de sortie du moteur) : la distribution
    # du radar vend toute la ligne, comme une distribution confirmée pour le fonds.
    return replace(sig, entry=(r >= ALERT_THRESHOLD).fillna(False) & sig.price_valid,
                   dist_slow=dist, dist_fast=dist, dist_hard=none, dist_foot=none, score=r.fillna(0.0))


def spy_series(data: fb.MarketData, synthetic: bool) -> Optional[pd.Series]:
    """Même indice que la page Desk : cours ajusté du SPY, à défaut l'indice du moteur."""
    path = ROOT / "data" / "phase1" / "prices" / "SPY.csv"
    if not synthetic and path.exists():
        spy = pd.read_csv(path, parse_dates=["date"]).set_index("date")["adjClose"]
        if spy.index.tz is not None:
            spy.index = spy.index.tz_localize(None)
        return spy.sort_index()
    return data.market


def summary(result, spy: Optional[pd.Series]) -> dict:
    m = result.metrics
    trades = result.trades
    closed = trades[trades["exit_reason"] != "EN_COURS"] if len(trades) else trades
    out = {"rendement_annuel": m["cagr"], "rendement_total": m["total_return"], "sharpe": m["sharpe"],
           "pire_perte": m["max_drawdown"], "volatilite": m["volatility"],
           "part_investie_moyenne": float(result.gross.mean()),
           "allers_retours": int(len(closed)),
           "taux_de_reussite": float((closed["pnl"] > 0).mean()) if len(closed) else float("nan"),
           "debut": f"{m['start']:%Y-%m-%d}", "fin": f"{m['end']:%Y-%m-%d}"}
    if spy is not None:
        s = spy.reindex(result.equity.index).ffill().dropna()
        b = fb.compute_metrics(s, fb.StrategyConfig().risk_free_rate)
        out["spy"] = {"rendement_annuel": b["cagr"], "sharpe": b["sharpe"], "pire_perte": b["max_drawdown"]}
    return out


def live_summary(live, spy: Optional[pd.Series], start: str) -> dict:
    eq = live.equity[live.equity.index >= pd.Timestamp(start)]
    last = live.signals.prices.ffill().iloc[-1]
    held = live.trades[live.trades["exit_reason"] == "EN_COURS"]
    out = {"rendement": float(eq.iloc[-1] / eq.iloc[0] - 1.0) if len(eq) > 1 else 0.0,
           "especes": float(live.cash.iloc[-1] / eq.iloc[-1]) if len(eq) else 1.0,
           "lignes": [{"titre": r.asset, "achat": f"{pd.Timestamp(r.first_fill):%d/%m/%Y}",
                       "prix_achat": float(r.avg_entry_price), "cours": float(last.get(r.asset, np.nan)),
                       "rendement": float(r["return"])} for _, r in held.iterrows()],
           "mouvements": [{"date": f"{pd.Timestamp(m.date):%d/%m/%Y}", "titre": m.asset, "action": m.label,
                           "prix": float(m.price)} for m in live.journal.itertuples()]}
    if spy is not None and len(eq) > 1:
        s = spy.reindex(eq.index).ffill()
        out["spy"] = float(s.iloc[-1] / s.iloc[0] - 1.0)
    return out


def alert_study(sig: fb.Signals, spy: Optional[pd.Series]) -> dict:
    """Écart moyen avec l'indice après une alerte (toutes les séances d'alerte, sans sélection)."""
    px, valid = sig.prices, sig.price_valid
    r = sig.radar.score.reindex(index=px.index, columns=px.columns)
    bench = (spy.reindex(px.index).ffill() if spy is not None else px.mean(axis=1))
    out = {}
    for h in HORIZONS:
        fwd = px.shift(-h) / px - 1.0
        excess = fwd.sub(bench.shift(-h) / bench - 1.0, axis=0).where(valid & fwd.notna())
        row = {}
        for name, mask in (("accumulation", r >= ALERT_THRESHOLD), ("distribution", r <= -ALERT_THRESHOLD),
                           ("toutes_les_seances", pd.DataFrame(True, index=r.index, columns=r.columns))):
            vals = excess.where(mask.fillna(False)).stack().dropna()
            row[name] = {"n": int(len(vals)), "ecart_moyen": float(vals.mean()) if len(vals) else float("nan"),
                         "ecart_median": float(vals.median()) if len(vals) else float("nan"),
                         "part_au_dessus": float((vals > 0).mean()) if len(vals) else float("nan")}
        out[f"{h}_seances"] = row
    return out


def pct(x: float, d: int = 1) -> str:
    return "n.d." if x is None or not np.isfinite(x) else f"{x:+.{d}%}".replace(".", ",")


def report(res: dict) -> str:
    f, r = res["backtest"]["fonds"], res["backtest"]["radar_seul"]
    lines = [f"# Radar seul face au fonds — {res['source']}", "",
             f"## 1. Backtest du {f['debut']} au {f['fin']} (mêmes règles, 12 lignes, frais compris)", "",
             "| | Fonds | Radar seul | S&P 500 |", "|---|---|---|---|"]
    spy = f.get("spy", {})
    lines += [f"| Rendement annuel | {pct(f['rendement_annuel'])} | {pct(r['rendement_annuel'])} | {pct(spy.get('rendement_annuel', np.nan))} |",
              f"| Rendement total | {pct(f['rendement_total'], 0)} | {pct(r['rendement_total'], 0)} | |",
              f"| Sharpe | {f['sharpe']:.2f} | {r['sharpe']:.2f} | {spy.get('sharpe', float('nan')):.2f} |",
              f"| Pire perte | {pct(f['pire_perte'])} | {pct(r['pire_perte'])} | {pct(spy.get('pire_perte', np.nan))} |",
              f"| Part investie en actions (moyenne) | {f['part_investie_moyenne']:.0%} | {r['part_investie_moyenne']:.0%} | |",
              f"| Allers-retours clôturés | {f['allers_retours']} | {r['allers_retours']} | |",
              f"| Taux de réussite | {pct(f['taux_de_reussite'], 0)} | {pct(r['taux_de_reussite'], 0)} | |", ""]
    lf, lr = res["reel"]["fonds"], res["reel"]["radar_seul"]
    lines += [f"## 2. Depuis le départ du portefeuille réel ({res['reel']['depart']})", "",
              f"- Signaux du fonds (portefeuille réel) : {pct(lf['rendement'])} ; espèces {lf['especes']:.0%}",
              f"- Signaux du radar seul : {pct(lr['rendement'])} ; espèces {lr['especes']:.0%}",
              f"- S&P 500 : {pct(lf.get('spy', np.nan))}", ""]
    for name, lv in (("fonds", lf), ("radar seul", lr)):
        lines.append(f"Lignes détenues ({name}) : " + (", ".join(
            f"{x['titre']} {pct(x['rendement'])} (acheté le {x['achat']})" for x in lv["lignes"]) or "aucune"))
        lines.append(f"Mouvements ({name}) : " + (", ".join(
            f"{x['date']} {x['action']} {x['titre']}" for x in lv["mouvements"]) or "aucun"))
    lines += ["", "## 3. Après une alerte du radar (écart avec le S&P 500)", ""]
    for h, row in res["apres_alerte"].items():
        lines.append(f"- {h.replace('_seances', ' séances après')} : " + " ; ".join(
            f"{k.replace('_', ' ').replace('seances', 'séances')} {pct(v['ecart_moyen'])} en moyenne (médiane {pct(v['ecart_median'])}, "
            f"{v['part_au_dessus']:.0%} au-dessus, {v['n']} cas)" for k, v in row.items()))
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> dict:
    parser = argparse.ArgumentParser(description="Radar des grands acteurs seul, face au fonds")
    parser.add_argument("--data-dir", default=str(ROOT / "data" / "phase1" / "engine"))
    parser.add_argument("--json", help="écrire aussi les résultats dans ce fichier JSON")
    args = parser.parse_args(argv)
    engine = Path(args.data_dir)
    synthetic = not (engine / "prices.csv").exists()
    data = fb.generate_synthetic_market() if synthetic else fb.load_market_from_csv(engine)
    cfg = sm.phase1_config(data)
    fund = fb.run_backtest(data, cfg)
    radar = fb.run_backtest(data, cfg, signals=radar_only_signals(fund.signals))
    spy = spy_series(data, synthetic)
    start = sm.LIVE_START if not synthetic else f"{fund.equity.index[-40]:%Y-%m-%d}"
    res = {"source": "DONNÉES SYNTHÉTIQUES de démonstration" if synthetic else "données réelles",
           "backtest": {"fonds": summary(fund, spy), "radar_seul": summary(radar, spy)},
           "reel": {"depart": start, "fonds": live_summary(ss.follow(fund, start), spy, start),
                    "radar_seul": live_summary(ss.follow(radar, start), spy, start)},
           "apres_alerte": alert_study(fund.signals, spy)}
    print(report(res))
    if args.json:
        Path(args.json).write_text(json.dumps(res, ensure_ascii=False, indent=1, default=float), encoding="utf-8")
    return res


if __name__ == "__main__":
    main()
