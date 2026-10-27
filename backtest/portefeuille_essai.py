# -*- coding: utf-8 -*-
"""
portefeuille_essai.py — Portefeuille d'essai « fonds OU radar », suivi en parallèle du portefeuille réel
=====================================================================================================

Décision des fondateurs (27/10/2026), après melange_radar.py : suivre sur la page Desk, sans rien changer
au fonds, un portefeuille d'essai qui applique le mélange « fonds OU radar » avec les règles du
portefeuille réel (suivi_signaux.py) :
  * départ le même jour que le portefeuille réel (smart_money.LIVE_START), 100 % en espèces ;
  * achats : d'abord les achats et renforcements publiés par le fonds (ligne complétée jusqu'à un douzième
    du portefeuille), puis, s'il reste des espèces, les alertes d'accumulation du radar (score >= +2,5),
    les plus fortes d'abord, sur des titres non détenus ;
  * ventes (toute la ligne) : vente publiée par le fonds ; ou, après la durée minimale de détention du fonds,
    distribution confirmée selon l'échelle de sortie du fonds (deux jambes vendeuses, ou liquidation des
    institutions) ; ou perte critique sur la ligne (même seuil que le gestionnaire de risque du fonds) ;
  * allègement = rien ; pas de ré-achat d'une alerte du radar pendant le délai de ré-achat du fonds ;
  * ordres au cours de clôture du jour, mêmes frais que le portefeuille réel ; espèces non rémunérées.
Seules les données connues à la clôture de chaque séance sont utilisées.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Optional

import numpy as np
import pandas as pd

import flow_backtest as fb
import suivi_signaux as ss
from institutional_radar import ALERT_THRESHOLD

RADAR_BUY = "ENTRY_RADAR"
LABELS = {**fb.ACTION_LABELS, RADAR_BUY: "Achat — alerte d'accumulation du radar"}


def follow_blend(fund: fb.BacktestResult, start: str, capital: float = 100_000.0, lines: int = ss.LINES,
                 fee: float = ss.FEE) -> ss.SuiviReel:
    """Portefeuille d'essai « fonds OU radar » depuis `start` (même forme que suivi_signaux.follow)."""
    sig, cfg = fund.signals, fund.config
    prices = sig.prices.ffill()
    cal, names = prices.index, list(prices.columns)
    col = {a: k for k, a in enumerate(names)}
    t0 = pd.Timestamp(start)
    radar = (sig.radar.score.reindex(index=cal, columns=names) if sig.radar is not None
             else pd.DataFrame(np.nan, index=cal, columns=names))
    legs = sig.dist_slow.astype(int) + sig.dist_fast.astype(int) + sig.dist_foot.astype(int)
    exit_rule = (sig.dist_hard | (legs >= 2)).reindex(index=cal, columns=names).fillna(False)
    valid = sig.price_valid.reindex(index=cal, columns=names).fillna(False)
    vol = sig.vol.reindex(index=cal, columns=names)
    events = fund.journal[pd.to_datetime(fund.journal["date"]) >= t0]
    events = events[events["action"].isin(ss.BUY + ss.SELL)]
    by_day = {d: g for d, g in events.groupby(pd.to_datetime(events["date"]))}

    cash = capital
    units: dict[str, float] = {}
    trades: dict[str, dict] = {}
    held_since: dict[str, pd.Timestamp] = {}
    stop_at: dict[str, float] = {}
    last_exit: dict[str, pd.Timestamp] = {}
    closed: list[dict] = []
    moves: list[dict] = []
    nav_hist, cash_hist = np.full(len(cal), capital), np.full(len(cal), capital)
    w_hist = np.zeros((len(cal), len(names)))

    def nav_at(p: pd.Series) -> float:
        return cash + sum(u * float(p.get(x, 0.0)) for x, u in units.items() if u > 0)

    def sell(a: str, t: pd.Timestamp, price: float, action: str) -> None:
        nonlocal cash
        proceeds = units[a] * price
        cash += proceeds * (1 - fee)
        tr = trades.pop(a)
        tr.update(exit_date=t, exit_reason=action, avg_exit_price=price,
                  pnl=proceeds * (1 - fee) - tr["buy_notional"] - tr["fees"])
        tr["return"] = price / tr["avg_entry_price"] - 1.0
        closed.append(tr)
        moves.append({"date": t, "asset": a, "action": action, "price": price, "amount": -proceeds,
                      "label": LABELS[action]})
        units[a] = 0.0
        held_since.pop(a, None)
        last_exit[a] = t

    def buy(a: str, t: pd.Timestamp, price: float, action: str, p: pd.Series, i: int) -> None:
        nonlocal cash
        target = nav_at(p) / lines
        held = units.get(a, 0.0) * price
        amount = min(target - held, cash / (1 + fee))
        if amount < ss.MIN_ORDER * target:
            return  # ligne déjà pleine, ou plus assez d'espèces
        qty = amount / price
        cash -= amount * (1 + fee)
        if held > 0:
            action = "ADD_REACCUMULATION"
        else:
            action = action if action == RADAR_BUY else "ENTRY"
            held_since[a] = t
            v = vol.iat[i, col[a]]
            limit = max(cfg.position_stop_loss or 1.0, cfg.stop_vol_multiple * (0.0 if np.isnan(v) else v))
            stop_at[a] = limit
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
                      "label": LABELS[action]})

    for i, t in enumerate(cal):
        p = prices.iloc[i]
        if t >= t0:
            day = by_day.get(t)
            fund_sells = set(day.loc[day["action"].isin(ss.SELL), "asset"]) if day is not None else set()
            sell_action = dict(zip(day["asset"], day["action"])) if day is not None else {}
            # 1. Ventes d'abord : elles libèrent des espèces pour les achats du même jour
            for a in [x for x, u in units.items() if u > 0]:
                price = float(p.get(a, np.nan))
                if not np.isfinite(price) or price <= 0:
                    continue
                if a in fund_sells:
                    sell(a, t, price, sell_action[a])
                elif price <= trades[a]["avg_entry_price"] * (1 - stop_at.get(a, 1.0)):
                    sell(a, t, price, "RISK_STOP")
                elif (t - held_since[a]).days >= cfg.min_holding_days and exit_rule.iat[i, col[a]]:
                    sell(a, t, price, "EXIT_INSTITUTIONAL_LIQUIDATION" if sig.dist_hard.iat[i, col[a]]
                         else "EXIT_DISTRIBUTION_CONFIRMED")
            # 2. Achats publiés par le fonds, puis alertes d'accumulation du radar sur les titres non détenus
            if day is not None:
                for ev in day[day["action"].isin(ss.BUY)].itertuples():
                    price = float(p.get(ev.asset, np.nan))
                    if np.isfinite(price) and price > 0:
                        buy(ev.asset, t, price, ev.action, p, i)
            row = radar.iloc[i]
            for a, _ in row[row >= ALERT_THRESHOLD].sort_values(ascending=False).items():
                price = float(p.get(a, np.nan))
                if (units.get(a, 0.0) > 0 or not valid.iat[i, col[a]] or exit_rule.iat[i, col[a]]
                        or not np.isfinite(price) or price <= 0
                        or (a in last_exit and (t - last_exit[a]).days < cfg.reentry_cooldown_days)):
                    continue
                buy(a, t, price, RADAR_BUY, p, i)
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
    weights = pd.DataFrame(w_hist, index=cal, columns=names)
    return ss.SuiviReel(
        config=SimpleNamespace(trading_start=start, lines=lines),
        equity=pd.Series(nav_hist, index=cal, name="Portefeuille d'essai"),
        journal=pd.DataFrame(moves, columns=["date", "asset", "action", "price", "amount", "label"]),
        trades=pd.DataFrame(closed, columns=trade_cols).sort_values("first_fill").reset_index(drop=True),
        scales=(weights > 0).astype(float),
        weights=weights,
        gross=weights.sum(axis=1).rename("gross"),
        signals=SimpleNamespace(prices=prices),
        cash=pd.Series(cash_hist, index=cal, name="cash"),
    )


def summary(live: ss.SuiviReel, start: str, spy: Optional[pd.Series] = None) -> dict:
    eq = live.equity[live.equity.index >= pd.Timestamp(start)]
    out = {"rendement": float(eq.iloc[-1] / eq.iloc[0] - 1.0) if len(eq) > 1 else 0.0,
           "especes": float(live.cash.iloc[-1] / eq.iloc[-1]) if len(eq) else 1.0,
           "lignes": int((live.weights.iloc[-1] > 0).sum())}
    if spy is not None and len(eq) > 1:
        s = spy.reindex(eq.index).ffill()
        out["spy"] = float(s.iloc[-1] / s.iloc[0] - 1.0)
    return out


def main(argv: Optional[list[str]] = None) -> dict:
    """Affiche le portefeuille d'essai à côté du portefeuille réel (mêmes données que la page Desk)."""
    import argparse

    import radar_seul as rs  # imports tardifs : la page Desk n'a besoin que de follow_blend
    import smart_money as sm
    parser = argparse.ArgumentParser(description="Portefeuille d'essai « fonds OU radar »")
    parser.add_argument("--data-dir", default=str(rs.ROOT / "data" / "phase1" / "engine"))
    args = parser.parse_args(argv)
    engine = Path(args.data_dir)
    synthetic = not (engine / "prices.csv").exists()
    data = fb.generate_synthetic_market() if synthetic else fb.load_market_from_csv(engine)
    fund = fb.run_backtest(data, sm.phase1_config(data))
    start = sm.LIVE_START if not synthetic else f"{fund.equity.index[-40]:%Y-%m-%d}"
    spy = rs.spy_series(data, synthetic)
    essai, real = follow_blend(fund, start), ss.follow(fund, start)
    res = {"depart": start, "essai": summary(essai, start, spy), "reel": summary(real, start, spy)}
    print(f"# Portefeuille d'essai « fonds OU radar » depuis le {pd.Timestamp(start):%d/%m/%Y}"
          f"{' (DONNÉES SYNTHÉTIQUES)' if synthetic else ''}\n")
    for name, key, live in (("Essai", "essai", essai), ("Réel", "reel", real)):
        s = res[key]
        print(f"- {name} : {rs.pct(s['rendement'])} ; {s['lignes']} lignes ; espèces {s['especes']:.0%}"
              + (f" ; S&P 500 {rs.pct(s['spy'])}" if "spy" in s else ""))
        held = live.trades[live.trades["exit_reason"] == "EN_COURS"]
        print("  Lignes : " + (", ".join(f"{r['asset']} {rs.pct(r['return'])}" for _, r in held.iterrows())
                               or "aucune"))
        print("  Mouvements : " + (", ".join(f"{pd.Timestamp(m.date):%d/%m} {m.label} {m.asset}"
                                              for m in live.journal.itertuples()) or "aucun"))
    return res


if __name__ == "__main__":
    main()
