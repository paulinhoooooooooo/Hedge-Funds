# -*- coding: utf-8 -*-
"""
modeles_range.py — Ranges, touches et modèles d'accumulation / distribution
===========================================================================

Règles du fondateur (voir docs/SCANNER_RANGES.md) :

Range
  * Un range se forme entre un haut et un bas du Zig Zag « 2 bougies » après une hausse ou une
    baisse (l'impulsion). Niveaux du retracement de Fibonacci : 0 = haut, 1 = bas, 0,5 = milieu,
    extensions -0,3 (au-dessus du haut) et 1,3 (sous le bas).
  * Le range n'est validé que si le prix revient au 0,5 après le dernier point de l'impulsion. Pendant
    le repli, un petit creux (ou sommet) intermédiaire ne change pas le range en attente ; un nouveau
    point plus loin que l'extrême remplace l'impulsion par la dernière jambe du Zig Zag.
  * Une fois validé, le range est gardé jusqu'à sa mort : les petits mouvements intérieurs ne créent
    pas de nouveau range.
  * Une mèche ou une clôture au-delà du haut (ou du bas) décale le haut (ou le bas) sur cette mèche,
    tant que la clôture reste en deçà de -0,3 (ou de 1,3) ; tous les niveaux sont alors recalculés.
  * Une clôture au-delà de -0,3 ou de 1,3 tue le range.

Touches
  * Le haut et le bas qui forment le range sont la 1re touche de chaque côté ; les deux côtés sont
    suivis en parallèle (haut = distribution, bas = accumulation).
  * Après un retour au milieu, une nouvelle touche a lieu quand le prix atteint (mèche ou clôture)
    la zone d'offre (ou de demande), le niveau de liquidité, ou le haut (ou le bas) du range.
  * ALERTE : 2e touche puis retour au milieu. Sans retour au milieu, pas d'alerte.

Zone d'offre / de demande (cherchée de l'unité de temps du range vers le 5 min)
  * Distribution : la bougie verte avant la baisse, au sommet, avec une mèche haute d'au moins 40 % de
    la bougie ; zone = du haut de la mèche à la clôture. Accumulation : la bougie rouge avant la
    hausse, au creux, avec une mèche basse d'au moins 40 % ; zone = du bas de la mèche à la clôture.

Niveau de liquidité (même cascade d'unités de temps)
  * Sur la jambe du Zig Zag entre la touche et le retour au milieu : le premier sommet intermédiaire
    (distribution), qui doit se trouver entre 0 et 0,25 ; ou le premier creux intermédiaire
    (accumulation), entre 0,75 et 1. Jambe droite ou point trop éloigné : unité de temps inférieure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np
import pandas as pd

from zigzag import Pivot, zigzag

EXTENSION = 0.3          # niveaux -0,3 et 1,3
LIMITE_LIQUIDITE = 0.25  # le niveau de liquidité doit être entre 0 et 0,25 (ou 0,75 et 1)
MECHE_MIN = 0.40         # mèche de rejet : au moins 40 % de la hauteur de la bougie
TOUCHES_ALERTE = 2       # alerte à la 2e touche suivie d'un retour au milieu

CASCADES = {
    "W": ["W", "D", "4h", "1h", "30m", "15m", "5m"],
    "D": ["D", "4h", "1h", "30m", "15m", "5m"],
    "4h": ["4h", "1h", "30m", "15m", "5m"],
}
COTES = ("haut", "bas")
MODELE = {"haut": "distribution", "bas": "accumulation"}

# barres(ut, debut, fin) -> DataFrame [start, end, open, high, low, close] (horodatages UTC),
# bougies terminées dont le début est dans [debut, fin)
SourceBarres = Callable[[str, pd.Timestamp, pd.Timestamp], pd.DataFrame]


# =============================================================================
# Structures
# =============================================================================

@dataclass
class Zone:
    ut: str
    bas: float
    haut: float
    debut: pd.Timestamp   # bougie de la zone


@dataclass
class Liquidite:
    ut: str
    prix: float
    debut: pd.Timestamp   # bougie du point intermédiaire


@dataclass
class Seuil:
    """Ce qui compte comme une nouvelle touche d'un côté du range."""
    zone: Optional[Zone] = None
    liquidite: Optional[Liquidite] = None

    def prix(self, cote: str, extreme: float, milieu: float) -> float:
        """Prix à atteindre (mèche ou clôture) : le plus proche du milieu parmi zone, liquidité et
        extrême, en restant du bon côté du milieu."""
        prix = [extreme]
        if self.zone is not None:
            prix.append(self.zone.bas if cote == "haut" else self.zone.haut)
        if self.liquidite is not None:
            prix.append(self.liquidite.prix)
        if cote == "haut":
            return min(p for p in prix if p > milieu) if any(p > milieu for p in prix) else extreme
        return max(p for p in prix if p < milieu) if any(p < milieu for p in prix) else extreme


@dataclass
class Cote:
    touches: int = 1
    arme: bool = True                      # un retour au milieu a eu lieu depuis la dernière touche
    barres_touches: list = field(default_factory=list)
    alerte: Optional[int] = None           # bougie du retour au milieu qui suit la 2e touche
    seuil: Optional[Seuil] = None


@dataclass
class RangeTCT:
    a: Pivot                    # début de l'impulsion
    b: Pivot                    # fin de l'impulsion (dernier point avant le retour au milieu)
    milieu_atteint: int         # 1re bougie revenue au 0,5 après b
    validation: int             # max(milieu_atteint, validation du pivot b)
    croisement_a: int           # 1re bougie après a qui a franchi le 0,5 (jambe de la touche de a)
    haut0: float
    bas0: float
    haut: float = np.nan
    bas: float = np.nan
    mort: Optional[int] = None
    sens_mort: Optional[str] = None
    cotes: dict = field(default_factory=dict)
    evenements: list = field(default_factory=list)

    @property
    def vivant(self) -> bool:
        return self.mort is None

    def cote_extreme(self, cote: str) -> Pivot:
        """Pivot qui forme la 1re touche de ce côté."""
        return self.b if (self.b.type == cote) else self.a

    def jambe(self, cote: str) -> tuple[int, int]:
        """(bougie de la 1re touche, bougie du retour au milieu qui suit) de ce côté."""
        if self.b.type == cote:
            return self.b.barre, self.milieu_atteint
        return self.a.barre, self.croisement_a


def niveaux(haut: float, bas: float) -> dict:
    h = haut - bas
    return {"0": haut, "0.25": haut - 0.25 * h, "0.5": haut - 0.5 * h, "0.75": haut - 0.75 * h, "1": bas,
            "-0.3": haut + EXTENSION * h, "1.3": bas - EXTENSION * h}


# =============================================================================
# Détection des ranges sur une unité de temps
# =============================================================================

def _au_dela(p: Pivot, ref: Pivot) -> bool:
    return p.prix > ref.prix if p.type == "haut" else p.prix < ref.prix


def detecter_ranges(df: pd.DataFrame, pivots: Optional[list[Pivot]] = None,
                    seuils: Optional[Callable[[RangeTCT], dict]] = None) -> list[RangeTCT]:
    """Tous les ranges successifs de la série (le dernier peut être encore vivant).

    `seuils(range)` (facultatif) renvoie {"haut": Seuil, "bas": Seuil} pour un range ; sans lui, une
    touche n'a lieu qu'au haut ou au bas du range."""
    o, h, l, c = (df[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    n = len(c)
    if pivots is None:
        pivots = zigzag(h, l, c, o)
    ranges: list[RangeTCT] = []
    p_idx = 0
    dernier: Optional[Pivot] = None
    cand: Optional[tuple[Pivot, Pivot]] = None
    scan = 0
    milieu_j: Optional[int] = None
    perime = False
    mort_a = -1
    i = 0
    while i < n:
        while p_idx < len(pivots) and pivots[p_idx].confirme <= i:
            p = pivots[p_idx]
            p_idx += 1
            precedent, dernier = dernier, p
            if p.confirme <= mort_a or precedent is None:
                continue
            nouveau = None
            if cand is None or perime:
                # pas de mouvement en attente, ou son extrême a été dépassé sans nouveau pivot plus loin :
                # la dernière jambe du Zig Zag devient le mouvement en attente
                nouveau = (precedent, p)
            else:
                a, b = cand
                if p.type == b.type and _au_dela(p, b):
                    nouveau = (precedent, p)      # nouvel extrême : la dernière jambe devient l'impulsion
                elif p.type == a.type and _au_dela(p, a):
                    nouveau = (b, p)              # impulsion inverse
            if nouveau is not None:
                cand = nouveau
                scan, milieu_j, perime = cand[1].barre + 1, None, False

        if cand is not None and milieu_j is None and not perime:
            a, b = cand
            mid = (a.prix + b.prix) / 2
            while scan <= i:
                k = scan
                scan += 1
                if (b.type == "haut" and h[k] > b.prix) or (b.type == "bas" and l[k] < b.prix):
                    perime = True                 # l'extrême est dépassé : on attend le pivot suivant
                    break
                if (b.type == "haut" and l[k] <= mid) or (b.type == "bas" and h[k] >= mid):
                    milieu_j = k
                    break

        if cand is not None and milieu_j is not None and i >= cand[1].confirme:
            a, b = cand
            mid = (a.prix + b.prix) / 2
            croise = a.barre + 1
            while croise < b.barre and not ((a.type == "bas" and h[croise] >= mid) or
                                            (a.type == "haut" and l[croise] <= mid)):
                croise += 1
            haut0, bas0 = max(a.prix, b.prix), min(a.prix, b.prix)
            rng = RangeTCT(a=a, b=b, milieu_atteint=milieu_j, validation=i, croisement_a=croise,
                           haut0=haut0, bas0=bas0)
            _vie(rng, o, h, l, c, None)
            if seuils is not None and (rng.mort is None or rng.mort > rng.validation):
                s = seuils(rng)                   # None : range sans intérêt (mort depuis longtemps)
                if s:
                    _vie(rng, o, h, l, c, s)
            cand, milieu_j = None, None
            if rng.mort is not None and rng.mort <= rng.validation:
                # mort avant même d'être visible (pivot validé tard) : pas de range
                mort_a = rng.mort
                i = rng.mort + 1
                continue
            ranges.append(rng)
            if rng.mort is None:
                break
            mort_a = rng.mort
            i = rng.mort + 1
            continue
        i += 1
    return ranges


def _vie(rng: RangeTCT, o, h, l, c, seuils: Optional[dict]) -> None:
    """Rejoue le range bougie par bougie depuis le retour au milieu : décalages, mort, touches, alertes."""
    haut, bas = rng.haut0, rng.bas0
    cotes = {k: Cote(seuil=(seuils or {}).get(k)) for k in COTES}
    for k in COTES:
        cotes[k].barres_touches.append(rng.cote_extreme(k).barre)
    ev = [{"type": "validation", "barre": rng.validation}]
    mort = sens = None
    # Limites -0,3 / 1,3 : celles du range tel qu'il était avant la sortie en cours. Pendant une sortie
    # au-delà du haut (ou du bas), le haut suit les mèches mais la limite ne s'éloigne pas avec lui,
    # sinon une tendance qui monte bougie après bougie ne tuerait jamais le range. Elle est recalculée
    # sur le nouveau haut au retour au milieu.
    lim_haut = haut + EXTENSION * (haut - bas)
    lim_bas = bas - EXTENSION * (haut - bas)
    for i in range(rng.milieu_atteint + 1, len(c)):
        if c[i] > lim_haut or c[i] < lim_bas:
            mort, sens = i, ("haut" if c[i] > lim_haut else "bas")
            ev.append({"type": "mort", "barre": i, "cote": sens})
            break
        if h[i] > haut:
            haut = h[i]
            ev.append({"type": "decalage", "barre": i, "cote": "haut", "prix": haut})
        if l[i] < bas:
            bas = l[i]
            ev.append({"type": "decalage", "barre": i, "cote": "bas", "prix": bas})
        mid = (haut + bas) / 2
        if l[i] <= mid:                     # pas (ou plus) de sortie par le haut
            lim_haut = haut + EXTENSION * (haut - bas)
        if h[i] >= mid:                     # pas (ou plus) de sortie par le bas
            lim_bas = bas - EXTENSION * (haut - bas)
        for k in COTES:
            s = cotes[k]
            extreme = haut if k == "haut" else bas
            if s.arme:
                cible = s.seuil.prix(k, extreme, mid) if s.seuil else extreme
                if (k == "haut" and h[i] >= cible) or (k == "bas" and l[i] <= cible):
                    s.touches += 1
                    s.arme = False
                    s.barres_touches.append(i)
                    ev.append({"type": "touche", "barre": i, "cote": k, "n": s.touches})
            elif (k == "haut" and l[i] <= mid) or (k == "bas" and h[i] >= mid):
                s.arme = True
                ev.append({"type": "retour_milieu", "barre": i, "cote": k, "n": s.touches})
                if s.touches == TOUCHES_ALERTE and s.alerte is None:
                    s.alerte = i
                    ev.append({"type": "alerte", "barre": i, "cote": k})
    rng.haut, rng.bas, rng.mort, rng.sens_mort = haut, bas, mort, sens
    rng.cotes, rng.evenements = cotes, ev


# =============================================================================
# Zones d'offre / de demande et niveaux de liquidité (cascade d'unités de temps)
# =============================================================================

def _position_extreme(df: pd.DataFrame, debut: pd.Timestamp, fin: pd.Timestamp, cote: str) -> Optional[int]:
    """Position (dans df) de la bougie la plus haute (ou la plus basse) parmi celles qui commencent
    dans [debut, fin) ; en cas d'égalité, la première."""
    masque = ((df["start"] >= debut) & (df["start"] < fin)).to_numpy()
    if not masque.any():
        return None
    pos = np.flatnonzero(masque)
    valeurs = df["high"].to_numpy(float)[pos] if cote == "haut" else df["low"].to_numpy(float)[pos]
    k = int(np.argmax(valeurs)) if cote == "haut" else int(np.argmin(valeurs))
    return int(pos[k])


def zone_offre_demande(barres: SourceBarres, cascade: list[str], cote: str, debut: pd.Timestamp,
                       fin: pd.Timestamp, meche_min: float = MECHE_MIN, marge: pd.Timedelta = pd.Timedelta(days=10)
                       ) -> Optional[Zone]:
    """Zone d'offre (cote="haut") ou de demande (cote="bas") de la touche située dans la bougie
    [debut, fin) de l'unité de temps du range ; on descend d'unité de temps tant qu'on ne trouve pas."""
    for ut in cascade:
        df = barres(ut, debut - marge, fin)
        if df is None or df.empty:
            continue
        df = df.reset_index(drop=True)
        k = _position_extreme(df, debut, fin, cote)
        if k is None:
            continue
        for p in (k, k - 1):                      # la bougie de l'extrême, ou celle juste avant
            if p < 0:
                break
            o, hh, ll, cc = (float(df.at[p, x]) for x in ("open", "high", "low", "close"))
            taille = hh - ll
            if cote == "haut" and cc > o:          # dernière bougie verte avant la baisse
                if taille > 0 and (hh - cc) / taille >= meche_min:
                    return Zone(ut, bas=cc, haut=hh, debut=df.at[p, "start"])
                break
            if cote == "bas" and cc < o:           # dernière bougie rouge avant la hausse
                if taille > 0 and (cc - ll) / taille >= meche_min:
                    return Zone(ut, bas=ll, haut=cc, debut=df.at[p, "start"])
                break
    return None


def niveau_liquidite(barres: SourceBarres, cascade: list[str], cote: str, debut: pd.Timestamp,
                     fin: pd.Timestamp, fin_jambe: pd.Timestamp, milieu: float, limite: float,
                     contexte: int = 30) -> Optional[Liquidite]:
    """Premier sommet intermédiaire (cote="haut") ou premier creux intermédiaire (cote="bas") de la
    jambe qui part de la touche (bougie [debut, fin) de l'unité de temps du range) jusqu'au retour
    au milieu (au plus tard dans la bougie qui finit à `fin_jambe`). Il doit être au-delà de `limite`
    (0,25 ou 0,75), sinon on descend d'unité de temps."""
    for ut in cascade:
        df = barres(ut, debut - pd.Timedelta(days=10), fin_jambe)
        if df is None or df.empty:
            continue
        df = df.reset_index(drop=True)
        k = _position_extreme(df, debut, fin, cote)
        if k is None:
            continue
        hi, lo, cl = (df[x].to_numpy(float) for x in ("high", "low", "close"))
        e = k + 1
        while e < len(df) and not ((cote == "haut" and lo[e] <= milieu) or (cote == "bas" and hi[e] >= milieu)):
            e += 1
        e = min(e, len(df) - 1)
        d = max(0, k - contexte)
        piv = zigzag(hi[d:e + 1], lo[d:e + 1], cl[d:e + 1], df["open"].to_numpy(float)[d:e + 1])
        apres = [p for p in piv if p.barre + d > k]
        oppose = "bas" if cote == "haut" else "haut"
        vu_oppose = False
        trouve = None
        for p in apres:
            if p.type == oppose:
                vu_oppose = True
            elif vu_oppose and p.type == cote:
                trouve = p
                break
        if trouve is None:
            continue                               # jambe droite : unité de temps inférieure
        if (cote == "haut" and trouve.prix >= limite) or (cote == "bas" and trouve.prix <= limite):
            return Liquidite(ut, prix=trouve.prix, debut=df.at[trouve.barre + d, "start"])
    return None


def seuils_du_range(rng: RangeTCT, df: pd.DataFrame, ut: str, barres: SourceBarres,
                    meche_min: float = MECHE_MIN) -> dict:
    """{"haut": Seuil, "bas": Seuil} : zone et liquidité de la 1re touche de chaque côté."""
    cascade = CASCADES[ut]
    niv = niveaux(rng.haut0, rng.bas0)
    out = {}
    for cote in COTES:
        barre_touche, barre_milieu = rng.jambe(cote)
        debut, fin = df.at[barre_touche, "start"], df.at[barre_touche, "end"]
        fin_jambe = df.at[min(barre_milieu, len(df) - 1), "end"]
        zone = zone_offre_demande(barres, cascade, cote, debut, fin, meche_min)
        limite = niv["0.25"] if cote == "haut" else niv["0.75"]
        liq = niveau_liquidite(barres, cascade, cote, debut, fin, fin_jambe, niv["0.5"], limite)
        out[cote] = Seuil(zone=zone, liquidite=liq)
    return out
