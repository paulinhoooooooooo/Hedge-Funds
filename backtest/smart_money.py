# -*- coding: utf-8 -*-
"""
smart_money.py — Liste « Smart Money » point-in-time et flux à budget nul (phase 1)
===================================================================================

Décisions des fondateurs (23/09/2026) mises en œuvre ici :
  * institutions de référence = une LISTE SMART MONEY : les gérants dont les portefeuilles
    déclarés (13F) ont le mieux performé par le passé, choisis date par date ;
  * budget données GRATUIT : les flux des ETF sectoriels (EPFR / parts en circulation, payants)
    sont remplacés par un proxy calculé à partir des cours et volumes gratuits.

Décision technique de l'équipe : la hausse de détention se mesure en NOMBRE D'ACTIONS
détenues (insensible aux variations de prix).

Chaîne de production (données SEC gratuites) :
    positions = flow_backtest.load_13f_positions(dossiers_sec, cusip_vers_ticker)
    rendements = manager_quarterly_returns(positions, prix)
    liste = select_smart_money(rendements, positions)
    holdings = smart_money_holdings_index(positions, liste)      # -> holdings.csv du moteur
    flows, aum = flows_from_ohlcv(ohlcv_des_etf)                  # -> flows.csv du moteur
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from flow_backtest import _as_ns

QUARTER = pd.offsets.QuarterEnd(1)

# Seuils du moteur quand la jambe rapide est le proxy volume (unités du Chaikin Money Flow,
# identiques à l'indicateur TradingView « Smart Money Flow Monitor »).
PROXY_FLOW_SETTINGS = {"entry_flow_threshold": 0.0, "exit_flow_threshold": -0.05}

# Réglages du fonds sur données réelles, adoptés par les fondateurs le 24/09/2026
# (docs/PISTES_AMELIORATION.md) : 12 lignes de même poids (piste 1) et trésorerie non investie
# placée dans le S&P 500 (piste 2, si le cours du SPY est fourni : fichier market.csv).
PHASE1_FUND_SETTINGS = {**PROXY_FLOW_SETTINGS, "sizing": "equal", "max_positions": 12}


def phase1_config(data=None, **overrides):
    """StrategyConfig du fonds sur données réelles ; la piste 2 n'est active que si data.market existe."""
    from flow_backtest import StrategyConfig
    settings = {**PHASE1_FUND_SETTINGS, "idle_cash_in_market": data is not None and data.market is not None}
    return StrategyConfig(**{**settings, **overrides})


def _quarter_end(dates: pd.Series) -> pd.Series:
    """Ramène chaque date d'arrêté à la fin de trimestre civile correspondante."""
    ts = pd.to_datetime(dates)
    return (ts + pd.offsets.QuarterEnd(0)).astype("datetime64[ns]")


def manager_quarterly_returns(positions: pd.DataFrame, prices: pd.DataFrame, weights: str = "shares") -> pd.DataFrame:
    """Rendement « copie conforme » de chaque gérant, trimestre par trimestre.

    Le portefeuille déclaré à la fin du trimestre q est conservé tel quel jusqu'à la fin du
    trimestre suivant, pondéré par sa valeur à q. Ce rendement n'est CONNU qu'à la fin du
    trimestre suivant (colonne known_at) : c'est la date à partir de laquelle il peut servir
    à sélectionner les gérants.

    positions : cik, asset, period_end, shares (sortie de load_13f_positions) ; avec
                weights="value", une colonne value (valeur déclarée) sert de pondération, ce
                qui rend le calcul insensible aux divisions d'actions entre la déclaration et
                les cours ajustés.
    prices    : DataFrame [date x asset] de cours ajustés des divisions d'actions
    Retourne cik, period_end, known_at, ret, n_priced.
    """
    pos = positions.assign(period_end=_quarter_end(positions["period_end"]))
    pos = pos.assign(known_at=pos["period_end"] + QUARTER)
    px = prices.sort_index().ffill()
    px.index = _as_ns(px.index)
    dates = pd.DatetimeIndex(sorted(set(pos["period_end"]) | set(pos["known_at"])))
    idx = px.index.searchsorted(dates, side="right") - 1
    ok = idx >= 0
    at = pd.DataFrame(px.to_numpy()[idx[ok]], index=dates[ok], columns=px.columns)
    at.index.name = "date"
    long = at.stack().rename("price").reset_index().rename(columns={"level_1": "asset"})
    long.columns = ["date", "asset", "price"]

    pos = pos.merge(long.rename(columns={"date": "period_end", "price": "p0"}), on=["period_end", "asset"])
    pos = pos.merge(long.rename(columns={"date": "known_at", "price": "p1"}), on=["known_at", "asset"])
    pos = pos[(pos["p0"] > 0) & (pos["p1"] > 0)]
    if weights == "value":
        pos = pos[pos["value"] > 0]
        pos = pos.assign(v0=pos["value"], v1=pos["value"] * pos["p1"] / pos["p0"])
    else:
        pos = pos.assign(v0=pos["shares"] * pos["p0"], v1=pos["shares"] * pos["p1"])
    out = pos.groupby(["cik", "period_end", "known_at"]).agg(v0=("v0", "sum"), v1=("v1", "sum"),
                                                             n_priced=("asset", "nunique")).reset_index()
    out["ret"] = out["v1"] / out["v0"] - 1.0
    return out[["cik", "period_end", "known_at", "ret", "n_priced"]]


def select_smart_money(
    returns: pd.DataFrame,
    positions: pd.DataFrame,
    lookback: int = 8,
    top_n: int = 50,
    min_positions: int = 20,
    max_positions: int = 1500,
    position_counts: pd.Series | None = None,
) -> pd.DataFrame:
    """Liste Smart Money point-in-time.

    À chaque fin de trimestre q, on classe les gérants selon le ratio d'information de leur
    rendement « copie conforme » en excès de la moyenne des gérants, sur les `lookback`
    derniers trimestres CONNUS à q (known_at <= q). Sont exclus :
      * les gérants sans historique complet sur la fenêtre ;
      * les quasi-indiciels (plus de max_positions lignes) : leurs achats ne sont pas des convictions ;
      * les portefeuilles trop concentrés (moins de min_positions lignes).
    Aucune information postérieure à q n'est utilisée ; la liste de q ne sert qu'aux rapports
    de q, eux-mêmes publiés 45 jours plus tard.

    position_counts : nombre TOTAL de lignes de chaque gérant, indexé par (period_end, cik),
    quand `positions` ne contient qu'une partie des portefeuilles (l'univers suivi).

    Retourne period_end, cik, score, rank.
    """
    r = returns.assign(excess=returns["ret"] - returns.groupby("period_end")["ret"].transform("mean"))
    excess = r.pivot_table(index="known_at", columns="cik", values="excess").sort_index()
    pos = positions.assign(period_end=_quarter_end(positions["period_end"]))
    counts = pos.groupby(["period_end", "cik"])["asset"].nunique() if position_counts is None else position_counts

    rows = []
    for q in sorted(pos["period_end"].unique()):
        hist = excess.loc[excess.index <= q].tail(lookback)
        if len(hist) < lookback or q not in counts.index.get_level_values(0):
            continue
        complete = hist.notna().sum() >= lookback
        std = hist.std(ddof=1)
        score = (hist.mean() / std)[complete & (std > 0)]
        n = counts.loc[q]
        eligible = n.index[(n >= min_positions) & (n <= max_positions)]
        best = score[score.index.isin(eligible)].sort_values(ascending=False).head(top_n)
        rows += [{"period_end": q, "cik": cik, "score": s, "rank": k}
                 for k, (cik, s) in enumerate(best.items(), start=1)]
    return pd.DataFrame(rows, columns=["period_end", "cik", "score", "rank"])


def smart_money_holdings_index(
    positions: pd.DataFrame,
    selection: pd.DataFrame,
    max_change: float = 1.0,
    min_change: float = -0.95,
) -> pd.DataFrame:
    """Indice de détention Smart Money par actif, au format holdings.csv du moteur.

    La liste évolue d'un trimestre à l'autre : comparer des totaux bruts confondrait « ils
    achètent » et « la liste a changé ». La variation du trimestre q est donc calculée à
    COMPOSITION CONSTANTE (les gérants retenus à q, comparés à leurs propres positions de q-1),
    puis chaînée dans un indice de niveau (base 100). Le moteur retrouve exactement cette
    variation en comparant deux rapports consécutifs.

    Une position ouverte à partir de zéro compte pour +100 % (plafond) ; une position soldée
    pour -95 % (plancher, le niveau doit rester positif).
    Retourne asset, period_end, filing_date, value, n_managers.
    """
    pos = positions.assign(period_end=_quarter_end(positions["period_end"]))
    level: dict[str, float] = {}
    frames = []
    for q in sorted(selection["period_end"].unique()):
        managers = set(selection.loc[selection["period_end"] == q, "cik"])
        cur_pos = pos[(pos["period_end"] == q) & pos["cik"].isin(managers)]
        prev_pos = pos[(pos["period_end"] == q - QUARTER) & pos["cik"].isin(managers)]
        if cur_pos.empty:
            continue
        cur = cur_pos.groupby("asset")["shares"].sum()
        prev = prev_pos.groupby("asset")["shares"].sum()
        both = pd.concat([cur.rename("cur"), prev.rename("prev")], axis=1).fillna(0.0)
        change = pd.Series(max_change, index=both.index)
        held_before = both["prev"] > 0
        change[held_before] = both.loc[held_before, "cur"] / both.loc[held_before, "prev"] - 1.0
        change = change.clip(min_change, max_change)
        values = np.array([level.get(a, 100.0) for a in both.index]) * (1.0 + change.to_numpy())
        level.update(zip(both.index, values))
        frames.append(pd.DataFrame({
            "asset": both.index,
            "period_end": q,
            "filing_date": cur_pos["filing_date"].max(),
            "value": values,
            "n_managers": cur_pos.groupby("asset")["cik"].nunique().reindex(both.index).fillna(0).astype(int).to_numpy(),
        }))
    if not frames:
        return pd.DataFrame(columns=["asset", "period_end", "filing_date", "value", "n_managers"])
    return pd.concat(frames, ignore_index=True).sort_values(["asset", "period_end"]).reset_index(drop=True)


def flows_from_ohlcv(ohlcv: pd.DataFrame, window: str = "30D") -> tuple[pd.DataFrame, pd.DataFrame]:
    """Proxy gratuit des flux d'un véhicule (ETF) à partir de ses cours et volumes quotidiens.

    Flux du jour = multiplicateur de Chaikin x volume en dollars : positif quand la séance
    clôture près de son plus haut (pression acheteuse), négatif près de son plus bas.
    « Encours » = volume en dollars cumulé sur la fenêtre. Le ratio calculé par le moteur
    (flux cumulés / encours) est alors le Chaikin Money Flow sur 30 jours, compris entre -1 et 1 :
    utiliser les seuils PROXY_FLOW_SETTINGS.

    C'est un proxy : il mesure la pression acheteuse sur le marché, pas les souscriptions et
    rachats réels. À remplacer par les flux réels (parts en circulation, EPFR) dès que le budget
    le permet.

    ohlcv : DataFrame long avec les colonnes date, vehicle, high, low, close, volume.
    Retourne (flows, aum), deux DataFrame [date x véhicule] au format du moteur.
    """
    df = ohlcv.assign(date=_as_ns(ohlcv["date"]))
    spread = df["high"] - df["low"]
    mfm = np.where(spread > 0, ((df["close"] - df["low"]) - (df["high"] - df["close"])) / spread.where(spread > 0), 0.0)
    dollar_volume = df["close"] * df["volume"]
    df = df.assign(flow=mfm * dollar_volume, dollar_volume=dollar_volume)
    flows = df.pivot_table(index="date", columns="vehicle", values="flow", aggfunc="sum").sort_index()
    dvol = df.pivot_table(index="date", columns="vehicle", values="dollar_volume", aggfunc="sum").sort_index()
    min_periods = max(3, int(pd.Timedelta(window).days * 0.5))
    aum = dvol.rolling(window, min_periods=min_periods).sum()
    return flows, aum
