# -*- coding: utf-8 -*-
"""
zigzag.py — Zig Zag « 2 bougies » (traduction fidèle de l'indicateur Pine du fondateur)
=====================================================================================

Règles (identiques à tradingview/zigzag_2_bougies.pine) :
  * bougie verte = clôture > clôture précédente, rouge = clôture < clôture précédente ;
  * sommet validé par N bougies rouges consécutives après une phase haussière (N = 2 par défaut),
    creux validé par N bougies vertes consécutives ;
  * le pivot est placé sur le plus haut (ou le plus bas) de la phase, sur les mèches ;
  * bougie intérieure de même couleur que la bougie mère : ignorée ;
  * bougie qui englobe la précédente de même couleur : les deux comptent pour une ;
  * après un sommet, le creux suivant se cherche à partir des bougies rouges qui ont validé le sommet
    (jamais sur la bougie du sommet), et inversement.

Chaque pivot porte la bougie où il est placé (`barre`) et la bougie qui l'a validé (`confirme`) :
le scanner n'utilise un pivot qu'à partir de sa validation, comme sur le graphique en direct.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

import numpy as np


@dataclass(frozen=True)
class Pivot:
    barre: int        # bougie du pivot (plus haut ou plus bas de la phase)
    prix: float
    type: str         # "haut" ou "bas"
    confirme: int     # bougie qui a validé le pivot


def _atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, length: int = 14) -> np.ndarray:
    """ATR de TradingView (moyenne de Wilder), utilisé seulement si `corps_min` > 0."""
    n = len(close)
    tr = np.empty(n)
    tr[0] = high[0] - low[0]
    for i in range(1, n):
        tr[i] = max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1]))
    out = np.full(n, np.nan)
    if n >= length:
        out[length - 1] = tr[:length].mean()
        for i in range(length, n):
            out[i] = (out[i - 1] * (length - 1) + tr[i]) / length
    return out


def zigzag(high: Sequence[float], low: Sequence[float], close: Sequence[float],
           open_: Optional[Sequence[float]] = None, nb_bougies: int = 2, sur_meches: bool = True,
           ignorer_interieures: bool = True, corps_min: float = 0.0, ignorer_doji: bool = False,
           derniere_bougie: bool = False) -> list[Pivot]:
    """Pivots du Zig Zag « 2 bougies », dans l'ordre (le premier est le point de départ du tracé).

    `open_` n'est nécessaire que pour `sur_meches=False` (pivots sur les corps)."""
    high = np.asarray(high, dtype=float)
    low = np.asarray(low, dtype=float)
    close = np.asarray(close, dtype=float)
    n = len(close)
    if n == 0:
        return []
    if sur_meches:
        haut_p, bas_p = high, low
    else:
        if open_ is None:
            raise ValueError("open_ requis pour placer les pivots sur les corps")
        o = np.asarray(open_, dtype=float)
        haut_p, bas_p = np.maximum(o, close), np.minimum(o, close)
    atr = _atr(high, low, close) if corps_min > 0 else None

    pivots: list[Pivot] = []
    interieure = np.zeros(n, dtype=bool)
    mere_haut = mere_bas = None
    mere_couleur = 0
    cnt_bull = cnt_bear = 0
    debut_bull = debut_bear = None
    direction = 0
    ext_prix = ext_bar = None
    cand_prix = cand_bar = None
    piv_prix = piv_bar = None

    for i in range(n):
        if i == 0:
            couleur = 0
        else:
            couleur = 1 if close[i] > close[i - 1] else (-1 if close[i] < close[i - 1] else 0)

        meme_couleur = couleur != 0 and couleur == mere_couleur
        dans_mere = mere_haut is not None and high[i] <= mere_haut and low[i] >= mere_bas
        englobe_mere = mere_haut is not None and high[i] >= mere_haut and low[i] <= mere_bas
        inter = ignorer_interieures and meme_couleur and dans_mere
        englobante = ignorer_interieures and meme_couleur and englobe_mere and not inter
        interieure[i] = inter
        if not inter:
            mere_haut, mere_bas, mere_couleur = high[i], low[i], couleur

        petit = False
        if atr is not None and i > 0 and not np.isnan(atr[i]):
            petit = abs(close[i] - close[i - 1]) < corps_min * atr[i]
        bull = couleur == 1 and not petit and not inter
        bear = couleur == -1 and not petit and not inter

        if not inter:
            if bull:
                if not englobante:
                    cnt_bull += 1
                    if cnt_bull == 1:
                        debut_bull = i
                cnt_bear = 0
            elif bear:
                if not englobante:
                    cnt_bear += 1
                    if cnt_bear == 1:
                        debut_bear = i
                cnt_bull = 0
            elif not ignorer_doji:
                cnt_bull = cnt_bear = 0

        if direction == 0:
            if i + 1 < nb_bougies:
                continue
            fen = slice(i - nb_bougies + 1, i + 1)
            if cnt_bull >= nb_bougies:
                direction = 1
                k = _dernier_extreme(bas_p[fen], plus_bas=True) + fen.start
                piv_prix, piv_bar = bas_p[k], k
                k = _dernier_extreme(haut_p[fen], plus_bas=False) + fen.start
                ext_prix, ext_bar = haut_p[k], k
                cand_prix, cand_bar = haut_p[i], i
                pivots.append(Pivot(piv_bar, float(piv_prix), "bas", i))
            elif cnt_bear >= nb_bougies:
                direction = -1
                k = _dernier_extreme(haut_p[fen], plus_bas=False) + fen.start
                piv_prix, piv_bar = haut_p[k], k
                k = _dernier_extreme(bas_p[fen], plus_bas=True) + fen.start
                ext_prix, ext_bar = bas_p[k], k
                cand_prix, cand_bar = bas_p[i], i
                pivots.append(Pivot(piv_bar, float(piv_prix), "haut", i))
        elif direction == 1:
            if not inter and (ext_prix is None or haut_p[i] >= ext_prix):
                ext_prix, ext_bar = haut_p[i], i
            if bull:
                cand_prix, cand_bar = haut_p[i], i
            if cnt_bear >= nb_bougies:
                px, bx = (cand_prix, cand_bar) if derniere_bougie else (ext_prix, ext_bar)
                if px is None:
                    px, bx = haut_p[i], i
                pivots.append(Pivot(int(bx), float(px), "haut", i))
                piv_prix, piv_bar = px, bx
                direction = -1
                debut = max(i if debut_bear is None else debut_bear, piv_bar + 1)
                lo = lo_bar = None
                for j in range(i, debut - 1, -1):
                    if not interieure[j] and (lo is None or bas_p[j] < lo):
                        lo, lo_bar = bas_p[j], j
                ext_prix, ext_bar = lo, lo_bar
                cand_prix, cand_bar = bas_p[i], i
        else:
            if not inter and (ext_prix is None or bas_p[i] <= ext_prix):
                ext_prix, ext_bar = bas_p[i], i
            if bear:
                cand_prix, cand_bar = bas_p[i], i
            if cnt_bull >= nb_bougies:
                px, bx = (cand_prix, cand_bar) if derniere_bougie else (ext_prix, ext_bar)
                if px is None:
                    px, bx = bas_p[i], i
                pivots.append(Pivot(int(bx), float(px), "bas", i))
                piv_prix, piv_bar = px, bx
                direction = 1
                debut = max(i if debut_bull is None else debut_bull, piv_bar + 1)
                hi = hi_bar = None
                for j in range(i, debut - 1, -1):
                    if not interieure[j] and (hi is None or haut_p[j] > hi):
                        hi, hi_bar = haut_p[j], j
                ext_prix, ext_bar = hi, hi_bar
                cand_prix, cand_bar = haut_p[i], i
    return pivots


def _dernier_extreme(values: np.ndarray, plus_bas: bool) -> int:
    """Position du plus haut (ou plus bas) ; en cas d'égalité, la bougie la plus récente."""
    target = values.min() if plus_bas else values.max()
    return int(np.flatnonzero(values == target)[-1])
