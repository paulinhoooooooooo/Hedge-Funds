# -*- coding: utf-8 -*-
"""
market_footprint.py — Empreinte de marché des grands acteurs (banques, institutions) par le prix et le volume
============================================================================================================

Troisième jambe du fonds, demandée par les fondateurs : « voir à travers le marché où sont
les banques avec le volume et le prix ». Les grands acteurs ne peuvent pas cacher leur
volume : là où ils achètent massivement, le marché garde une trace.

Quatre mesures, toutes calculées avec les seules séances déjà clôturées :
  * PROFIL DE VOLUME (6 mois) : à quels prix s'est échangé l'essentiel du volume.
      - point de contrôle (POC) : le prix où il s'est le plus échangé ;
      - zone de valeur : la fourchette de prix qui concentre 70 % du volume.
    C'est la meilleure estimation publique du « prix de revient » des grands acheteurs :
    au-dessus de la zone, ils sont en gain et la défendent ; en dessous, ils sont en perte
    et ont souvent commencé à sortir.
  * VWAP 63 SÉANCES : prix moyen payé par l'ensemble des acheteurs du dernier trimestre
    (le rythme des déclarations 13F).
  * RATIO DE VOLUME HAUSSIER / BAISSIER (50 séances) : le volume des séances de hausse
    rapporté à celui des séances de baisse. Au-dessus de 1, les acheteurs sont plus
    nombreux (accumulation) ; en dessous, les vendeurs le sont (distribution).
  * JOURS DE DISTRIBUTION (25 séances) : séances de baisse d'au moins 0,2 % sur un volume
    au moins 20 % au-dessus de sa moyenne 50 séances, signe de ventes de grands acteurs
    (d'après la règle de W. O'Neil, durcie pour les actions individuelles : un volume
    simplement supérieur à la veille arrive une séance sur deux par pur hasard).
    Cinq ou plus en 25 séances : alerte.

Limites : le volume du FX au comptant n'est pas un vrai volume (utiliser les contrats à
terme) ; ces mesures voient la pression des grands acteurs, pas leur identité. Les
données nominatives sur les banques viennent d'ailleurs : rapport COT (catégorie
« Dealer / Intermediary » = banques), volumes des plateformes alternatives (FINRA ATS).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class Footprint:
    poc: pd.DataFrame  # point de contrôle du profil de volume
    va_low: pd.DataFrame  # bas de la zone de valeur
    va_high: pd.DataFrame  # haut de la zone de valeur
    vwap: pd.DataFrame  # prix moyen pondéré par le volume sur la fenêtre
    updown_ratio: pd.DataFrame  # volume des hausses / volume des baisses
    distribution_days: pd.DataFrame  # nombre de jours de distribution dans la fenêtre
    accumulation: pd.DataFrame  # empreinte acheteuse (bool)
    distribution: pd.DataFrame  # empreinte vendeuse (bool)


def _profile(tp: np.ndarray, vol: np.ndarray, bins: int, value_area: float) -> tuple[float, float, float]:
    """Point de contrôle et zone de valeur d'une fenêtre (volume affecté au prix typique)."""
    ok = np.isfinite(tp) & np.isfinite(vol) & (vol > 0)
    if ok.sum() < 10 or np.ptp(tp[ok]) == 0:
        return np.nan, np.nan, np.nan
    hist, edges = np.histogram(tp[ok], bins=bins, weights=vol[ok])
    k = int(np.argmax(hist))
    lo = hi = k
    covered, target = hist[k], value_area * hist.sum()
    while covered < target and (lo > 0 or hi < bins - 1):
        left = hist[lo - 1] if lo > 0 else -1.0
        right = hist[hi + 1] if hi < bins - 1 else -1.0
        if right >= left:
            hi += 1
            covered += hist[hi]
        else:
            lo -= 1
            covered += hist[lo]
    return (edges[k] + edges[k + 1]) / 2, edges[lo], edges[hi + 1]


def rolling_volume_profile(
    typical: pd.DataFrame, volume: pd.DataFrame, lookback: int = 126, bins: int = 40,
    value_area: float = 0.70, step: int = 5,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Profil de volume glissant, recalculé toutes les `step` séances puis reporté.

    La valeur publiée à la date t n'utilise que les séances jusqu'à t incluse.
    """
    arrays = [np.full(typical.shape, np.nan) for _ in range(3)]
    tp_all, vol_all = typical.to_numpy(float), volume.to_numpy(float)
    for j in range(typical.shape[1]):
        for end in range(lookback, len(typical) + 1, step):
            res = _profile(tp_all[end - lookback:end, j], vol_all[end - lookback:end, j], bins, value_area)
            for arr, value in zip(arrays, res):
                arr[end - 1, j] = value
    return tuple(pd.DataFrame(arr, index=typical.index, columns=typical.columns).ffill(limit=step - 1)
                 for arr in arrays)


def compute_footprint(
    high: pd.DataFrame, low: pd.DataFrame, close: pd.DataFrame, volume: pd.DataFrame,
    profile_lookback: int = 126, profile_bins: int = 40, value_area: float = 0.70,
    vwap_window: int = 63, updown_window: int = 50, updown_acc: float = 1.15, updown_dist: float = 0.85,
    dist_day_window: int = 25, dist_day_count: int = 5, dist_day_drop: float = -0.002,
    dist_day_volume: float = 1.2,
) -> Footprint:
    """Calcule l'empreinte de marché de chaque actif (DataFrame [date x actif] alignés)."""
    volume = volume.where(volume > 0)
    typical = (high + low + close) / 3.0
    ret = close / close.shift(1) - 1.0

    poc, va_low, va_high = rolling_volume_profile(typical, volume, profile_lookback, profile_bins, value_area)
    min_vwap = vwap_window // 2
    vwap = ((typical * volume).rolling(vwap_window, min_periods=min_vwap).sum()
            / volume.rolling(vwap_window, min_periods=min_vwap).sum())
    min_ud = updown_window // 2
    up_vol = volume.where(ret > 0, 0.0).rolling(updown_window, min_periods=min_ud).sum()
    down_vol = volume.where(ret < 0, 0.0).rolling(updown_window, min_periods=min_ud).sum()
    updown = up_vol / down_vol.where(down_vol > 0)
    avg_volume = volume.rolling(updown_window, min_periods=min_ud).mean().shift(1)
    heavy = volume >= dist_day_volume * avg_volume
    dist_day = ((ret <= dist_day_drop) & heavy).astype(float).where(volume.notna() & avg_volume.notna())
    dist_days = dist_day.rolling(dist_day_window, min_periods=dist_day_window).sum()

    accumulation = (updown >= updown_acc) & (close >= vwap)
    distribution = (dist_days >= dist_day_count) | ((updown <= updown_dist) & (close < va_low))
    return Footprint(poc=poc, va_low=va_low, va_high=va_high, vwap=vwap, updown_ratio=updown,
                     distribution_days=dist_days, accumulation=accumulation, distribution=distribution)


def describe_footprint(fp: Footprint, close: float, i: int, j: int) -> str:
    """Phrase d'explication pour la revue de position (ligne i, colonne j)."""
    poc, lo, hi = fp.poc.iat[i, j], fp.va_low.iat[i, j], fp.va_high.iat[i, j]
    ud, dd, vwap = fp.updown_ratio.iat[i, j], fp.distribution_days.iat[i, j], fp.vwap.iat[i, j]
    if not np.isfinite(poc):
        return "Empreinte de marché non disponible (pas de volume exploitable)."
    if close > hi:
        where = "au-dessus : les grands acheteurs sont en gain et défendent la zone"
    elif close < lo:
        where = "en dessous : les grands acheteurs sont en perte, risque de sorties"
    else:
        where = "à l'intérieur : le marché est à l'équilibre sur leur prix de revient"
    parts = [f"Empreinte de marché : sur 6 mois, 70 % du volume s'est échangé entre {lo:,.2f} et {hi:,.2f} "
             f"(zone de valeur ; prix le plus échangé {poc:,.2f}) ; cours {close:,.2f} {where}."]
    if np.isfinite(ud):
        tone = "acheteurs dominants" if ud >= 1.0 else "vendeurs dominants"
        parts.append(f"Volume des hausses / des baisses : {ud:.2f} ({tone}).")
    if np.isfinite(dd):
        parts.append(f"{int(dd)} jour(s) de distribution sur 25 séances.")
    if np.isfinite(vwap):
        side = "au-dessus" if close >= vwap else "en dessous"
        parts.append(f"Cours {side} du prix moyen des acheteurs du trimestre (VWAP {vwap:,.2f}).")
    return " ".join(parts)
