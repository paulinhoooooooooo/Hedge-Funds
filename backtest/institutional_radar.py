# -*- coding: utf-8 -*-
"""
institutional_radar.py — Radar des grands acteurs, sur plusieurs unités de temps
================================================================================

Objectif des fondateurs : repérer presque en direct qu'une institution est en train d'acheter
ou de vendre une action (« mouvement bizarre, volume très fort d'un coup, gros achats »), en
regardant l'HEURE, le JOUR, la SEMAINE et le MOIS.

Personne ne voit l'identité des acheteurs dans les données publiques : le radar cherche des
TRACES, puis les recoupe entre unités de temps. Pour chaque unité de temps :
  * volume anormal : volume de la fenêtre / volume habituel sur la même durée ;
  * pression acheteuse : Chaikin Money Flow de la fenêtre (clôtures près des plus hauts sur
    fort volume = acheteurs pressés) et position du dernier cours dans la fourchette de la fenêtre ;
  * accumulation discrète (semaine, mois) : volume élevé SANS mouvement de prix — quelqu'un
    absorbe les ventes sans faire monter le cours, ce que font les institutions ;
  * échanges hors bourse (données FINRA gratuites, publiées le soir même, utilisées le
    lendemain) : part du volume passée hors des bourses et part de ventes à découvert hors
    bourse. Une part de vente à découvert élevée signale souvent des intermédiaires qui servent
    un gros acheteur (ils vendent à découvert pour le livrer).

Unités de temps (fenêtres glissantes, calculées chaque séance avec les seules données connues) :
  heure    dernière heure de cotation vs la même heure des 20 séances précédentes (données
           horaires optionnelles ; le volume horaire gratuit de Tiingo ne couvre que la bourse IEX)
  jour     la séance vs les 120 séances précédentes
  semaine  les 5 dernières séances vs les 26 semaines précédentes
  mois     les 21 dernières séances vs les 12 mois précédents

Score = somme pondérée des unités de temps en accumulation (+) ou distribution (-) :
heure 0,5 ; jour 1 ; semaine 1,5 ; mois 2. Alerte à partir de ±2,5, c'est-à-dire au moins deux
unités de temps concordantes. Limites : résultats d'entreprise, échéances d'options et
rééquilibrages d'indices créent aussi des volumes anormaux ; la part hors bourse inclut les
ordres des particuliers internalisés par les courtiers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

# nom -> (fenêtre en séances, historique de référence en séances, seuil de volume anormal, poids)
TIMEFRAMES: dict[str, tuple[int, int, float, float]] = {
    "jour": (1, 120, 2.5, 1.0),
    "semaine": (5, 130, 1.8, 1.5),
    "mois": (21, 252, 1.4, 2.0),
}
HOUR_WEIGHT = 0.5
HOUR_VOLUME_THRESHOLD = 3.0
ALERT_THRESHOLD = 2.5
PRESSURE_THRESHOLD = 0.15  # Chaikin Money Flow de la fenêtre
STEALTH_VOLUME = 1.3  # accumulation discrète : volume >= 1,3 x la normale…
STEALTH_MOVE = 0.5  # … et mouvement de prix < 0,5 écart-type


@dataclass
class TimeframeView:
    volume_ratio: pd.DataFrame
    pressure: pd.DataFrame  # Chaikin Money Flow de la fenêtre
    close_location: pd.DataFrame  # -1 = plus bas de la fenêtre, +1 = plus haut
    move: pd.DataFrame  # variation de prix en écarts-types
    state: pd.DataFrame  # +1 accumulation, +0.5 accumulation discrète, -1 distribution, 0 rien
    offx_share: Optional[pd.DataFrame] = None  # part du volume hors bourse
    offx_share_normal: Optional[pd.DataFrame] = None
    offx_short: Optional[pd.DataFrame] = None  # part de ventes à découvert hors bourse
    offx_short_normal: Optional[pd.DataFrame] = None


@dataclass
class Radar:
    views: dict[str, TimeframeView]
    score: pd.DataFrame
    hourly: dict[str, dict] = field(default_factory=dict)  # actif -> dernière heure analysée

    def alerts(self, date=None, threshold: float = ALERT_THRESHOLD) -> pd.DataFrame:
        """Alertes d'une séance (la dernière par défaut), les plus fortes d'abord."""
        i = len(self.score) - 1 if date is None else int(self.score.index.searchsorted(pd.Timestamp(date), side="right")) - 1
        row = self.score.iloc[i]
        hits = row[row.abs() >= threshold].sort_values(key=np.abs, ascending=False)
        return pd.DataFrame([{"date": self.score.index[i], "asset": a, "score": s,
                              "sens": "accumulation" if s > 0 else "distribution",
                              "explication": self.explain(a, i)} for a, s in hits.items()])

    def explain(self, asset: str, i: int) -> str:
        """Explication en français simple, unité de temps par unité de temps."""
        parts = []
        hour = self.hourly.get(asset)
        if hour and hour.get("state"):
            side = "achats" if hour["state"] > 0 else "ventes"
            parts.append(f"Heure : volume ×{hour['volume_ratio']:.1f} la normale de ce créneau, {side} dominants.")
        for name, view in self.views.items():
            state = view.state[asset].iloc[i]
            if state == 0 or not np.isfinite(state):
                continue
            vr, pr = view.volume_ratio[asset].iloc[i], view.pressure[asset].iloc[i]
            label = name.capitalize()
            if state == 0.5:
                text = (f"{label} : volume ×{vr:.1f} la normale sans mouvement de prix — quelqu'un absorbe "
                        f"les ventes (accumulation discrète).")
            else:
                side = "acheteuse" if state > 0 else "vendeuse"
                text = f"{label} : volume ×{vr:.1f} la normale, pression {side} ({pr:+.2f})."
            if view.offx_share is not None:
                share, normal = view.offx_share[asset].iloc[i], view.offx_share_normal[asset].iloc[i]
                short, short_n = view.offx_short[asset].iloc[i], view.offx_short_normal[asset].iloc[i]
                if np.isfinite(share) and np.isfinite(normal) and share - normal >= 0.05:
                    text += f" Échanges hors bourse {share:.0%} du volume (habituellement {normal:.0%})."
                if state > 0 and np.isfinite(short) and np.isfinite(short_n) and short - short_n >= 0.05:
                    text += (f" Ventes à découvert hors bourse {short:.0%} (habituellement {short_n:.0%}) : "
                             f"des intermédiaires servent probablement un gros acheteur.")
            parts.append(text)
        return " ".join(parts) if parts else "Aucune trace anormale."


def _window_view(high: pd.DataFrame, low: pd.DataFrame, close: pd.DataFrame, volume: pd.DataFrame,
                 window: int, baseline: int, threshold: float, allow_stealth: bool,
                 offx_total: Optional[pd.DataFrame], offx_short: Optional[pd.DataFrame]) -> TimeframeView:
    minp = max(1, window)
    vol_w = volume.rolling(window, min_periods=minp).sum()
    normal = volume.shift(window).rolling(baseline, min_periods=baseline // 2).mean() * window
    volume_ratio = vol_w / normal.where(normal > 0)

    spread = (high - low)
    mfm = (((close - low) - (high - close)) / spread.where(spread > 0)).fillna(0.0)
    pressure = (mfm * volume).rolling(window, min_periods=minp).sum() / vol_w.where(vol_w > 0)
    hi_w, lo_w = high.rolling(window, min_periods=minp).max(), low.rolling(window, min_periods=minp).min()
    rng = (hi_w - lo_w)
    close_location = ((close - lo_w) - (hi_w - close)) / rng.where(rng > 0)
    daily_ret = close / close.shift(1) - 1.0
    sigma = daily_ret.shift(window).rolling(baseline, min_periods=baseline // 2).std()
    move = (close / close.shift(window) - 1.0) / (sigma * np.sqrt(window))

    hot = volume_ratio >= threshold
    state = pd.DataFrame(0.0, index=close.index, columns=close.columns)
    state = state.mask(hot & (pressure >= PRESSURE_THRESHOLD), 1.0)
    state = state.mask(hot & (pressure <= -PRESSURE_THRESHOLD), -1.0)
    if allow_stealth:
        stealth = ((volume_ratio >= STEALTH_VOLUME) & (move.abs() < STEALTH_MOVE)
                   & (pressure >= PRESSURE_THRESHOLD / 1.5) & (state == 0))
        state = state.mask(stealth, 0.5)

    view = TimeframeView(volume_ratio, pressure, close_location, move, state)
    if offx_total is not None:
        tot = offx_total.reindex_like(close)
        sh = offx_short.reindex_like(close) if offx_short is not None else None
        tot_w = tot.rolling(window, min_periods=minp).sum()
        view.offx_share = tot_w / vol_w.where(vol_w > 0)
        view.offx_share_normal = (tot.shift(window).rolling(baseline, min_periods=baseline // 2).sum()
                                  / volume.shift(window).rolling(baseline, min_periods=baseline // 2).sum())
        if sh is not None:
            view.offx_short = sh.rolling(window, min_periods=minp).sum() / tot_w.where(tot_w > 0)
            view.offx_short_normal = (sh.shift(window).rolling(baseline, min_periods=baseline // 2).sum()
                                      / tot.shift(window).rolling(baseline, min_periods=baseline // 2).sum())
    return view


def hourly_view(bars: pd.DataFrame, sessions: int = 20) -> dict:
    """Dernière heure de cotation vs la même heure des `sessions` séances précédentes.

    bars : index datetime, colonnes high, low, close, volume (une ligne par heure).
    Le volume d'une heure se compare à la même heure des autres jours : l'ouverture et la
    clôture sont toujours plus actives que la mi-journée.
    """
    if bars is None or len(bars) < 10:
        return {}
    b = bars.sort_index()
    last = b.iloc[-1]
    same_hour = b[b.index.hour == b.index[-1].hour].iloc[:-1].tail(sessions)
    normal = same_hour["volume"].mean()
    if not normal or not np.isfinite(normal):
        return {}
    ratio = float(last["volume"] / normal)
    rng = last["high"] - last["low"]
    loc = float(((last["close"] - last["low"]) - (last["high"] - last["close"])) / rng) if rng > 0 else 0.0
    state = 0
    if ratio >= HOUR_VOLUME_THRESHOLD:
        state = 1 if loc >= 0.3 else (-1 if loc <= -0.3 else 0)
    return {"time": b.index[-1], "volume_ratio": ratio, "close_location": loc, "state": state}


def compute_radar(high: pd.DataFrame, low: pd.DataFrame, close: pd.DataFrame, volume: pd.DataFrame,
                  offx_total: Optional[pd.DataFrame] = None, offx_short: Optional[pd.DataFrame] = None,
                  hourly: Optional[dict[str, pd.DataFrame]] = None) -> Radar:
    """Radar complet. Les données hors bourse du jour J (publiées le soir) sont décalées d'une séance."""
    volume = volume.where(volume > 0)
    if offx_total is not None:
        offx_total = offx_total.reindex_like(close).shift(1)
        offx_short = offx_short.reindex_like(close).shift(1) if offx_short is not None else None
    views = {}
    score = pd.DataFrame(0.0, index=close.index, columns=close.columns)
    for name, (window, baseline, threshold, weight) in TIMEFRAMES.items():
        view = _window_view(high, low, close, volume, window, baseline, threshold,
                            allow_stealth=window > 1, offx_total=offx_total, offx_short=offx_short)
        views[name] = view
        score = score + weight * view.state.fillna(0.0)
    radar = Radar(views=views, score=score)
    for asset, bars in (hourly or {}).items():
        view = hourly_view(bars)
        if view:
            radar.hourly[asset] = view
            if asset in score.columns and view["state"]:
                score.iloc[-1, score.columns.get_loc(asset)] += HOUR_WEIGHT * view["state"]
    return radar


def alert_history(radar: Radar, close: pd.DataFrame, as_of=None, horizons=(21, 63, 126),
                  threshold: float = ALERT_THRESHOLD, cooldown: int = 10, side: int = 1) -> dict:
    """Ce qu'il s'est passé après les alertes passées, avec les seules issues connues à `as_of`.

    Un « épisode » est le premier jour d'alerte après au moins `cooldown` séances sans alerte :
    une même accumulation n'est comptée qu'une fois. Retourne, par horizon (en séances), le nombre
    d'épisodes, la part de hausses et le rendement moyen et médian.
    """
    flag = (radar.score * side) >= threshold
    recent = flag.shift(1).rolling(cooldown, min_periods=1).max().fillna(0).astype(bool)
    start = flag & ~recent
    idx = close.index
    i_end = len(idx) - 1 if as_of is None else int(idx.searchsorted(pd.Timestamp(as_of), side="right")) - 1
    out = {}
    for h in horizons:
        fwd = close.shift(-h) / close - 1.0
        last_ok = i_end - h
        if last_ok <= 0:
            out[h] = {"n": 0}
            continue
        vals = fwd.iloc[: last_ok + 1][start.iloc[: last_ok + 1]].stack().dropna()
        out[h] = {"n": int(len(vals)), "hit_rate": float((vals > 0).mean()) if len(vals) else np.nan,
                  "mean": float(vals.mean()) if len(vals) else np.nan,
                  "median": float(vals.median()) if len(vals) else np.nan}
    return out
