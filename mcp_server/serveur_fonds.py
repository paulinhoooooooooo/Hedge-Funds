#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
serveur_fonds.py — Serveur MCP open source du fonds (lecture seule)
===================================================================

Permet de poser des questions en français à un assistant IA (Claude Desktop, Claude Code…)
sur le travail du fonds : état du portefeuille, revue des lignes, où se positionnent les
grands acteurs (prix / volume), candidats à l'achat, journal des décisions.

Principes :
  * LECTURE SEULE : aucun ordre n'est passé, aucune donnée n'est modifiée ;
  * AUCUN ACCÈS À UN COMPTE TRADINGVIEW : les données viennent de l'entrepôt du fonds
    (CSV au format de load_market_from_csv, sources officielles : SEC, CFTC, FINRA, cours) ;
  * sans FLOWFUND_DATA_DIR, le serveur lit les données réelles de data/phase1/engine si elles ont été
    téléchargées ; à défaut, il tourne sur les données SYNTHÉTIQUES de démonstration, et chaque
    réponse le signale.

Lancement (transport stdio, utilisé par les assistants) :
    python mcp_server/serveur_fonds.py
    FLOWFUND_DATA_DIR=mes_donnees/ python mcp_server/serveur_fonds.py

Dépendance : paquet « mcp » (SDK officiel du Model Context Protocol), versions 1.x ou 2.x.
"""

from __future__ import annotations

import os
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backtest"))

import flow_backtest as fb  # noqa: E402
import smart_money as sm  # noqa: E402
import trade_cards as tc  # noqa: E402
from market_footprint import describe_footprint  # noqa: E402

INSTRUCTIONS = (
    "Serveur du fonds « Moyen/Long-Term Flow Trading ». Il suit l'argent des grands acteurs "
    "(déclarations 13F des gérants, flux des fonds, empreinte prix / volume des banques et institutions) "
    "et explique chaque décision. Réponds en français simple : l'utilisateur est débutant. "
    "Signale toujours si les données sont synthétiques. Ne présente jamais un résultat comme un "
    "conseil d'investissement personnalisé."
)


@dataclass
class FundState:
    data: fb.MarketData
    result: fb.BacktestResult
    review: pd.DataFrame
    label: str
    synthetic: bool
    buyers: Optional[pd.DataFrame] = None
    cot: Optional[pd.DataFrame] = None


def _optional_csv(path: Path, dates: tuple[str, ...]) -> Optional[pd.DataFrame]:
    if not path.exists():
        return None
    df = pd.read_csv(path)
    for col in dates:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col])
    return df


def load_state(data_dir: Optional[str] = None, seed: int = 7) -> FundState:
    """Charge les données (CSV réels ou démonstration synthétique) et exécute le moteur."""
    buyers = cot = None
    if data_dir:
        data = fb.load_market_from_csv(data_dir)
        label, synthetic = f"données réelles ({data_dir})", False
        # Jambe rapide gratuite = Chaikin Money Flow des ETF sectoriels (seuils du §9.2 de la synthèse)
        cfg = sm.phase1_config(data)
        buyers = _optional_csv(Path(data_dir) / "smart_money_buyers.csv", ("period_end", "available_date"))
        cot = _optional_csv(Path(__file__).resolve().parents[1] / "data" / "phase1" / "cot_dealers.csv",
                            ("report_date", "available_date"))
    else:
        data = fb.generate_synthetic_market(seed=seed)
        label, synthetic = "DONNÉES SYNTHÉTIQUES de démonstration — aucune valeur réelle", True
        cfg = fb.StrategyConfig()
    result = fb.run_backtest(data, cfg)
    return FundState(data=data, result=result, review=fb.review_positions(result), label=label,
                     synthetic=synthetic, buyers=buyers, cot=cot)


def _header(state: FundState) -> str:
    date = state.result.equity.index[-1]
    return f"_Source : {state.label} — situation au {date:%d/%m/%Y}._\n\n"


def _resolve(state: FundState, actif: str) -> str:
    """Retrouve un actif par son code interne ou son symbole TradingView (insensible à la casse)."""
    wanted = actif.strip().upper()
    assets = state.result.assets
    for name in assets.index:
        symbols = {name.upper()}
        if "tv_symbol" in assets.columns and isinstance(assets.at[name, "tv_symbol"], str):
            tv = assets.at[name, "tv_symbol"].upper()
            symbols |= {tv, tv.split(":")[-1]}
        if wanted in symbols:
            return name
    known = ", ".join(assets.index[:30])
    raise ValueError(f"Actif inconnu : « {actif} ». Actifs disponibles : {known}")


def etat_du_fonds(state: FundState) -> str:
    m, b = state.result.metrics, state.result.benchmark_metrics
    n_lines = int((state.result.scales.iloc[-1] > 0).sum())
    rec = "non récupéré" if m["recovery_time_days"] is None else f"{m['recovery_time_days']} jours"
    return _header(state) + "\n".join([
        "## État du fonds",
        f"- Lignes en portefeuille : {n_lines} ; part investie en actions : {state.result.gross.iloc[-1]:.0%}"
        + (f" ; trésorerie placée dans le S&P 500 : {state.result.index_exposure.iloc[-1]:.0%}"
           if state.result.index_exposure is not None and state.result.index_exposure.iloc[-1] > 0 else ""),
        f"- Rendement annuel moyen : {m['cagr']:+.1%} (univers équipondéré : {b['cagr']:+.1%})",
        f"- Sharpe (rendement par unité de risque) : {m['sharpe']:.2f} (univers : {b['sharpe']:.2f})",
        f"- Pire perte depuis un plus haut : {m['max_drawdown']:.1%} ; temps pour s'en remettre : {rec}",
        f"- Allers-retours clôturés : {m.get('n_trades', 0)} ; taux de réussite : {m.get('win_rate', float('nan')):.0%}",
    ])


def revue_des_positions(state: FundState) -> str:
    review = state.review
    if review.empty:
        return _header(state) + "Aucune ligne en portefeuille."
    lines = [_header(state) + "## Revue des lignes actives"]
    for _, r in review.sort_values("weight", ascending=False).iterrows():
        probs = ", ".join(f"{h} {r[f'p_up_{h}']:.0%}" for h in ("J+5", "M+3", "A+1") if np.isfinite(r[f"p_up_{h}"]))
        lines.append(f"### {r['asset']} — poids {r['weight']:.1%} — {r['exit_signal']}\n"
                     f"Probabilité de hausse : {probs or 'n.d.'}\n\n{r['justification']}")
    return "\n\n".join(lines)


def empreinte_grands_acteurs(state: FundState, actif: str) -> str:
    name = _resolve(state, actif)
    sig = state.result.signals
    if sig.footprint is None:
        return _header(state) + f"Pas de volume disponible pour {name} : empreinte impossible à calculer."
    j = list(sig.prices.columns).index(name)
    i = len(sig.calendar) - 1
    status = ("vendeuse (distribution)" if sig.dist_foot.iat[i, j]
              else "acheteuse (accumulation)" if sig.footprint.accumulation.iat[i, j] else "neutre")
    return (_header(state) + f"## Où sont les grands acteurs sur {name} ?\n"
            f"Empreinte actuelle : **{status}**.\n\n"
            + describe_footprint(sig.footprint, sig.prices.iat[i, j], i, j))


def analyser_actif(state: FundState, actif: str) -> str:
    name = _resolve(state, actif)
    res, sig = state.result, state.result.signals
    j = list(sig.prices.columns).index(name)
    i = len(sig.calendar) - 1
    spec = fb.ASSET_CLASS_SPECS[res.assets.at[name, "asset_class"]]
    io, pe, fr = sig.io_change.iat[i, j], sig.io_period_end.iat[i, j], sig.flow_ratio.iat[i, j]
    legs = [lbl for lbl, flag in (("positionnement institutionnel", sig.dist_slow.iat[i, j]),
                                  ("flux de fonds", sig.dist_fast.iat[i, j]),
                                  ("empreinte prix / volume", sig.dist_foot.iat[i, j])) if flag]
    held = res.scales.iat[i, j] > 0
    parts = [
        _header(state) + f"## Analyse de {name}",
        f"- En portefeuille : {'oui, poids ' + format(res.weights.iat[i, j], '.1%') if held else 'non'}",
        f"- Positionnement des gérants de référence ({spec.name}) : "
        + ("n.d." if not np.isfinite(io) else f"{io:+.1%} sur ~1 trimestre (rapport arrêté au {pd.Timestamp(pe):%d/%m/%Y})"),
        "- Flux nets sur 30 jours : " + ("n.d." if not np.isfinite(fr) else f"{fr:+.2%} de l'encours"),
        f"- Signal d'achat actif : {'oui' if sig.entry.iat[i, j] else 'non'}",
        f"- Jambes en distribution : {', '.join(legs) if legs else 'aucune'}",
    ]
    if sig.footprint is not None:
        parts.append("\n" + describe_footprint(sig.footprint, sig.prices.iat[i, j], i, j))
    events = res.journal[res.journal["asset"] == name].tail(5)
    if len(events):
        parts.append("\n**Dernières décisions du fonds :**")
        parts += [f"- {e.date:%d/%m/%Y} — {e.label}" for e in events.itertuples()]
    return "\n".join(parts)


def candidats_a_l_achat(state: FundState) -> str:
    sig, res = state.result.signals, state.result
    i = len(sig.calendar) - 1
    names = [a for a in sig.prices.columns if sig.entry[a].iloc[i] and res.scales[a].iloc[i] == 0]
    if not names:
        return _header(state) + "Aucun candidat : aucun actif ne réunit aujourd'hui accumulation des gérants et flux entrants."
    names.sort(key=lambda a: -sig.score[a].iloc[i])
    rows = [f"- {a} : gérants {sig.io_change[a].iloc[i]:+.1%}, flux 30 j {sig.flow_ratio[a].iloc[i]:+.2%}"
            + (" ; empreinte acheteuse" if sig.footprint is not None and sig.footprint.accumulation[a].iloc[i] else "")
            for a in names]
    return _header(state) + "## Candidats à l'achat (convergence des signaux)\n" + "\n".join(rows)


def journal_des_decisions(state: FundState, actif: Optional[str] = None, nombre: int = 15) -> str:
    j = state.result.journal
    if actif:
        j = j[j["asset"] == _resolve(state, actif)]
    j = j.tail(max(1, min(int(nombre), 100)))
    if j.empty:
        return _header(state) + "Aucune décision enregistrée."
    return _header(state) + "## Journal des décisions\n" + "\n\n".join(
        f"**{e.date:%d/%m/%Y} — {e.asset} — {e.label}**\n{e.justification}" for e in j.itertuples())


def derniers_trades(state: FundState, nombre: int = 5) -> str:
    """Fiches des dernières décisions : quoi, pourquoi, ce que le signal a donné par le passé, aujourd'hui."""
    cards = tc.build_trade_cards(state.result, n=max(1, min(int(nombre), 20)), buyers=state.buyers,
                                 cot=state.cot, synthetic=state.synthetic)
    if not cards:
        return _header(state) + "Aucune décision enregistrée."
    return _header(state) + "## Dernières décisions du fonds\n\n" + "\n\n".join(c.to_text() for c in cards)


def radar_grands_acteurs(state: FundState, actif: Optional[str] = None) -> str:
    """Alertes du radar (heure, jour, semaine, mois) : où les grands acteurs laissent des traces."""
    radar = state.result.signals.radar
    if radar is None:
        return _header(state) + "Radar indisponible : pas de volume dans les données."
    if actif:
        name = _resolve(state, actif)
        i = len(radar.score) - 1
        score = radar.score[name].iloc[i]
        return (_header(state) + f"## Radar sur {name}\nScore {score:+.1f} (alerte à partir de ±2,5).\n\n"
                + radar.explain(name, i))
    rows = []
    for i in range(len(radar.score) - 1, max(-1, len(radar.score) - 11), -1):
        rows += [f"- {a.date:%d/%m/%Y} — {a.asset} — {a.sens} (score {a.score:+.1f}) : {a.explication}"
                 for a in radar.alerts(date=radar.score.index[i]).itertuples()]
    body = "\n".join(rows[:15]) if rows else "Aucune trace anormale sur les 10 dernières séances."
    return _header(state) + "## Radar des grands acteurs (10 dernières séances)\n" + body


def lister_actifs(state: FundState) -> str:
    a = state.result.assets
    rows = [f"- {name} ({row.asset_class}, véhicule de flux {row.flow_vehicle})" for name, row in a.iterrows()]
    return _header(state) + "## Actifs suivis\n" + "\n".join(rows)


TOOLS: dict[str, tuple[Callable, str]] = {
    "etat_du_fonds": (etat_du_fonds, "Performance et risque du fonds, nombre de lignes, part investie."),
    "revue_des_positions": (revue_des_positions,
                            "Revue de toutes les lignes en portefeuille : probabilités à 5 jours, 3 mois et 1 an, "
                            "signal de sortie et explication."),
    "analyser_actif": (analyser_actif,
                       "Analyse complète d'un actif : gérants, flux, empreinte des grands acteurs, décisions récentes."),
    "empreinte_grands_acteurs": (empreinte_grands_acteurs,
                                 "Où sont positionnés les grands acteurs (banques, institutions) sur un actif, "
                                 "d'après le prix et le volume : zone de valeur, prix moyen, jours de distribution."),
    "candidats_a_l_achat": (candidats_a_l_achat, "Actifs qui réunissent aujourd'hui les conditions d'achat du fonds."),
    "journal_des_decisions": (journal_des_decisions,
                              "Dernières décisions du fonds et leur justification, pour un actif ou pour tout le portefeuille."),
    "lister_actifs": (lister_actifs, "Liste des actifs suivis par le fonds."),
    "derniers_trades": (derniers_trades,
                        "Fiches des dernières décisions du fonds : pourquoi il a acheté ou vendu, ce que ce signal "
                        "a donné par le passé, et où en est la position aujourd'hui."),
    "radar_grands_acteurs": (radar_grands_acteurs,
                             "Radar des grands acteurs : volumes anormaux et achats ou ventes massifs repérés sur "
                             "l'heure, le jour, la semaine et le mois, pour tout le marché ou pour un actif."),
}


def build_server(state_loader: Callable[[], FundState], prewarm: bool = False):
    """Construit le serveur MCP. L'état est calculé une seule fois : au premier appel d'outil, ou dès
    le lancement en tâche de fond (prewarm) pour que la première question n'attende pas une minute."""
    try:
        from mcp.server.mcpserver import MCPServer as Server  # SDK 2.x
    except ImportError:
        from mcp.server.fastmcp import FastMCP as Server  # SDK 1.x

    server = Server(name="fonds-flux", instructions=INSTRUCTIONS)
    cache: dict[str, FundState] = {}
    lock = threading.Lock()

    def state() -> FundState:
        with lock:  # un seul calcul, même si une question arrive pendant le préchargement
            if "state" not in cache:
                cache["state"] = state_loader()
            return cache["state"]

    def register(name: str, fn: Callable, description: str) -> None:
        if name in ("analyser_actif", "empreinte_grands_acteurs"):
            def tool(actif: str) -> str:
                return fn(state(), actif)
        elif name == "derniers_trades":
            def tool(nombre: int = 5) -> str:
                return fn(state(), nombre)
        elif name == "radar_grands_acteurs":
            def tool(actif: Optional[str] = None) -> str:
                return fn(state(), actif)
        elif name == "journal_des_decisions":
            def tool(actif: Optional[str] = None, nombre: int = 15) -> str:
                return fn(state(), actif, nombre)
        else:
            def tool() -> str:
                return fn(state())
        server.tool(name=name, description=description)(tool)

    for tool_name, (fn, description) in TOOLS.items():
        register(tool_name, fn, description)
    if prewarm:
        threading.Thread(target=state, name="prechargement-fonds", daemon=True).start()
    return server


def default_data_dir() -> Optional[str]:
    """FLOWFUND_DATA_DIR, sinon les données réelles de la phase 1 si elles ont été téléchargées
    (data/phase1/engine), sinon None : démonstration synthétique, signalée dans chaque réponse."""
    explicit = os.environ.get("FLOWFUND_DATA_DIR")
    if explicit:
        return explicit
    engine = Path(__file__).resolve().parents[1] / "data" / "phase1" / "engine"
    return str(engine) if (engine / "assets.csv").exists() else None


def main() -> None:
    data_dir = default_data_dir()
    build_server(lambda: load_state(data_dir), prewarm=True).run()


if __name__ == "__main__":
    main()
