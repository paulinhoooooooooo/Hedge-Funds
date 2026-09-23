# -*- coding: utf-8 -*-
"""
tradingview_bridge.py — Passerelle entre le moteur de flux et TradingView
=========================================================================

TradingView est la COUCHE VISUELLE ET D'ALERTE de l'équipe, pas la source de vérité :
les données 13F / EPFR / COT / on-chain restent dans l'entrepôt point-in-time du fonds.
Ce module produit des fichiers que l'équipe importe dans TradingView par les voies
officielles (import de liste, éditeur Pine), sans automatisation de la plateforme :

  * watchlist_fonds.txt   liste importable (sections : lignes actives, alertes, candidats) ;
  * pine/<actif>.pine     indicateur Pine Script v6 superposant au graphique les décisions
                          du fonds (achats, allègements, sorties, stops) et la dernière
                          Matrice prédictive, pour chaque ligne active.

Le symbole TradingView de chaque actif est lu dans la colonne optionnelle `tv_symbol`
de assets.csv (ex. NASDAQ:AAPL, FX:EURUSD, COMEX:GC1!, BINANCE:BTCUSDT).
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

# Code, libellé court et couleur Pine de chaque décision du journal
PINE_ACTIONS = {
    "ENTRY": (1, "ACHAT", "color.new(color.teal, 0)"),
    "ADD_REACCUMULATION": (2, "RENFORT", "color.new(color.teal, 30)"),
    "REDUCE_DISTRIBUTION_ALERT": (3, "ALLÈGEMENT", "color.new(color.orange, 0)"),
    "EXIT_DISTRIBUTION_CONFIRMED": (4, "SORTIE", "color.new(color.red, 0)"),
    "EXIT_INSTITUTIONAL_LIQUIDATION": (5, "SORTIE", "color.new(color.red, 0)"),
    "RISK_STOP": (6, "STOP", "color.new(color.maroon, 0)"),
    "RISK_DRAWDOWN": (7, "DÉ-RISQUE", "color.new(color.purple, 0)"),
    "RISK_TRIM": (8, "ÉCRÊTAGE", "color.new(color.gray, 0)"),
}
MAX_PINE_EVENTS = 150  # garde le script sous les limites de compilation Pine
MAX_TOOLTIP_CHARS = 280


def tv_symbol(assets: pd.DataFrame, asset: str) -> str:
    """Symbole TradingView de l'actif (colonne tv_symbol), à défaut le code interne."""
    if "tv_symbol" in assets.columns:
        value = assets.at[asset, "tv_symbol"]
        if isinstance(value, str) and value.strip():
            return value.strip()
    return asset


def _pine_str(text: str, limit: int = MAX_TOOLTIP_CHARS) -> str:
    """Littéral de chaîne Pine (guillemets et antislashs échappés, sur une ligne)."""
    text = re.sub(r"\s+", " ", str(text)).strip()
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _fmt_prob(x: float) -> str:
    return "n.d." if x is None or not np.isfinite(x) else f"{x:.0%}"


def build_watchlist(result, review: pd.DataFrame) -> str:
    """Liste au format d'import TradingView : « ###Section,SYM1,SYM2,... »."""
    assets = result.assets
    sig = result.signals
    last = -1
    active = [a for a in assets.index if result.scales[a].iloc[last] > 0]
    alerts = list(review.loc[~review["exit_signal"].str.startswith("CONSERVER"), "asset"]) if len(review) else []
    candidates = [a for a in assets.index if bool(sig.entry[a].iloc[last]) and a not in active]
    sections = [
        ("LIGNES ACTIVES", active),
        ("ALERTES DISTRIBUTION", alerts),
        ("CANDIDATS ACCUMULATION", candidates),
    ]
    parts = []
    for title, members in sections:
        if members:
            parts.append("###" + title)
            parts.extend(tv_symbol(assets, a) for a in members)
    return ",".join(parts) + "\n"


def build_pine_overlay(result, review: pd.DataFrame, asset: str, synthetic: bool = False) -> str:
    """Indicateur Pine v6 : décisions du journal + dernière revue de la ligne, sur son graphique."""
    journal = result.journal[result.journal["asset"] == asset].tail(MAX_PINE_EVENTS)
    dates = [int(pd.Timestamp(d).strftime("%Y%m%d")) for d in journal["date"]]
    codes = [PINE_ACTIONS[a][0] for a in journal["action"]]
    scales = [f"{s:.4f}" for s in journal["target_scale"]]
    tooltips = [_pine_str(f"{pd.Timestamp(d):%d/%m/%Y} — {lbl}. {txt}")
                for d, lbl, txt in zip(journal["date"], journal["label"], journal["justification"])]

    row = review[review["asset"] == asset]
    row = row.iloc[0] if len(row) else None
    symbol = tv_symbol(result.assets, asset)
    review_date = f"{pd.Timestamp(row['review_date']):%d/%m/%Y}" if row is not None else "n.d."

    def arr(kind: str, values: list) -> str:
        if not values:
            return f"array.new<{kind}>()"
        return "array.from(" + ", ".join(str(v) for v in values) + ")"

    label_cases = "\n".join(
        f"        {code} => \"{short}\"" for code, short, _ in sorted(PINE_ACTIONS.values())
    )
    color_cases = "\n".join(
        f"        {code} => {col}" for code, _, col in sorted(PINE_ACTIONS.values())
    )
    table_rows = []
    if row is not None:
        cells = [
            ("Poids actuel", f"{row['weight']:.1%}"),
            ("P(hausse) J+5", _fmt_prob(row.get("p_up_J+5"))),
            ("P(hausse) M+3", _fmt_prob(row.get("p_up_M+3"))),
            ("P(hausse) A+1", _fmt_prob(row.get("p_up_A+1"))),
            ("Exit Signal", row["exit_signal"]),
        ]
        for r, (k, v) in enumerate(cells, start=1):
            table_rows.append(f"    table.cell(tb, 0, {r}, {_pine_str(k)}, text_color = color.white, text_size = size.small)")
            table_rows.append(f"    table.cell(tb, 1, {r}, {_pine_str(v)}, text_color = color.white, text_size = size.small)")
    warning = (
        "// ATTENTION : journal issu de DONNÉES SYNTHÉTIQUES — démonstration du format uniquement,\n"
        "// ne pas superposer à un graphique réel pour en tirer une conclusion.\n"
        if synthetic else ""
    )

    return f"""//@version=6
// Généré par backtest/tradingview_bridge.py le {date.today():%d/%m/%Y} — ne pas modifier à la main.
// Ligne : {asset} | Symbole TradingView attendu : {symbol} | Unité de temps conseillée : 1D ou 1W
{warning}// Import : Pine Editor > nouveau script > coller > « Ajouter au graphique ».
indicator("Flow Fund — Journal {asset}", shorttitle = "FF {asset}", overlay = true, max_labels_count = 500)

showLabels = input.bool(true, "Afficher les décisions du fonds")
showBg = input.bool(true, "Colorer la période de détention")
showTable = input.bool(true, "Afficher la dernière revue de position")

// Journal d'arbitrages : date (AAAAMMJJ), code décision, échelle cible après décision, justification
var array<int> evDate = {arr("int", dates)}
var array<int> evCode = {arr("int", codes)}
var array<float> evScale = {arr("float", scales)}
var array<string> evTip = {arr("string", tooltips)}

labelText(int code) =>
    switch code
{label_cases}
        => "?"

labelColor(int code) =>
    switch code
{color_cases}
        => color.gray

int barDate = year * 10000 + month * 100 + dayofmonth
var int k = 0
var float posScale = 0.0
while k < array.size(evDate) and array.get(evDate, k) <= barDate
    int code = array.get(evCode, k)
    posScale := array.get(evScale, k)
    if showLabels and bar_index > 0
        bool isBuy = code <= 2
        label.new(bar_index, isBuy ? low : high, labelText(code),
             style = isBuy ? label.style_label_up : label.style_label_down,
             color = labelColor(code), textcolor = color.white, size = size.small,
             tooltip = array.get(evTip, k))
    k += 1

bgcolor(showBg and posScale >= 1.0 ? color.new(color.teal, 88) : showBg and posScale > 0.0 ? color.new(color.orange, 88) : na, title = "Détention")

var table tb = table.new(position.top_right, 2, 6, bgcolor = color.new(color.black, 25), border_width = 1)
if showTable and barstate.islast
    table.cell(tb, 0, 0, {_pine_str("Revue du " + review_date)}, text_color = color.white, text_size = size.small)
    table.cell(tb, 1, 0, {_pine_str(asset)}, text_color = color.white, text_size = size.small)
{chr(10).join(table_rows) if table_rows else '    table.cell(tb, 0, 1, "Aucune revue disponible", text_color = color.white)'}
"""


def export_tradingview(result, review: pd.DataFrame, out_dir: str | Path, synthetic: bool = False) -> Path:
    """Écrit la watchlist et un indicateur Pine par ligne active dans out_dir."""
    out = Path(out_dir)
    (out / "pine").mkdir(parents=True, exist_ok=True)
    (out / "watchlist_fonds.txt").write_text(build_watchlist(result, review), encoding="utf-8")
    for asset in review["asset"] if len(review) else []:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", asset)
        (out / "pine" / f"{safe}.pine").write_text(
            build_pine_overlay(result, review, asset, synthetic), encoding="utf-8")
    return out
