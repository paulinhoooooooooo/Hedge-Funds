# -*- coding: utf-8 -*-
"""
suivi_signaux.py — Portefeuille réel : il ne fait que suivre les signaux publiés par le fonds
===========================================================================================

Règles fixées par le fondateur (15/10/2026) :
  * départ à zéro le jour `start` (smart_money.LIVE_START) : 100 % en espèces, aucune action ;
  * ACHAT ou RENFORCEMENT publié par le fonds -> on achète, en complétant la ligne jusqu'à un douzième
    du portefeuille (12 lignes de même poids), dans la limite des espèces disponibles ;
  * ALLÈGEMENT (et les réductions du gestionnaire de risque : dé-risquage, écrêtage) -> on ne fait rien ;
  * VENTE (distribution confirmée, liquidation des institutions, stop de perte) -> on vend toute la ligne ;
  * l'argent non investi reste en espèces (aucun rendement).

Les ordres sont passés au cours de clôture du jour du signal (le cours affiché sur la fiche), frais
compris. Le résultat a la même forme que celle qu'utilisent la page Desk et la notification (equity,
journal, trades, scales, weights, gross, signals.prices, config.trading_start).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Optional

import numpy as np
import pandas as pd

import flow_backtest as fb

BUY = ("ENTRY", "ADD_REACCUMULATION")
SELL = ("EXIT_DISTRIBUTION_CONFIRMED", "EXIT_INSTITUTIONAL_LIQUIDATION", "RISK_STOP")
LINES = 12
FEE = 0.001  # frais par achat ou vente (0,10 %)
MIN_ORDER = 0.10  # un achat inférieur à 10 % d'une ligne pleine n'est pas passé


@dataclass
class SuiviReel:
    config: SimpleNamespace
    equity: pd.Series
    journal: pd.DataFrame
    trades: pd.DataFrame
    scales: pd.DataFrame
    weights: pd.DataFrame
    gross: pd.Series
    signals: SimpleNamespace
    index_exposure: Optional[pd.Series] = None
    cash: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))


def follow(backtest: fb.BacktestResult, start: str, capital: float = 100_000.0, lines: int = LINES,
           fee: float = FEE) -> SuiviReel:
    """Applique les règles ci-dessus aux décisions du fonds (journal du backtest) à partir de `start`."""
    prices = backtest.signals.prices.ffill()
    cal = prices.index
    t0 = pd.Timestamp(start)
    events = backtest.journal[pd.to_datetime(backtest.journal["date"]) >= t0]
    events = events[events["action"].isin(BUY + SELL)]
    by_day = {d: g for d, g in events.groupby(pd.to_datetime(events["date"]))}

    cash = capital
    units: dict[str, float] = {}
    trades: dict[str, dict] = {}
    closed: list[dict] = []
    moves: list[dict] = []
    nav_hist, cash_hist = np.full(len(cal), capital), np.full(len(cal), capital)
    w_hist = np.zeros((len(cal), prices.shape[1]))
    col = {a: k for k, a in enumerate(prices.columns)}

    for i, t in enumerate(cal):
        p = prices.iloc[i]
        day = by_day.get(t)
        if day is not None:
            # Ventes d'abord : elles libèrent des espèces pour les achats du même jour
            ordered = pd.concat([day[day["action"].isin(SELL)], day[day["action"].isin(BUY)]])
            for ev in ordered.itertuples():
                a, price = ev.asset, float(p.get(ev.asset, np.nan))
                if not np.isfinite(price) or price <= 0:
                    continue
                if ev.action in SELL:
                    if units.get(a, 0.0) <= 0:
                        continue
                    proceeds = units[a] * price
                    cash += proceeds * (1 - fee)
                    tr = trades.pop(a)
                    tr.update(exit_date=t, exit_reason=ev.action, avg_exit_price=price,
                              pnl=proceeds * (1 - fee) - tr["buy_notional"] - tr["fees"])
                    tr["return"] = price / tr["avg_entry_price"] - 1.0
                    closed.append(tr)
                    moves.append({"date": t, "asset": a, "action": ev.action, "price": price,
                                  "amount": -proceeds, "label": fb.ACTION_LABELS[ev.action]})
                    units[a] = 0.0
                    continue
                nav = cash + sum(u * float(p.get(x, 0.0)) for x, u in units.items() if u > 0)
                target = nav / lines
                held = units.get(a, 0.0) * price
                amount = min(target - held, cash / (1 + fee))
                if amount < MIN_ORDER * target:
                    continue  # ligne déjà pleine, ou plus assez d'espèces
                qty = amount / price
                cash -= amount * (1 + fee)
                action = "ADD_REACCUMULATION" if held > 0 else "ENTRY"
                tr = trades.get(a)
                if tr is None:
                    tr = trades[a] = {"asset": a, "asset_class": "", "entry_signal_date": t, "first_fill": t,
                                      "buy_units": 0.0, "buy_notional": 0.0, "fees": 0.0}
                tr["buy_units"] += qty
                tr["buy_notional"] += amount
                tr["fees"] += amount * fee
                tr["avg_entry_price"] = tr["buy_notional"] / tr["buy_units"]
                units[a] = units.get(a, 0.0) + qty
                moves.append({"date": t, "asset": a, "action": action, "price": price, "amount": amount,
                              "label": fb.ACTION_LABELS[action]})
        value = {a: u * float(p.get(a, np.nan)) for a, u in units.items() if u > 0}
        nav = cash + float(np.nansum(list(value.values())))
        nav_hist[i], cash_hist[i] = nav, cash
        for a, v in value.items():
            w_hist[i, col[a]] = v / nav if nav > 0 else 0.0

    last = prices.iloc[-1]
    for a, tr in trades.items():  # lignes détenues : valorisées au dernier cours
        mark = units[a] * float(last.get(a, np.nan))
        tr.update(exit_date=pd.NaT, exit_reason="EN_COURS", avg_exit_price=float(last.get(a, np.nan)),
                  pnl=mark - tr["buy_notional"] - tr["fees"])
        tr["return"] = float(last.get(a, np.nan)) / tr["avg_entry_price"] - 1.0
        closed.append(tr)
    trade_cols = ["asset", "asset_class", "entry_signal_date", "first_fill", "exit_date", "exit_reason",
                  "avg_entry_price", "avg_exit_price", "buy_notional", "fees", "pnl", "return"]
    weights = pd.DataFrame(w_hist, index=cal, columns=prices.columns)
    return SuiviReel(
        config=SimpleNamespace(trading_start=start, lines=lines),
        equity=pd.Series(nav_hist, index=cal, name="Portefeuille réel"),
        journal=pd.DataFrame(moves, columns=["date", "asset", "action", "price", "amount", "label"]),
        trades=pd.DataFrame(closed, columns=trade_cols).sort_values("first_fill").reset_index(drop=True),
        scales=(weights > 0).astype(float),
        weights=weights,
        gross=weights.sum(axis=1).rename("gross"),
        signals=SimpleNamespace(prices=prices),
        cash=pd.Series(cash_hist, index=cal, name="cash"),
    )
