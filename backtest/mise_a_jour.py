# -*- coding: utf-8 -*-
"""
mise_a_jour.py — Mise à jour quotidienne du fonds (données réelles, phase 1)
============================================================================

Une seule commande, lancée chaque soir de semaine après la clôture de New York :

    python backtest/mise_a_jour.py

1. télécharge toutes les données (étapes de phase1_data.py ; une étape en échec n'arrête pas les
   suivantes, elle est signalée) ;
2. relance le fonds avec les réglages adoptés (smart_money.phase1_config) ;
3. réécrit la page Desk (outputs/desk_smart_money.html) ;
4. écrit outputs/notification.md : les mouvements de portefeuille décidés à la dernière séance,
   en français simple, prêts à être envoyés aux fondateurs.

Le conteneur étant vidé entre deux sessions, tout est retéléchargé à chaque passage (2 à 4 heures).
Les décisions du fonds ne dépendent que des données : relancer le calcul redonne les mêmes décisions
passées, seules celles de la dernière séance sont nouvelles.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import dashboard  # noqa: E402
import flow_backtest as fb  # noqa: E402
import phase1_data as p1  # noqa: E402
import smart_money as sm  # noqa: E402

STAGES = ["sec", "universe", "figi", "sectors", "prices", "finra", "cot", "ats", "short", "events",
          "insiders", "alpaca", "build", "names", "buyers"]
OUT = p1.ROOT / "outputs"
MOVES = {
    "ENTRY": "ACHAT", "ADD_REACCUMULATION": "RENFORCEMENT", "REDUCE_DISTRIBUTION_ALERT": "ALLÈGEMENT",
    "EXIT_DISTRIBUTION_CONFIRMED": "VENTE", "EXIT_INSTITUTIONAL_LIQUIDATION": "VENTE", "RISK_STOP": "VENTE URGENTE",
    "RISK_DRAWDOWN": "DÉ-RISQUAGE", "RISK_TRIM": "ÉCRÊTAGE",
}


def run_stages(block_days: int) -> list[str]:
    failed = []
    for stage in STAGES:
        print(f"=== {stage} ===", flush=True)
        cmd = [sys.executable, str(HERE / "phase1_data.py"), stage, "--block-days", str(block_days)]
        if subprocess.run(cmd).returncode != 0:
            failed.append(stage)
    return failed


def notification(result: fb.BacktestResult, failed: list[str], spy: pd.Series) -> str:
    last = result.equity.index[-1]
    today = result.journal[pd.to_datetime(result.journal["date"]) == last]
    m = result.metrics
    day = result.equity.pct_change().iloc[-1]
    spy_day = spy.reindex(result.equity.index).ffill().pct_change().iloc[-1]
    index_share = result.index_exposure.iloc[-1] if result.index_exposure is not None else 0.0
    lines = [f"# Fonds — séance du {last:%d/%m/%Y}", ""]
    if today.empty:
        lines.append("Aucun mouvement de portefeuille aujourd'hui.")
    else:
        lines.append(f"{len(today)} mouvement(s) de portefeuille :")
        for r in today.itertuples():
            lines.append(f"- **{MOVES.get(r.action, r.action)} {r.asset}** à {r.price:,.2f} $ — {r.label}")
    lines += [
        "",
        f"- Séance : fonds {day:+.2%}, S&P 500 {spy_day:+.2%}",
        f"- Lignes détenues : {int((result.scales.iloc[-1] > 0).sum())} ; actions {result.gross.iloc[-1]:.0%}, "
        f"S&P 500 {index_share:.0%}",
        f"- Depuis 2016 : {m['cagr']:+.1%} par an ; pire perte {m['max_drawdown']:.1%}",
    ]
    if failed:
        lines += ["", f"⚠️ Étapes de données en échec (données de la veille utilisées) : {', '.join(failed)}"]
    lines += ["", "Aucun ordre réel n'est passé : simulation sur données réelles, pas un conseil d'investissement."]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> str:
    parser = argparse.ArgumentParser(description="Mise à jour quotidienne du fonds")
    parser.add_argument("--skip-download", action="store_true", help="utiliser les données déjà présentes")
    parser.add_argument("--block-days", type=int, default=5, help="séances de gros blocs (radar : 5 suffisent)")
    args = parser.parse_args(argv)
    failed = [] if args.skip_download else run_stages(args.block_days)
    engine = p1.DATA / "engine"
    data = fb.load_market_from_csv(engine)
    result = fb.run_backtest(data, sm.phase1_config(data))
    dashboard.main(["--data-dir", str(engine), "--out", str(OUT / "desk_smart_money.html")])
    spy = data.market if data.market is not None else result.benchmark
    text = notification(result, failed, spy)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "notification.md").write_text(text, encoding="utf-8")
    print(text)
    return text


if __name__ == "__main__":
    main()
