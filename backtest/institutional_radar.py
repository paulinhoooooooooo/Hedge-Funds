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

Autres indices, gratuits, qui s'ajoutent au score (RadarExtras) :
  * divergence volume / prix sur un mois (le volume des hausses domine sans que le prix avance) ;
  * solde des jours d'accumulation et de distribution sur 5 semaines ;
  * force relative sur 3 mois face au groupe de l'action, avec un volume en hausse ;
  * bourses privées (FINRA, hebdomadaire, publié 2 à 3 semaines après) : part du volume, blocs
    des plateformes institutionnelles (Liquidnet, BIDS, Luminex…), activité des plateformes des
    banques (UBS, JPMorgan, Morgan Stanley, Goldman Sachs…) ;
  * positions vendeuses déclarées (FINRA, deux fois par mois) ;
  * achats des dirigeants sur le marché (Form 4) et franchissements de 5 % du capital (13D/13G) ;
  * filtre des jours d'événement (résultats, échéances d'options, rééquilibrages d'indices) : le
    volume de ces séances est mécanique et n'est pas compté.

Score = somme pondérée des unités de temps en accumulation (+) ou distribution (-) :
heure 0,5 ; jour 1 ; semaine 1,5 ; mois 2 ; plus 0,5 ou 1 par indice complémentaire.
Alerte à partir de ±2,5, c'est-à-dire au moins deux traces concordantes. Limites : résultats d'entreprise, échéances d'options et
rééquilibrages d'indices créent aussi des volumes anormaux ; la part hors bourse inclut les
ordres des particuliers internalisés par les courtiers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

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


# Plateformes privées (ATS) : code FINRA (MPID) -> gestionnaire, vérifié sur les données FINRA.
# Les banques y exécutent les ordres de leurs clients institutionnels ; les plateformes de blocs
# n'acceptent que de gros ordres (plusieurs milliers d'actions par échange en moyenne).
BANK_VENUES = {
    "UBSA": "UBS", "JPMX": "JPMorgan", "JPBX": "JPMorgan", "MSPL": "Morgan Stanley", "MSRP": "Morgan Stanley",
    "MSTX": "Morgan Stanley", "SGMT": "Goldman Sachs", "LATS": "Barclays", "ONEC": "Citi", "BNPX": "BNP Paribas",
    "MLIX": "Bank of America", "ICBX": "Nomura (Instinet)", "STFX": "Stifel",
}
BLOCK_VENUES = {"LQNA": "Liquidnet", "LQNT": "Liquidnet", "BIDS": "BIDS", "BLKX": "Instinet BlockCross",
                "LMNX": "Luminex"}


@dataclass
class RadarExtras:
    """Données complémentaires, toutes optionnelles, en format long avec leur date de publication.

    ats            asset, week_start, published, ats_volume, block_volume, bank_volume, banks
                   (FINRA, volumes hebdomadaires des bourses privées ; banks = plateformes bancaires
                   les plus actives, en texte)
    short_interest asset, settlement, available, short_qty (FINRA, positions vendeuses déclarées)
    insiders       asset, filing_date, owner, role, value (Form 4, achats sur le marché, code P)
    filings_5pct   asset, filing_date, form, filer (13D / 13G initiaux, hors amendements)
    earnings       asset, date (publication des résultats : 8-K, rubrique 2.02)
    groups         actif -> groupe (secteur) pour la force relative
    """

    ats: Optional[pd.DataFrame] = None
    short_interest: Optional[pd.DataFrame] = None
    insiders: Optional[pd.DataFrame] = None
    filings_5pct: Optional[pd.DataFrame] = None
    earnings: Optional[pd.DataFrame] = None
    groups: Optional[dict] = None

    TABLES = ("ats", "short_interest", "insiders", "filings_5pct", "earnings")
    DATE_COLUMNS = {"ats": ("week_start", "published"), "short_interest": ("settlement", "available"),
                    "insiders": ("filing_date",), "filings_5pct": ("filing_date",), "earnings": ("date",)}

    def is_empty(self) -> bool:
        return all(getattr(self, name) is None or len(getattr(self, name)) == 0 for name in self.TABLES)


@dataclass
class Radar:
    views: dict[str, TimeframeView]
    score: pd.DataFrame
    hourly: dict[str, dict] = field(default_factory=dict)  # actif -> dernière heure analysée
    evidence: dict[str, pd.DataFrame] = field(default_factory=dict)  # famille -> points [date x actif]
    notes: dict[str, Callable] = field(default_factory=dict)  # famille -> texte(actif, i)
    events: Optional[pd.DataFrame] = None  # jours d'événement (bool) [date x actif]

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
        if self.events is not None and bool(self.events[asset].iloc[i]):
            parts.append("Jour d'événement (résultats, échéance d'options ou rééquilibrage d'indices) : "
                         "le volume du jour est mécanique et n'est pas compté.")
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
        for family, points in self.evidence.items():
            value = points[asset].iloc[i]
            if np.isfinite(value) and value != 0 and family in self.notes:
                parts.append(self.notes[family](asset, i))
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


def _fmt(x: float, digits: int = 0) -> str:
    return f"{x:+.{digits}%}"


def event_days(index: pd.DatetimeIndex, columns, earnings: Optional[pd.DataFrame] = None) -> pd.DataFrame:
    """Séances au volume mécanique : échéances mensuelles d'options (3e vendredi), échéances
    trimestrielles et rééquilibrages d'indices (3e vendredi de mars, juin, septembre, décembre),
    reconstitution Russell (dernier vendredi de juin), et la veille, le jour et le lendemain des
    résultats de l'entreprise."""
    idx = pd.DatetimeIndex(index)
    third_friday = (idx.weekday == 4) & (idx.day >= 15) & (idx.day <= 21)
    russell = (idx.month == 6) & (idx.weekday == 4) & (idx.day >= 24)
    market = pd.Series(third_friday | russell, index=idx)
    out = pd.DataFrame(np.repeat(market.to_numpy()[:, None], len(columns), axis=1), index=idx, columns=columns)
    if earnings is not None and len(earnings):
        for asset, grp in earnings.groupby("asset"):
            if asset not in out.columns:
                continue
            pos = idx.searchsorted(pd.to_datetime(grp["date"]).to_numpy())
            for k in (-1, 0, 1):
                p = np.clip(pos + k, 0, len(idx) - 1)
                out.iloc[p, out.columns.get_loc(asset)] = True
    return out


def _as_of_daily(long: pd.DataFrame, value_cols: list[str], date_col: str, index, columns,
                 max_age_days: int) -> dict[str, pd.DataFrame]:
    """Reporte des données publiées à une date (format long) sur le calendrier quotidien,
    à partir de la séance qui suit la publication, et les oublie après `max_age_days`."""
    idx = pd.DatetimeIndex(index)
    out = {c: pd.DataFrame(np.nan, index=idx, columns=columns) for c in value_cols + ["_age"]}
    for asset, grp in long.sort_values(date_col).groupby("asset"):
        if asset not in columns:
            continue
        j = columns.get_loc(asset)
        pos = idx.searchsorted(pd.to_datetime(grp[date_col]).to_numpy(), side="right")  # séance suivante
        ok = pos < len(idx)
        for c in value_cols:
            out[c].iloc[pos[ok], j] = grp[c].to_numpy()[ok]
        out["_age"].iloc[pos[ok], j] = idx[pos[ok]].to_numpy().astype("datetime64[ns]").astype("int64")
    age_days = (idx.to_numpy().astype("datetime64[ns]").astype("int64")[:, None]
                - out["_age"].ffill().to_numpy()) / 86_400e9
    fresh = age_days <= max_age_days
    return {c: out[c].ffill().where(fresh) for c in value_cols}


def _points(index, columns) -> pd.DataFrame:
    return pd.DataFrame(0.0, index=index, columns=columns)


def _divergence(close: pd.DataFrame, volume: pd.DataFrame, ret: pd.DataFrame):
    """Sur un mois, le volume des hausses domine alors que le prix n'avance pas (ou l'inverse)."""
    signed = (np.sign(ret) * volume).rolling(21, min_periods=15).sum()
    balance = signed / volume.rolling(21, min_periods=15).sum()
    move = close / close.shift(21) - 1.0
    pts = _points(close.index, close.columns).mask((balance >= 0.15) & (move <= 0), 0.5)
    pts = pts.mask((balance <= -0.15) & (move >= 0), -0.5)

    def note(a, i):
        b, m = balance[a].iloc[i], move[a].iloc[i]
        return (f"Divergence : sur un mois, le volume des {'hausses' if b > 0 else 'baisses'} domine ({b:+.2f}) "
                f"alors que le prix {'recule ou stagne' if b > 0 else 'monte'} ({_fmt(m, 1)}) — "
                f"{'achats cachés' if b > 0 else 'ventes cachées'}.")
    return pts, note


def _acc_dist_days(close: pd.DataFrame, volume: pd.DataFrame, ret: pd.DataFrame, events: pd.DataFrame):
    """Solde des jours d'accumulation et de distribution sur 5 semaines (hors jours d'événement)."""
    avg = volume.rolling(50, min_periods=25).mean().shift(1)
    heavy = (volume >= 1.2 * avg) & ~events
    acc = ((ret >= 0.002) & heavy).astype(float).rolling(25, min_periods=25).sum()
    dist = ((ret <= -0.002) & heavy).astype(float).rolling(25, min_periods=25).sum()
    net = acc - dist
    pts = _points(close.index, close.columns).mask(net >= 5, 0.5).mask(net <= -5, -0.5)

    def note(a, i):
        return (f"{int(acc[a].iloc[i])} jours d'accumulation contre {int(dist[a].iloc[i])} de distribution "
                f"sur 5 semaines (hausses ou baisses sur fort volume).")
    return pts, note


def _relative_strength(close: pd.DataFrame, month_volume_ratio: pd.DataFrame, groups: Optional[dict]):
    """Surperformance sur 3 mois face au groupe de l'action, avec un volume en hausse."""
    groups = groups or {}
    members: dict[str, list[str]] = {}
    for a in close.columns:
        members.setdefault(groups.get(a, "_"), []).append(a)
    r63 = close / close.shift(63) - 1.0
    excess = pd.DataFrame(np.nan, index=close.index, columns=close.columns)
    for names in members.values():
        if len(names) >= 3:
            excess[names] = r63[names].sub(r63[names].mean(axis=1), axis=0)
    vol_up = month_volume_ratio >= 1.1
    pts = _points(close.index, close.columns).mask((excess >= 0.10) & vol_up, 0.5)
    pts = pts.mask((excess <= -0.10) & vol_up, -0.5)

    def note(a, i):
        e = excess[a].iloc[i]
        return (f"Force relative : {_fmt(e, 1)} sur 3 mois face à son secteur, avec un volume en hausse "
                f"({'soutien' if e > 0 else 'désengagement'} probable des institutions).")
    return pts, note


def _private_venues(ats: pd.DataFrame, close: pd.DataFrame, volume: pd.DataFrame, ret: pd.DataFrame):
    """Bourses privées (FINRA, hebdomadaire) : part du volume, blocs et plateformes des banques.

    Une semaine est « chaude » si la part des bourses privées dépasse de 5 points son habitude, ou si
    les blocs ou les plateformes bancaires traitent au moins deux fois leur volume habituel. Le sens
    (achat ou vente) est celui de la semaine en bourse (volume des hausses moins volume des baisses).
    Utilisable seulement à partir de la séance qui suit la publication FINRA.
    """
    idx, cols = close.index, close.columns
    weekly = lambda f: f.resample("W-MON", label="left", closed="left").sum(min_count=1)
    week_vol = weekly(volume).stack().rename("total")
    week_side = np.sign(weekly(np.sign(ret.fillna(0.0)) * volume)).stack().rename("side")
    w = ats[ats["asset"].isin(cols)].copy()
    if w.empty:
        return None
    w["week_start"] = pd.to_datetime(w["week_start"]).astype("datetime64[ns]")
    w = w.join(week_vol, on=["week_start", "asset"]).join(week_side, on=["week_start", "asset"])
    w = w.dropna(subset=["total"]).sort_values(["asset", "week_start"])
    if w.empty:
        return None
    w["share"] = w["ats_volume"] / w["total"].where(w["total"] > 0)
    g = w.groupby("asset")
    habit = lambda col: g[col].transform(lambda x: x.shift(1).rolling(26, min_periods=8).median())
    w["share_normal"] = habit("share")
    w["block_ratio"] = w["block_volume"] / habit("block_volume").where(lambda x: x > 0)
    w["bank_ratio"] = w["bank_volume"] / habit("bank_volume").where(lambda x: x > 0)
    hot = (w["share"] - w["share_normal"] >= 0.05) | (w["block_ratio"] >= 2.0) | (w["bank_ratio"] >= 2.0)
    w["points"] = np.where(hot, 0.5 * w["side"].fillna(0.0), 0.0)
    w["week_ord"] = w["week_start"].astype("int64")
    daily = _as_of_daily(w, ["points", "share", "share_normal", "block_ratio", "bank_ratio", "week_ord"],
                         "published", idx, cols, max_age_days=21)
    banks_by = w.set_index(["asset", "week_ord"])["banks"].to_dict() if "banks" in w.columns else {}

    def note(a, i):
        week = int(daily["week_ord"][a].iloc[i])
        side = "hausses" if daily["points"][a].iloc[i] > 0 else "baisses"
        txt = (f"Bourses privées (semaine du {pd.Timestamp(week):%d/%m}, chiffres FINRA) : "
               f"{daily['share'][a].iloc[i]:.0%} du volume (habituellement {daily['share_normal'][a].iloc[i]:.0%})")
        for key, label in (("block_ratio", "plateformes de blocs"), ("bank_ratio", "plateformes des banques")):
            r = daily[key][a].iloc[i]
            if np.isfinite(r) and r >= 1.5:
                txt += f", {label} ×{r:.1f} leur volume habituel"
        banks = banks_by.get((a, week), "")
        if isinstance(banks, str) and banks:
            txt += f" ; banques les plus actives : {banks}"
        return txt + f" ; semaine dominée par les {side}."
    return daily["points"].fillna(0.0), note


def _short_interest(si: pd.DataFrame, idx, cols):
    """Positions vendeuses déclarées (FINRA, deux fois par mois) : fortes baisses = rachats des vendeurs."""
    si = si[si["asset"].isin(cols)].sort_values(["asset", "settlement"]).copy()
    if si.empty:
        return None
    si["change"] = si.groupby("asset")["short_qty"].pct_change()
    si["points"] = np.where(si["change"] <= -0.15, 0.5, np.where(si["change"] >= 0.15, -0.5, 0.0))
    si["settle_ord"] = pd.to_datetime(si["settlement"]).astype("datetime64[ns]").astype("int64")
    daily = _as_of_daily(si, ["points", "change", "settle_ord"], "available", idx, cols, max_age_days=20)

    def note(a, i):
        c = daily["change"][a].iloc[i]
        return (f"Positions vendeuses déclarées : {_fmt(c)} depuis le rapport précédent (arrêté au "
                f"{pd.Timestamp(int(daily['settle_ord'][a].iloc[i])):%d/%m}) — "
                f"{'les vendeurs à découvert se retirent' if c < 0 else 'les paris à la baisse augmentent'}.")
    return daily["points"].fillna(0.0), note


def _window_hits(dates: np.ndarray, idx: pd.DatetimeIndex, days: int) -> tuple[np.ndarray, np.ndarray]:
    """Séances [début, fin) pendant lesquelles un dépôt daté `dates` est connu et récent : de la séance
    qui suit le dépôt jusqu'à `days` jours calendaires après."""
    d = pd.to_datetime(dates).to_numpy().astype("datetime64[ns]")
    return idx.searchsorted(d, side="right"), idx.searchsorted(d + np.timedelta64(days, "D"), side="right")


def _insiders(ins: pd.DataFrame, idx: pd.DatetimeIndex, cols, days: int = 90):
    """Achats des dirigeants sur le marché (Form 4, code P) sur 3 mois : +1 si au moins deux acheteurs
    différents ou 1 M$ achetés."""
    ins = ins[ins["asset"].isin(cols)].copy()
    if ins.empty:
        return None
    ins["filing_date"] = pd.to_datetime(ins["filing_date"]).astype("datetime64[ns]")
    n = len(idx)
    n_buyers = np.zeros((n, len(cols)))
    value = np.zeros((n, len(cols)))
    by_asset = {}
    for asset, grp in ins.groupby("asset"):
        j = cols.get_loc(asset)
        by_asset[asset] = grp
        start, end = _window_hits(grp["filing_date"], idx, days)
        diff = np.zeros(n + 1)
        np.add.at(diff, start, grp["value"].to_numpy(dtype=float))
        np.add.at(diff, end, -grp["value"].to_numpy(dtype=float))
        value[:, j] = np.cumsum(diff)[:-1]
        for _, og in grp.groupby("owner"):
            s_o, e_o = _window_hits(og["filing_date"], idx, days)
            d = np.zeros(n + 1)
            np.add.at(d, s_o, 1)
            np.add.at(d, e_o, -1)
            n_buyers[:, j] += np.cumsum(d)[:-1] > 0
    n_buyers = pd.DataFrame(n_buyers, index=idx, columns=cols)
    value = pd.DataFrame(value, index=idx, columns=cols)
    pts = _points(idx, cols).mask((n_buyers >= 2) | (value >= 1e6), 1.0)

    def note(a, i):
        day = idx[i]
        g = by_asset[a]
        recent = g[(g["filing_date"] < day) & (g["filing_date"] >= day - pd.Timedelta(days=days))]
        top = recent.groupby("owner")["value"].sum().sort_values(ascending=False).head(3)
        roles = recent.drop_duplicates("owner").set_index("owner").get("role")
        who = ", ".join(f"{o}{f' ({roles[o]})' if roles is not None and isinstance(roles.get(o), str) else ''}"
                        for o in top.index)
        return (f"Achats des dirigeants : {int(n_buyers[a].iloc[i])} dirigeant(s) ou gros actionnaire(s) ont "
                f"acheté sur le marché pour {value[a].iloc[i] / 1e6:,.1f} M$ sur 3 mois (Form 4) — {who}.")
    return pts, note


def _five_percent(f5: pd.DataFrame, idx: pd.DatetimeIndex, cols, days: int = 60):
    """Franchissement de 5 % du capital (13D : investisseur qui veut peser ; 13G : investisseur passif)."""
    f5 = f5[f5["asset"].isin(cols) & ~f5["form"].astype(str).str.endswith("/A")].copy()
    if f5.empty:
        return None
    f5["filing_date"] = pd.to_datetime(f5["filing_date"]).astype("datetime64[ns]")
    # Vague administrative (réorganisation d'un gérant qui redépose sur des dizaines de sociétés) :
    # un déclarant avec au moins 5 dépôts sur les 30 derniers jours n'est pas un signal.
    f5 = f5.sort_values("filing_date")
    filer = f5["filer"].fillna("").astype(str) if "filer" in f5.columns else pd.Series("", index=f5.index)
    wave = pd.Series(0, index=f5.index)
    for name, grp in f5[filer != ""].groupby(filer[filer != ""]):
        d = grp["filing_date"].to_numpy()
        wave.loc[grp.index] = np.searchsorted(d, d, side="right") - np.searchsorted(d, d - np.timedelta64(30, "D"))
    f5 = f5[wave < 5]
    if f5.empty:
        return None
    f5["weight"] = np.where(f5["form"].str.contains("13D"), 1.0, 0.5)
    pts = np.zeros((len(idx), len(cols)))
    by_asset = {}
    for asset, grp in f5.groupby("asset"):
        j = cols.get_loc(asset)
        by_asset[asset] = grp
        start, end = _window_hits(grp["filing_date"], idx, days)
        for s_, e_, wgt in zip(start, end, grp["weight"]):
            pts[s_:e_, j] = np.maximum(pts[s_:e_, j], wgt)
    pts = pd.DataFrame(pts, index=idx, columns=cols)

    def note(a, i):
        day = idx[i]
        g = by_asset[a]
        recent = g[(g["filing_date"] < day) & (g["filing_date"] >= day - pd.Timedelta(days=days))]
        parts = []
        for r in recent.sort_values("filing_date").itertuples():
            who = str(getattr(r, "filer", "") or "").strip() or "un investisseur"
            kind = "avec l'intention de peser sur l'entreprise (13D)" if "13D" in r.form else "(13G, investisseur passif)"
            parts.append(f"{who} a déclaré plus de 5 % du capital le {r.filing_date:%d/%m/%Y} {kind}")
        return "Franchissement de seuil : " + " ; ".join(parts) + "."
    return pts, note


def _evidence(radar: "Radar", close: pd.DataFrame, volume: pd.DataFrame, extras: RadarExtras) -> None:
    """Ajoute au radar les indices complémentaires (points) et leur explication."""
    idx, cols = close.index, close.columns
    ret = close / close.shift(1) - 1.0
    families = {
        "divergence": _divergence(close, volume, ret),
        "jours": _acc_dist_days(close, volume, ret, radar.events),
        "force": _relative_strength(close, radar.views["mois"].volume_ratio, extras.groups),
    }
    if extras.ats is not None and len(extras.ats):
        families["bourses_privees"] = _private_venues(extras.ats, close, volume, ret)
    if extras.short_interest is not None and len(extras.short_interest):
        families["ventes_a_decouvert"] = _short_interest(extras.short_interest, idx, cols)
    if extras.insiders is not None and len(extras.insiders):
        families["dirigeants"] = _insiders(extras.insiders, idx, cols)
    if extras.filings_5pct is not None and len(extras.filings_5pct):
        families["cinq_pourcent"] = _five_percent(extras.filings_5pct, idx, cols)
    for name, found in families.items():
        if found is not None:
            radar.evidence[name], radar.notes[name] = found


def compute_radar(high: pd.DataFrame, low: pd.DataFrame, close: pd.DataFrame, volume: pd.DataFrame,
                  offx_total: Optional[pd.DataFrame] = None, offx_short: Optional[pd.DataFrame] = None,
                  hourly: Optional[dict[str, pd.DataFrame]] = None, extras: Optional[RadarExtras] = None) -> Radar:
    """Radar complet. Les données hors bourse du jour J (publiées le soir) sont décalées d'une séance ;
    les jours d'événement ne comptent pas dans l'unité de temps « jour »."""
    volume = volume.where(volume > 0)
    extras = extras or RadarExtras()
    if offx_total is not None:
        offx_total = offx_total.reindex_like(close).shift(1)
        offx_short = offx_short.reindex_like(close).shift(1) if offx_short is not None else None
    events = event_days(close.index, close.columns, extras.earnings)
    views = {}
    score = pd.DataFrame(0.0, index=close.index, columns=close.columns)
    for name, (window, baseline, threshold, weight) in TIMEFRAMES.items():
        view = _window_view(high, low, close, volume, window, baseline, threshold,
                            allow_stealth=window > 1, offx_total=offx_total, offx_short=offx_short)
        if name == "jour":
            view.state = view.state.mask(events, 0.0)
        views[name] = view
        score = score + weight * view.state.fillna(0.0)
    radar = Radar(views=views, score=score, events=events)
    _evidence(radar, close, volume, extras)
    for points in radar.evidence.values():
        radar.score = radar.score + points.reindex_like(score).fillna(0.0)
    score = radar.score
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
