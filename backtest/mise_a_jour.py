# -*- coding: utf-8 -*-
"""
mise_a_jour.py — Mise à jour quotidienne du fonds (données réelles, phase 1)
============================================================================

Une seule commande, lancée chaque soir de semaine après la clôture de New York :

    python backtest/mise_a_jour.py --etat <page Desk publiée la veille (fichier HTML)>

1. télécharge toutes les données (étapes de phase1_data.py ; une étape en échec est retentée une
   fois, puis signalée) ;
2. relance le portefeuille réel (démarré vide le jour smart_money.LIVE_START) et la copie Pelosi ;
3. réécrit la page Desk (outputs/desk_smart_money.html) et y range un petit état (dernière séance
   signalée, déclarations Pelosi déjà vues) ;
4. écrit outputs/notification.md (texte à envoyer) et outputs/etat_mise_a_jour.json (complet ou non).

Garde-fous :
  * le conteneur est vide à chaque passage : une étape de données en échec veut dire des données
    MANQUANTES, donc des décisions faussées. Dans ce cas la mise à jour est déclarée incomplète,
    aucun mouvement n'est annoncé et la page de la veille doit être conservée ;
  * cours périmés (aucune séance récente) : mise à jour déclarée incomplète ;
  * grâce à l'état rangé dans la page de la veille, une soirée manquée (panne, limite d'utilisation)
    n'efface aucun mouvement : le passage suivant annonce tout ce qui s'est passé depuis, sans doublon ;
    un jour férié à New York n'annonce rien de nouveau.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Optional

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
PAGE = OUT / "desk_smart_money.html"
MAX_STALE_DAYS = 4  # un week-end prolongé d'un jour férié ; au-delà, les cours ne sont plus à jour
STATE_TAG = re.compile(r'<script type="application/json" id="etat-desk">(.*?)</script>', re.S)
PTR_LINK = "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{doc}.pdf"
MOVES = {
    "ENTRY": "ACHAT", "ADD_REACCUMULATION": "RENFORCEMENT", "REDUCE_DISTRIBUTION_ALERT": "ALLÈGEMENT",
    "EXIT_DISTRIBUTION_CONFIRMED": "VENTE", "EXIT_INSTITUTIONAL_LIQUIDATION": "VENTE", "RISK_STOP": "VENTE URGENTE",
    "RISK_DRAWDOWN": "DÉ-RISQUAGE", "RISK_TRIM": "ÉCRÊTAGE",
}
DISCLAIMER = "Aucun ordre réel n'est passé : simulation sur données réelles, pas un conseil d'investissement."


# =============================================================================
# État rangé dans la page Desk publiée
# =============================================================================

def read_state(page: Optional[Path]) -> dict:
    """État de la veille, lu dans la page Desk publiée ({} si absente ou sans état)."""
    if page is None or not Path(page).exists():
        return {}
    match = STATE_TAG.search(Path(page).read_text(encoding="utf-8", errors="ignore"))
    if not match:
        return {}
    try:
        state = json.loads(match.group(1))
    except json.JSONDecodeError:
        return {}
    return state if isinstance(state, dict) else {}


def embed_state(page: Path, state: dict) -> None:
    html = STATE_TAG.sub("", page.read_text(encoding="utf-8"))
    payload = json.dumps(state, ensure_ascii=False).replace("</", "<\\/")
    page.write_text(html + f'\n<script type="application/json" id="etat-desk">{payload}</script>\n',
                    encoding="utf-8")


# =============================================================================
# Étapes
# =============================================================================

def run_stages(block_days: int, retry_wait: float = 120.0) -> list[str]:
    """Toutes les étapes ; une étape en échec est retentée une fois (panne réseau passagère)."""
    failed = []
    for stage in STAGES:
        cmd = [sys.executable, str(HERE / "phase1_data.py"), stage, "--block-days", str(block_days)]
        for attempt in (1, 2):
            print(f"=== {stage} (essai {attempt}) ===", flush=True)
            if subprocess.run(cmd).returncode == 0:
                break
            if attempt == 1:
                time.sleep(retry_wait)
        else:
            failed.append(stage)
    return failed


def stale_days(last: pd.Timestamp, now: Optional[pd.Timestamp] = None) -> int:
    now = now if now is not None else pd.Timestamp.now(tz="America/New_York")
    return (now.tz_localize(None).normalize() - pd.Timestamp(last).normalize()).days


# =============================================================================
# Notification
# =============================================================================

def new_moves(result: fb.BacktestResult, state: dict) -> tuple[pd.DataFrame, pd.Timestamp]:
    """Mouvements du portefeuille réel non encore signalés : postérieurs à la dernière séance signalée
    (sinon ceux de la dernière séance). Renvoie aussi la date de début de la période couverte."""
    last = result.equity.index[-1]
    dates = pd.to_datetime(result.journal["date"])
    since = pd.Timestamp(state["derniere_seance"]) if state.get("derniere_seance") else None
    if since is not None and since < last:
        return result.journal[dates > since], result.equity.index[result.equity.index > since][0]
    if since is not None:  # jour férié ou page déjà à jour : rien de nouveau
        return result.journal.iloc[0:0], last
    return result.journal[dates == last], last


def new_pelosi(tx: pd.DataFrame, index: pd.DataFrame, state: dict,
               previous_session: pd.Timestamp) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(transactions nouvelles, déclarations nouvelles sans transaction lisible)."""
    seen = set(map(str, state.get("pelosi_docs", [])))
    if seen:
        fresh = index[~index["doc_id"].astype(str).isin(seen)]
    else:  # premier passage : déclarations publiées depuis la séance précédente
        fresh = index[pd.to_datetime(index["filing_date"]) >= previous_session]
    fresh_tx = tx[tx["doc_id"].astype(str).isin(set(fresh["doc_id"].astype(str)))]
    unreadable = fresh[(~fresh["readable"].astype(bool)) | (fresh["n_transactions"] == 0)]
    return fresh_tx, unreadable


def notification(result: fb.BacktestResult, failed: list[str], spy: pd.Series,
                 pelosi_new: Optional[pd.DataFrame] = None, state: Optional[dict] = None,
                 pelosi_unreadable: Optional[pd.DataFrame] = None, complete: bool = True) -> str:
    """result : portefeuille réel (démarré vide, smart_money.LIVE_START)."""
    state = state or {}
    last = result.equity.index[-1]
    moves, first = new_moves(result, state)
    period = f"séance du {last:%d/%m/%Y}" if first == last else f"séances du {first:%d/%m/%Y} au {last:%d/%m/%Y}"
    if not complete:
        lines = [f"# ⚠️ Fonds — mise à jour INCOMPLÈTE ({period})", "",
                 "Des données manquent : les mouvements du jour ne sont PAS fiables et ne sont pas annoncés.",
                 "La page Desk de la veille est conservée. Le prochain passage rattrapera les mouvements manqués.",
                 "", f"Problèmes : {', '.join(failed)}", "", DISCLAIMER]
        return "\n".join(lines) + "\n"
    day = result.equity.pct_change().iloc[-1]
    spy_day = spy.reindex(result.equity.index).ffill().pct_change().iloc[-1]
    index_share = result.index_exposure.iloc[-1] if result.index_exposure is not None else 0.0
    start = pd.Timestamp(result.config.trading_start or result.equity.index[0])
    live = result.equity[result.equity.index >= start]
    spy_live = spy.reindex(live.index).ffill()
    since = live.iloc[-1] / live.iloc[0] - 1 if len(live) > 1 else 0.0
    spy_since = spy_live.iloc[-1] / spy_live.iloc[0] - 1 if len(live) > 1 else 0.0
    lines = [f"# Fonds — {period}", ""]
    if moves.empty:
        lines.append("Aucun mouvement de portefeuille." if state.get("derniere_seance") != f"{last:%Y-%m-%d}"
                     else "Aucune nouvelle séance (jour férié à New York ?) : rien de nouveau.")
    else:
        lines.append(f"{len(moves)} mouvement(s) de portefeuille :")
        for r in moves.itertuples():
            lines.append(f"- {pd.Timestamp(r.date):%d/%m} **{MOVES.get(r.action, r.action)} {r.asset}** "
                         f"à {r.price:,.2f} $ — {r.label}")
    lines += [
        "",
        f"- Dernière séance : fonds {day:+.2%}, S&P 500 {spy_day:+.2%}",
        f"- Lignes détenues : {int((result.scales.iloc[-1] > 0).sum())} ; actions {result.gross.iloc[-1]:.0%}, "
        f"S&P 500 {index_share:.0%}",
        f"- Depuis le départ du portefeuille réel ({start:%d/%m/%Y}) : fonds {since:+.2%}, S&P 500 {spy_since:+.2%}",
    ]
    if pelosi_new is not None and not pelosi_new.empty:
        lines += ["", f"Nouvelle(s) déclaration(s) de Nancy Pelosi ({len(pelosi_new)} transaction(s)) :"]
        for r in pelosi_new.itertuples():
            verb = {"BUY": "achat", "SELL": "vente", "SELL_PARTIAL": "vente partielle"}.get(r.type, r.type.lower())
            lines.append(f"- {verb} {r.ticker} ({'options' if r.kind == 'OP' else 'actions'}) le "
                         f"{pd.Timestamp(r.tx_date):%d/%m/%Y}, environ {r.amount:,.0f} $")
    if pelosi_unreadable is not None and not pelosi_unreadable.empty:
        lines += ["", "Déclaration(s) de Nancy Pelosi sans titre coté lu automatiquement (placement non coté, "
                      "ou document à vérifier à la main) :"]
        lines += [f"- {PTR_LINK.format(year=pd.Timestamp(r.filing_date).year, doc=r.doc_id)}"
                  for r in pelosi_unreadable.itertuples()]
    if failed:
        lines += ["", f"⚠️ À surveiller (sans effet sur le fonds) : {', '.join(failed)}"]
    lines += ["", DISCLAIMER]
    return "\n".join(lines) + "\n"


def write_outputs(text: str, complete: bool, reasons: list[str]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "notification.md").write_text(text, encoding="utf-8")
    (OUT / "etat_mise_a_jour.json").write_text(json.dumps(
        {"complet": complete, "problemes": reasons, "genere_le": pd.Timestamp.now(tz="UTC").isoformat()},
        ensure_ascii=False, indent=1), encoding="utf-8")
    print(text, flush=True)


# =============================================================================
# Programme
# =============================================================================

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Mise à jour quotidienne du fonds")
    parser.add_argument("--etat", type=Path, help="page Desk publiée la veille (fichier HTML) : état à reprendre")
    parser.add_argument("--skip-download", action="store_true", help="utiliser les données déjà présentes")
    parser.add_argument("--block-days", type=int, default=5, help="séances de gros blocs (radar : 5 suffisent)")
    args = parser.parse_args(argv)
    state = read_state(args.etat)
    critical = [] if args.skip_download else run_stages(args.block_days)
    try:
        engine = p1.DATA / "engine"
        data = fb.load_market_from_csv(engine)
        result = fb.run_backtest(data, sm.phase1_config(data, trading_start=sm.LIVE_START))
    except Exception as err:  # noqa: BLE001 — pas de fichiers moteur : échec franc, jamais silencieux
        traceback.print_exc()
        reasons = critical + [f"calcul du fonds impossible ({type(err).__name__}: {err})"]
        write_outputs(f"# ⚠️ Fonds — ÉCHEC de la mise à jour\n\nProblèmes : {', '.join(reasons)}\n\n"
                      f"La page Desk de la veille est conservée.\n\n{DISCLAIMER}\n", False, reasons)
        return 1
    last = result.equity.index[-1]
    age = stale_days(last)
    if age > MAX_STALE_DAYS:
        critical.append(f"cours périmés (dernière séance le {last:%d/%m/%Y}, il y a {age} jours)")
    complete = not critical

    warnings, pelosi_new, pelosi_unreadable, pelosi_docs = [], None, None, state.get("pelosi_docs", [])
    try:
        import pelosi
        out = pelosi.main()
        previous = result.equity.index[-2] if len(result.equity) > 1 else last
        pelosi_new, pelosi_unreadable = new_pelosi(out["tx"], out["index"], state, previous)
        # Déclarations lues (même sans titre coté) : vues. Non téléchargeables ou illisibles : retentées.
        idx = out["index"]
        pelosi_docs = sorted(set(idx.loc[idx["readable"].astype(bool), "doc_id"].astype(str))
                             | set(map(str, state.get("pelosi_docs", []))))
    except Exception as err:  # noqa: BLE001 — Pelosi ne doit pas empêcher la mise à jour du fonds
        traceback.print_exc()
        warnings.append(f"suivi Pelosi indisponible ({type(err).__name__}: {err})")

    spy = data.market if data.market is not None else result.benchmark
    text = notification(result, critical + warnings, spy, pelosi_new, state, pelosi_unreadable, complete)
    if complete:
        dashboard.main(["--data-dir", str(engine), "--out", str(PAGE)])
        embed_state(PAGE, {"derniere_seance": f"{last:%Y-%m-%d}", "pelosi_docs": pelosi_docs,
                           "genere_le": pd.Timestamp.now(tz="UTC").isoformat(), "depart": sm.LIVE_START})
    write_outputs(text, complete, critical + warnings)
    return 0 if complete else 2


if __name__ == "__main__":
    sys.exit(main())
