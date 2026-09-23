# -*- coding: utf-8 -*-
"""
trade_cards.py — Fiches de trade : chaque décision du fonds expliquée simplement
===============================================================================

Pour chaque achat, renforcement, allègement ou vente, une fiche répond à quatre questions :
  1. QUOI    : l'action, la date, la décision ;
  2. POURQUOI: les gérants de référence (et leurs noms quand les données réelles le permettent),
               l'argent des fonds, l'empreinte des grands acteurs, le radar (heure, jour, semaine,
               mois) et la position des banques ;
  3. HISTORIQUE : ce que ce même signal a donné par le passé, en n'utilisant que les issues
               CONNUES à la date de la décision (aucun regard vers le futur) ;
  4. AUJOURD'HUI : la position est-elle encore détenue, gagnante ou perdante ?
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

from market_footprint import describe_footprint

ACTION_TITLES = {
    "ENTRY": "ACHAT",
    "ADD_REACCUMULATION": "RENFORCEMENT",
    "REDUCE_DISTRIBUTION_ALERT": "ALLÈGEMENT",
    "EXIT_DISTRIBUTION_CONFIRMED": "VENTE",
    "EXIT_INSTITUTIONAL_LIQUIDATION": "VENTE",
    "RISK_STOP": "VENTE (sécurité)",
    "RISK_DRAWDOWN": "ALLÈGEMENT (sécurité)",
    "RISK_TRIM": "ALLÈGEMENT (concentration)",
    "RISK_VOL": "ALLÈGEMENT (volatilité)",
    "RISK_REGIME": "ALLÈGEMENT (marché sous tension)",
}
BUY_ACTIONS = {"ENTRY", "ADD_REACCUMULATION"}
SELL_SIGNAL_ACTIONS = {"REDUCE_DISTRIBUTION_ALERT", "EXIT_DISTRIBUTION_CONFIRMED", "EXIT_INSTITUTIONAL_LIQUIDATION"}
HORIZONS = {63: "3 mois", 126: "6 mois"}
HOLDING_WORDS = {  # comment se mesure la détention des gérants, selon la classe d'actifs
    "EQUITY": "en nombre d'actions (déclarations 13F",
    "FX": "sur les contrats à terme (rapport COT de la CFTC",
    "COMMODITY": "sur les contrats à terme (rapport COT de la CFTC",
    "CRYPTO": "en avoirs de long terme (données on-chain",
}


@dataclass
class TradeCard:
    date: pd.Timestamp
    asset: str
    action: str
    title: str
    reasons: list[str]
    history: str
    status: str
    synthetic: bool
    tv_symbol: str = ""
    extra: dict = field(default_factory=dict)

    def to_text(self) -> str:
        lines = [f"**{self.title} {self.asset} — {self.date:%d/%m/%Y}**"]
        lines += [f"- {r}" for r in self.reasons]
        lines.append(f"**Historique :** {self.history}")
        lines.append(f"**Aujourd'hui :** {self.status}")
        return "\n".join(lines)


def _episodes(flag: pd.DataFrame, cooldown: int = 20) -> pd.DataFrame:
    """Premier jour de chaque épisode (signal vrai après `cooldown` séances sans signal)."""
    recent = flag.shift(1).rolling(cooldown, min_periods=1).max().fillna(0).astype(bool)
    return flag & ~recent


class HistoryBook:
    """Rendements passés après chaque épisode de signal, pour un calcul « à la date »."""

    def __init__(self, result, cooldown: int = 20):
        sig = result.signals
        px = sig.prices
        classes = result.assets["asset_class"]
        legs = sig.dist_slow.astype(int) + sig.dist_fast.astype(int) + sig.dist_foot.astype(int)
        flags = {"buy": sig.entry, "sell": (legs >= 2) | sig.dist_hard, "alert": legs >= 1}
        rows = []
        for kind, flag in flags.items():
            starts = _episodes(flag.fillna(False).astype(bool), cooldown)
            for h in HORIZONS:
                fwd = px.shift(-h) / px - 1.0
                stacked = fwd[starts].stack().dropna()
                end_dates = sig.calendar[np.minimum(sig.calendar.get_indexer(stacked.index.get_level_values(0)) + h,
                                                    len(sig.calendar) - 1)]
                rows.append(pd.DataFrame({
                    "kind": kind, "horizon": h, "date": stacked.index.get_level_values(0),
                    "asset": stacked.index.get_level_values(1), "ret": stacked.to_numpy(), "known_at": end_dates,
                }))
        book = pd.concat(rows, ignore_index=True)
        book["asset_class"] = book["asset"].map(classes)
        self.book = book

    def summary(self, kind: str, asset_class: str, as_of: pd.Timestamp) -> dict:
        b = self.book
        b = b[(b["kind"] == kind) & (b["asset_class"] == asset_class) & (b["known_at"] <= as_of)]
        out = {}
        for h in HORIZONS:
            vals = b.loc[b["horizon"] == h, "ret"]
            out[h] = {"n": int(len(vals)), "up": float((vals > 0).mean()) if len(vals) else np.nan,
                      "mean": float(vals.mean()) if len(vals) else np.nan}
        return out


def _history_text(summary: dict, kind: str, since: pd.Timestamp) -> str:
    n = max(v["n"] for v in summary.values())
    if n < 5:
        return "Pas encore assez de cas passés comparables pour en tirer une statistique."
    parts = []
    for h, label in HORIZONS.items():
        s = summary[h]
        if s["n"]:
            ups = round(10 * s["up"])
            if kind == "buy":
                parts.append(f"{label} après : hausse {ups} fois sur 10, {s['mean']:+.1%} en moyenne")
            else:
                parts.append(f"{label} après : baisse {10 - ups} fois sur 10, {s['mean']:+.1%} en moyenne")
    what = "ce signal d'achat" if kind == "buy" else "ce signal de vente"
    return f"Depuis {since:%Y}, {what} est apparu {n} fois sur des actifs de même type. " + " ; ".join(parts) + "."


def _status(result, asset: str, date: pd.Timestamp, action: str) -> str:
    trades = result.trades[result.trades["asset"] == asset]
    if action in BUY_ACTIONS:
        match = trades[trades["entry_signal_date"] <= date].tail(1)
        if match.empty:
            return "Ordre en cours d'exécution."
        t = match.iloc[0]
        if t["exit_reason"] == "EN_COURS":
            return f"Toujours en portefeuille : {t['return']:+.1%} depuis l'achat (après frais)."
        return f"Vendu le {t['exit_date']:%d/%m/%Y} : {t['return']:+.1%} sur la position, en {int(t['holding_days'])} jours."
    closed = trades[trades["exit_date"] == date]
    if len(closed):
        t = closed.iloc[0]
        return f"Position soldée : {t['return']:+.1%} au total, détenue {int(t['holding_days'])} jours."
    later = trades[(trades["first_fill"] <= date) & (trades["exit_reason"] == "EN_COURS")]
    if len(later):
        return f"Ligne réduite, le reste est toujours détenu ({later.iloc[0]['return']:+.1%} depuis l'achat)."
    return "Ligne réduite puis vendue ensuite."


def _banks_text(cot: Optional[pd.DataFrame], date: pd.Timestamp) -> Optional[str]:
    if cot is None or cot.empty:
        return None
    known = cot[cot["available_date"] <= date]
    if known.empty:
        return None
    parts = []
    for market, grp in known.groupby("market"):
        last = grp.sort_values("report_date").iloc[-1]
        habit = grp["dealer_net_pct_oi"].median()
        tone = "plus vendeuses" if last["dealer_net_pct_oi"] < habit else "moins vendeuses"
        parts.append(f"{market} {tone} que d'habitude ({last['dealer_net_pct_oi']:+.0%} contre {habit:+.0%})")
    return "Banques sur les contrats à terme : " + " ; ".join(parts) + "."


def _buyers_text(buyers: Optional[pd.DataFrame], asset: str, date: pd.Timestamp) -> Optional[str]:
    if buyers is None or buyers.empty:
        return None
    known = buyers[(buyers["asset"] == asset) & (buyers["available_date"] <= date)]
    if known.empty:
        return None
    last = known.sort_values("period_end").iloc[-1]
    text = (f"Sur le trimestre arrêté au {pd.Timestamp(last['period_end']):%d/%m/%Y} : {int(last['n_buyers'])} des "
            f"{int(last['n_managers'])} meilleurs gérants suivis ont acheté, {int(last['n_sellers'])} ont vendu.")
    names = str(last.get("top_buyers", "") or "").strip()
    if names:
        text += f" Principaux acheteurs : {names}."
    return text


def _trigger(sig, i: int, j: int, action: str, justification: str) -> str:
    """Première ligne de la fiche : ce qui a déclenché la décision."""
    if action in BUY_ACTIONS:
        return ("Déclencheur : les gérants de référence accumulent, l'argent entre, et aucune vente massive "
                "des grands acteurs n'est visible.")
    if action.startswith("RISK"):
        return f"Déclencheur : {justification}"
    legs = [label for label, frame in (("les gérants réduisent", sig.dist_slow), ("l'argent sort", sig.dist_fast),
                                       ("les grands acteurs vendent (prix / volume)", sig.dist_foot))
            if bool(frame.iat[i, j])]
    if sig.dist_hard.iat[i, j]:
        legs.insert(0, "les gérants liquident")
    joined = " + ".join(legs) if legs else "signal de distribution"
    return f"Déclencheur : {joined}."


def build_trade_cards(result, n: int = 12, buyers: Optional[pd.DataFrame] = None,
                      cot: Optional[pd.DataFrame] = None, synthetic: bool = True,
                      history: Optional[HistoryBook] = None) -> list[TradeCard]:
    """Fiches des `n` dernières décisions du journal, de la plus récente à la plus ancienne."""
    sig = result.signals
    names = list(sig.prices.columns)
    history = history or HistoryBook(result)
    since = sig.calendar[0]
    cards = []
    for e in result.journal.tail(n).iloc[::-1].itertuples():
        date, asset, action = pd.Timestamp(e.date), e.asset, e.action
        i, j = int(sig.calendar.get_loc(date)), names.index(asset)
        cls = result.assets.at[asset, "asset_class"]
        vehicle = result.assets.at[asset, "flow_vehicle"]
        reasons = [_trigger(sig, i, j, action, e.justification)]
        io, pe = sig.io_change.iat[i, j], sig.io_period_end.iat[i, j]
        if np.isfinite(io):
            verb = "augmenté" if io > 0 else "réduit"
            reasons.append(f"Les gérants de référence ont {verb} leurs positions de {abs(io):.1%} "
                           f"{HOLDING_WORDS.get(cls, '(')} arrêtées au {pd.Timestamp(pe):%d/%m/%Y}, déjà publiées).")
        bt = _buyers_text(buyers, asset, date)
        if bt:
            reasons.append(bt)
        fr = sig.flow_ratio.iat[i, j]
        if np.isfinite(fr):
            direction = "entre dans" if fr > 0 else "sort de"
            reasons.append(f"L'argent {direction} {vehicle} : {fr:+.2%} sur 30 jours.")
        if sig.footprint is not None:
            reasons.append(describe_footprint(sig.footprint, sig.prices.iat[i, j], i, j))
        if sig.radar is not None:
            radar = sig.radar.explain(asset, i)
            if radar != "Aucune trace anormale.":
                reasons.append(f"Radar des grands acteurs (score {sig.radar.score.iat[i, j]:+.1f}) : {radar}")
        banks = _banks_text(cot, date) if cls == "EQUITY" else None
        if banks:
            reasons.append(banks)
        if action.startswith("RISK"):
            hist = "Décision de sécurité du Risk Manager : pas de statistique de signal."
        else:
            kind = "buy" if action in BUY_ACTIONS else "sell"
            hist = _history_text(history.summary(kind, cls, date), kind, since)
        cards.append(TradeCard(
            date=date, asset=asset, action=action, title=ACTION_TITLES.get(action, action), reasons=reasons,
            history=hist, status=_status(result, asset, date, action), synthetic=synthetic,
            tv_symbol=str(result.assets.at[asset, "tv_symbol"]) if "tv_symbol" in result.assets.columns else asset,
        ))
    return cards
