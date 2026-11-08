# -*- coding: utf-8 -*-
"""
scanner_ranges.py — Scanner des modèles d'accumulation / distribution (ranges du fondateur)
==========================================================================================

Chaque nuit, pour le S&P 500 et les 20 premières cryptos, en hebdomadaire (W), journalier (D) et
4 heures (4h) :
  1. trace le Zig Zag « 2 bougies » (zigzag.py) ;
  2. repère le range en cours et le suit (modeles_range.py) : validation au 0,5, décalages, mort ;
  3. cherche la zone d'offre / de demande et le niveau de liquidité de chaque côté, du W au 5 min ;
  4. compte les touches et lève une ALERTE quand un côté a fait sa 2e touche suivie d'un retour au
     milieu (sans retour au milieu : pas d'alerte) ;
  5. écrit le rapport (Markdown + CSV) et envoie les nouvelles alertes sur le téléphone (ntfy).

Données : Alpaca (clés ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY, déjà utilisées par le fonds).
Actions : cours ajustés des dividendes et des divisions, séance de New York seulement (comme le
graphique TradingView du fondateur). Crypto : paires en dollars, journées et semaines en UTC.
Seules les bougies terminées sont utilisées.

    python backtest/scanner_ranges.py                              # tout l'univers
    python backtest/scanner_ranges.py --symboles JNJ,AAPL,BTC/USD  # quelques actifs
    python backtest/scanner_ranges.py --etat etat_ranges.json --notifier
"""

from __future__ import annotations

import argparse
import io
import json
import math
import os
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Iterable, Optional

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import modeles_range as mr  # noqa: E402
import notifier  # noqa: E402
import phase1_data as p1  # noqa: E402

NY = "America/New_York"
OUVERTURE = 9 * 60 + 30   # 9 h 30, heure de New York
FERMETURE = 16 * 60
UTS = ("W", "D", "4h")
MINUTES = {"4h": 240, "1h": 60, "30m": 30, "15m": 15, "5m": 5}
DEBUT_JOUR_ACTIONS = "2016-01-01"
DEBUT_JOUR_CRYPTO = "2021-01-01"
HISTO_4H_JOURS = 200       # profondeur de l'historique 4 heures (≈ 270 bougies 4h pour une action)
HISTO_4H_CRYPTO = 365      # la crypto tient en une seule requête groupée
TRAVAILLEURS = 4           # téléchargements en parallèle (débit Alpaca partagé : 200 requêtes / min)
MAX_FENETRE_5M = 400       # jours : au-delà, pas de recherche en 5 min (fenêtre trop longue)
JOURS_ALERTE = 5           # une alerte plus ancienne n'est plus envoyée
GARDE_ALERTES = 90         # jours pendant lesquels on se souvient d'une alerte envoyée

ALPACA_CRYPTO = "https://data.alpaca.markets/v1beta3/crypto/us/bars"
ALPACA_ASSETS = "https://paper-api.alpaca.markets/v2/assets"
SP500_CSV = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"
COINGECKO = "https://api.coingecko.com/api/v3/coins/markets"
NB_CRYPTOS = 20
STABLES = {"USDT", "USDC", "USDG", "USDS", "DAI", "USDE", "PYUSD", "FDUSD", "TUSD", "USD1", "RLUSD", "USDD",
           "USDY", "USDF", "PAXG", "XAUT", "BUIDL", "USYC", "EURC", "WBTC", "STETH", "WETH"}
# Si CoinGecko ne répond pas : capitalisations d'octobre 2026, paires disponibles chez Alpaca
CRYPTOS_SECOURS = ["BTC", "ETH", "XRP", "SOL", "HYPE", "DOGE", "LINK", "ADA", "BCH", "UNI", "LTC", "AVAX",
                   "SHIB", "PEPE", "AAVE", "ONDO", "DOT", "SKY", "RENDER", "ARB", "FIL", "POL", "TRUMP"]
SP500_SECOURS = HERE / "donnees" / "sp500.txt"

OUT = p1.ROOT / "outputs" / "ranges"
AVERTISSEMENT = ("Repérage automatique des règles du fondateur, à vérifier sur le graphique : "
                 "ce n'est pas un conseil d'investissement.")


# =============================================================================
# Univers
# =============================================================================

def univers_actions(http: Optional[p1.Http] = None) -> list[str]:
    """Composition actuelle du S&P 500 (symboles Alpaca, ex. BRK.B) ; liste rangée dans le dépôt en secours."""
    http = http or p1.Http(headers={"User-Agent": "FlowFund-Research/1.0"}, retries=2)
    try:
        df = pd.read_csv(io.BytesIO(http.get(SP500_CSV)))
        symboles = sorted({str(s).strip().upper().replace("-", ".") for s in df["Symbol"].dropna()})
        if len(symboles) >= 450:
            return symboles
    except Exception as err:  # noqa: BLE001
        print(f"S&P 500 : liste en ligne indisponible ({err}), liste du dépôt utilisée.", file=sys.stderr)
    return [s.strip() for s in SP500_SECOURS.read_text().split() if s.strip()]


def univers_crypto(http: Optional[p1.Http] = None, n: int = NB_CRYPTOS) -> list[str]:
    """Les n premières cryptos par capitalisation (hors monnaies stables), parmi les paires en dollars
    d'Alpaca. Renvoie des symboles « BTC/USD »."""
    http = http or p1.Http(headers={"User-Agent": "FlowFund-Research/1.0"}, retries=2)
    disponibles: Optional[set[str]] = None
    try:
        api = p1.alpaca_http()
        actifs = json.loads(api.get(ALPACA_ASSETS, params={"asset_class": "crypto", "status": "active"}))
        disponibles = {a["symbol"].split("/")[0] for a in actifs if a["symbol"].endswith("/USD")}
    except (Exception, SystemExit) as err:  # noqa: BLE001
        print(f"Crypto : liste Alpaca indisponible ({err}).", file=sys.stderr)
    try:
        top = json.loads(http.get(COINGECKO, params={"vs_currency": "usd", "order": "market_cap_desc",
                                                      "per_page": 250, "page": 1}))
        classement = [c["symbol"].upper() for c in top]
    except Exception as err:  # noqa: BLE001
        print(f"Crypto : classement CoinGecko indisponible ({err}), liste de secours.", file=sys.stderr)
        classement = CRYPTOS_SECOURS
    out = []
    for s in classement:
        if s in STABLES or s in out or (disponibles is not None and s not in disponibles):
            continue
        out.append(s)
        if len(out) == n:
            break
    return [f"{s}/USD" for s in out]


# =============================================================================
# Barres : téléchargement et agrégation
# =============================================================================

def _cadre(rows: list[dict]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=["t", "open", "high", "low", "close"])
    df = pd.DataFrame(rows)
    return pd.DataFrame({"t": pd.to_datetime(df["t"], utc=True), "open": df["o"].astype(float),
                         "high": df["h"].astype(float), "low": df["l"].astype(float),
                         "close": df["c"].astype(float)}).sort_values("t", ignore_index=True)


def crypto_query(http: p1.Http, symbols: list[str], params: dict) -> dict[str, list[dict]]:
    """Barres crypto d'Alpaca, page par page (10 000 lignes par page)."""
    out: dict[str, list[dict]] = {}
    token = None
    while True:
        query = {"symbols": ",".join(symbols), "limit": 10000, **params}
        if token:
            query["page_token"] = token
        page = json.loads(http.get(ALPACA_CRYPTO, params=query))
        for sym, rows in (page.get("bars") or {}).items():
            out.setdefault(sym, []).extend(rows)
        token = page.get("next_page_token")
        if not token:
            return out


class _Cadence:
    """Espacement minimal entre deux requêtes, partagé par tous les fils (offre gratuite Alpaca :
    200 requêtes par minute pour tout le compte)."""

    def __init__(self, intervalle: float = 0.32):
        self.verrou = threading.Lock()
        self.prochain = 0.0
        self.intervalle = intervalle

    def attendre(self) -> None:
        with self.verrou:
            maintenant = time.monotonic()
            creneau = max(maintenant, self.prochain)
            self.prochain = creneau + self.intervalle
        if creneau > maintenant:
            time.sleep(creneau - maintenant)


CADENCE = _Cadence()
_fil = threading.local()


@dataclass
class HttpCadence(p1.Http):
    cadence: Optional[_Cadence] = None

    def request(self, *args, **kwargs) -> bytes:
        if self.cadence is not None:
            self.cadence.attendre()
        return super().request(*args, **kwargs)


def http_du_fil() -> p1.Http:
    """Client Alpaca propre au fil d'exécution, cadencé avec les autres fils."""
    if not hasattr(_fil, "http"):
        base = p1.alpaca_http()
        _fil.http = HttpCadence(headers=dict(base.headers), min_interval=0.0, cadence=CADENCE)
    return _fil.http


def _iso(ts: pd.Timestamp) -> str:
    return pd.Timestamp(ts).tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ")


def journalier_actions(rows: list[dict]) -> pd.DataFrame:
    """Barres journalières Alpaca -> [start, end, open, high, low, close] : séance 9 h 30 - 16 h (New York)."""
    df = _cadre(rows)
    jour = df["t"].dt.tz_convert(NY).dt.normalize()
    df["start"] = (jour + pd.Timedelta(minutes=OUVERTURE)).dt.tz_convert("UTC")
    df["end"] = (jour + pd.Timedelta(minutes=FERMETURE)).dt.tz_convert("UTC")
    return df[["start", "end", "open", "high", "low", "close"]]


def seance_actions(df: pd.DataFrame) -> pd.DataFrame:
    """Garde les barres intraday de la séance régulière (9 h 30 - 16 h, New York)."""
    if df.empty:
        return df
    loc = df["t"].dt.tz_convert(NY)
    m = loc.dt.hour * 60 + loc.dt.minute
    return df[(m >= OUVERTURE) & (m < FERMETURE)].reset_index(drop=True)


def agreger(df: pd.DataFrame, minutes: int, actions: bool) -> pd.DataFrame:
    """Barres fines [t, open, high, low, close] -> barres de `minutes` minutes [start, end, ...].
    Actions : ancrées sur l'ouverture de 9 h 30 (comme TradingView), coupées à 16 h. Crypto : UTC."""
    cols = ["start", "end", "open", "high", "low", "close"]
    if df.empty:
        return pd.DataFrame(columns=cols)
    if actions:
        loc = df["t"].dt.tz_convert(NY)
        jour = loc.dt.normalize()
        m = loc.dt.hour * 60 + loc.dt.minute
        debut = jour + pd.to_timedelta(OUVERTURE + ((m - OUVERTURE) // minutes) * minutes, unit="min")
        fin = debut + pd.Timedelta(minutes=minutes)
        cloture = jour + pd.Timedelta(minutes=FERMETURE)
        fin = fin.where(fin <= cloture, cloture).dt.tz_convert("UTC")
        cle = debut.dt.tz_convert("UTC")
    else:
        cle = df["t"].dt.floor(f"{minutes}min")
        fin = cle + pd.Timedelta(minutes=minutes)
    g = pd.DataFrame({"start": cle, "end": fin, "open": df["open"], "high": df["high"], "low": df["low"],
                      "close": df["close"]}).groupby("start", sort=True)
    out = g.agg(end=("end", "max"), open=("open", "first"), high=("high", "max"), low=("low", "min"),
                close=("close", "last")).reset_index()
    return out[cols]


def hebdomadaire(jour: pd.DataFrame, actions: bool) -> pd.DataFrame:
    """Barres journalières -> hebdomadaires (semaine du lundi). Fin nominale : vendredi 16 h (actions)
    ou lundi suivant 0 h UTC (crypto), pour ne garder que les semaines terminées."""
    cols = ["start", "end", "open", "high", "low", "close"]
    if jour.empty:
        return pd.DataFrame(columns=cols)
    ref = jour["start"].dt.tz_convert(NY) if actions else jour["start"]
    lundi = (ref.dt.normalize() - pd.to_timedelta(ref.dt.day_of_week, unit="D"))
    if actions:
        fin = (lundi + pd.Timedelta(days=4, minutes=FERMETURE)).dt.tz_convert("UTC")
    else:
        fin = lundi + pd.Timedelta(days=7)
    g = pd.DataFrame({"semaine": lundi, "start": jour["start"], "end": fin, "open": jour["open"],
                      "high": jour["high"], "low": jour["low"], "close": jour["close"]}).groupby("semaine", sort=True)
    out = g.agg(start=("start", "first"), end=("end", "max"), open=("open", "first"), high=("high", "max"),
                low=("low", "min"), close=("close", "last")).reset_index(drop=True)
    return out[cols]


def terminees(df: pd.DataFrame, maintenant: pd.Timestamp) -> pd.DataFrame:
    return df[df["end"] <= maintenant].reset_index(drop=True)


@dataclass
class Actif:
    """Un actif et ses barres. Les barres fines (5 min) sont téléchargées à la demande, fenêtre par
    fenêtre, quand une zone ou une liquidité n'est pas trouvée en unité de temps plus haute."""

    symbole: str
    crypto: bool
    jour: pd.DataFrame                      # [start, end, open, high, low, close]
    h4: pd.DataFrame
    maintenant: pd.Timestamp
    http: Optional[p1.Http] = None
    _cinq: list = field(default_factory=list)   # [(debut, fin, DataFrame [t, ...])]
    _series: dict = field(default_factory=dict)
    telechargements: int = 0

    def serie(self, ut: str) -> pd.DataFrame:
        """Série complète (bougies terminées) d'une unité de temps du range : W, D ou 4h."""
        if ut not in self._series:
            if ut == "W":
                df = hebdomadaire(self.jour, not self.crypto)
            elif ut == "D":
                df = self.jour
            else:
                df = self.h4
            self._series[ut] = terminees(df, self.maintenant)
        return self._series[ut]

    def _barres_5m(self, debut: pd.Timestamp, fin: pd.Timestamp) -> pd.DataFrame:
        for d, f, df in self._cinq:
            if d <= debut and fin <= f:
                return df[(df["t"] >= debut) & (df["t"] < fin)]
        if self.http is None or (fin - debut).days > MAX_FENETRE_5M:
            return pd.DataFrame(columns=["t", "open", "high", "low", "close"])
        d, f = debut - pd.Timedelta(days=1), min(fin + pd.Timedelta(days=1), self.maintenant)
        params = {"timeframe": "5Min", "start": _iso(d), "end": _iso(f)}
        self.telechargements += 1
        if self.crypto:
            rows = crypto_query(self.http, [self.symbole], params).get(self.symbole, [])
            df = _cadre(rows)
        else:
            rows = p1.alpaca_query(self.http, "bars", [self.symbole], {**params, "adjustment": "all"})
            df = seance_actions(_cadre(rows.get(self.symbole, [])))
        self._cinq.append((d, f, df))
        return df[(df["t"] >= debut) & (df["t"] < fin)]

    def barres(self, ut: str, debut: pd.Timestamp, fin: pd.Timestamp) -> pd.DataFrame:
        """Bougies terminées de l'unité de temps `ut` qui commencent dans [debut, fin)."""
        if ut in ("W", "D"):
            df = self.serie(ut)
        elif ut == "4h" and not self.h4.empty and self.h4["start"].iloc[0] <= debut:
            df = self.serie("4h")
        else:
            cinq = self._barres_5m(debut, fin)
            df = terminees(agreger(cinq, MINUTES[ut], not self.crypto), self.maintenant)
        return df[(df["start"] >= debut) & (df["start"] < fin)].reset_index(drop=True)


def charger_actifs(actions: list[str], cryptos: list[str], maintenant: pd.Timestamp,
                   lot: int = 100, lot_4h: int = 20) -> list[Actif]:
    """Télécharge le journalier et le 4 heures de tout l'univers (requêtes groupées, en parallèle)."""
    http = http_du_fil()
    debut_4h = _iso(maintenant - pd.Timedelta(days=HISTO_4H_JOURS))
    jours: dict[str, pd.DataFrame] = {}
    for i in range(0, len(actions), lot):
        paquet = actions[i:i + lot]
        brut = p1.alpaca_query(http, "bars", paquet, {"timeframe": "1Day", "start": DEBUT_JOUR_ACTIONS,
                                                      "adjustment": "all"})
        jours.update({s: journalier_actions(rows) for s, rows in brut.items() if rows})
        print(f"Cours journaliers des actions : {min(i + lot, len(actions))}/{len(actions)}", flush=True)

    def quatre_heures(paquet: list[str]) -> dict[str, pd.DataFrame]:
        brut = p1.alpaca_query(http_du_fil(), "bars", paquet, {"timeframe": "30Min", "start": debut_4h,
                                                               "adjustment": "all"})
        return {s: agreger(seance_actions(_cadre(rows)), 240, actions=True) for s, rows in brut.items()}

    h4s: dict[str, pd.DataFrame] = {}
    paquets = [actions[i:i + lot_4h] for i in range(0, len(actions), lot_4h)]
    with ThreadPoolExecutor(TRAVAILLEURS) as ex:
        for k, res in enumerate(ex.map(quatre_heures, paquets), 1):
            h4s.update(res)
            if k % 5 == 0 or k == len(paquets):
                print(f"Cours 4h des actions : {min(k * lot_4h, len(actions))}/{len(actions)}", flush=True)
    vide = pd.DataFrame(columns=["start", "end", "open", "high", "low", "close"])
    out: list[Actif] = []
    for s in actions:
        if s not in jours:
            print(f"{s} : aucun cours Alpaca, ignoré.", file=sys.stderr)
            continue
        out.append(Actif(s, False, jours[s], h4s.get(s, vide), maintenant, http))
    if cryptos:
        jour = crypto_query(http, cryptos, {"timeframe": "1Day", "start": DEBUT_JOUR_CRYPTO})
        h4 = crypto_query(http, cryptos, {"timeframe": "4Hour",
                                          "start": _iso(maintenant - pd.Timedelta(days=HISTO_4H_CRYPTO))})
        for s in cryptos:
            if not jour.get(s):
                print(f"{s} : aucun cours Alpaca, ignoré.", file=sys.stderr)
                continue
            dj, d4 = _cadre(jour[s]), _cadre(h4.get(s, []))
            dj = dj.assign(start=dj["t"], end=dj["t"] + pd.Timedelta(days=1))
            d4 = d4.assign(start=d4["t"], end=d4["t"] + pd.Timedelta(hours=4))
            cols = ["start", "end", "open", "high", "low", "close"]
            out.append(Actif(s, True, dj[cols], d4[cols], maintenant, http))
        print(f"Cours des cryptos : {len(cryptos)}", flush=True)
    return out


# =============================================================================
# Analyse
# =============================================================================

def _date(ts) -> str:
    return pd.Timestamp(ts).tz_convert(NY).strftime("%d/%m/%Y") if ts is not None and not pd.isna(ts) else ""


def _jour_iso(ts) -> str:
    return pd.Timestamp(ts).tz_convert("UTC").strftime("%Y-%m-%d") if ts is not None else ""


def decrire(actif: Actif, ut: str, df: pd.DataFrame, rng: mr.RangeTCT) -> dict:
    """Une ligne du rapport pour un range."""
    niv = mr.niveaux(rng.haut, rng.bas)
    dernier = float(df["close"].iloc[-1])
    hauteur = rng.haut - rng.bas
    ligne = {
        "actif": actif.symbole, "classe": "crypto" if actif.crypto else "action", "ut": ut,
        "debut_impulsion": _jour_iso(df.at[rng.a.barre, "start"]), "prix_debut": rng.a.prix,
        "fin_impulsion": _jour_iso(df.at[rng.b.barre, "start"]), "prix_fin": rng.b.prix,
        "valide_le": _jour_iso(df.at[rng.validation, "end"]),
        "haut": rng.haut, "milieu": niv["0.5"], "bas": rng.bas, "ext_haut": niv["-0.3"], "ext_bas": niv["1.3"],
        "vivant": rng.vivant, "mort_le": _jour_iso(df.at[rng.mort, "end"]) if rng.mort is not None else "",
        "sens_mort": rng.sens_mort or "", "cours": dernier,
        "position": (rng.haut - dernier) / hauteur if hauteur > 0 else np.nan,   # 0 = haut, 1 = bas
        "derniere_bougie": _jour_iso(df["end"].iloc[-1]),
    }
    for cote in mr.COTES:
        s: mr.Cote = rng.cotes[cote]
        seuil = s.seuil or mr.Seuil()
        ligne[f"{cote}_touches"] = s.touches
        ligne[f"{cote}_retour_milieu"] = s.arme
        ligne[f"{cote}_alerte_le"] = _jour_iso(df.at[s.alerte, "end"]) if s.alerte is not None else ""
        ligne[f"{cote}_alerte_ts"] = df.at[s.alerte, "end"] if s.alerte is not None else None
        z, lq = seuil.zone, seuil.liquidite
        ligne[f"{cote}_zone_bas"] = z.bas if z else np.nan
        ligne[f"{cote}_zone_haut"] = z.haut if z else np.nan
        ligne[f"{cote}_zone_ut"] = z.ut if z else ""
        ligne[f"{cote}_liquidite"] = lq.prix if lq else np.nan
        ligne[f"{cote}_liquidite_ut"] = lq.ut if lq else ""
    return ligne


def analyser_actif(actif: Actif, recent_jours: int = JOURS_ALERTE, uts: Iterable[str] = UTS) -> list[dict]:
    """Ranges vivants (et morts récemment) de l'actif, avec leurs touches, sur chaque unité de temps."""
    lignes = []
    limite = actif.maintenant - pd.Timedelta(days=recent_jours)
    for ut in uts:
        df = actif.serie(ut)
        if len(df) < 30:
            continue

        def seuils(rng: mr.RangeTCT, df=df, ut=ut):
            if rng.mort is not None and df.at[rng.mort, "end"] < limite:
                return None
            return mr.seuils_du_range(rng, df, ut, actif.barres)

        for rng in mr.detecter_ranges(df, seuils=seuils):
            if rng.vivant or df.at[rng.mort, "end"] >= limite:
                lignes.append(decrire(actif, ut, df, rng))
    return lignes


def alertes(lignes: list[dict], maintenant: pd.Timestamp, recent_jours: int = JOURS_ALERTE) -> list[dict]:
    """Alertes récentes (2e touche + retour au milieu) des ranges encore vivants."""
    out = []
    for ln in lignes:
        if not ln["vivant"]:
            continue
        for cote in mr.COTES:
            ts = ln[f"{cote}_alerte_ts"]
            if ts is None or ts < maintenant - pd.Timedelta(days=recent_jours):
                continue
            ident = f"{ln['actif']}|{ln['ut']}|{cote}|{ln['debut_impulsion']}|{ln['fin_impulsion']}"
            out.append({**ln, "cote": cote, "modele": mr.MODELE[cote], "id": ident,
                        "alerte_le": ln[f"{cote}_alerte_le"], "touches": ln[f"{cote}_touches"]})
    return sorted(out, key=lambda a: (a["alerte_le"], a["ut"] == "W", a["ut"] == "D"), reverse=True)


# =============================================================================
# Rapport et notification
# =============================================================================

def _p(x: float) -> str:
    """Prix lisible (format français, chiffres significatifs selon l'ordre de grandeur)."""
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return ""
    dec = 2 if abs(x) >= 1 else (4 if abs(x) >= 0.01 else 8)
    return f"{x:,.{dec}f}".replace(",", " ").replace(".", ",")


def _zone_txt(ln: dict, cote: str) -> str:
    parts = []
    if ln[f"{cote}_zone_ut"]:
        parts.append(f"zone {_p(ln[f'{cote}_zone_bas'])}–{_p(ln[f'{cote}_zone_haut'])} ({ln[f'{cote}_zone_ut']})")
    if ln[f"{cote}_liquidite_ut"]:
        parts.append(f"liquidité {_p(ln[f'{cote}_liquidite'])} ({ln[f'{cote}_liquidite_ut']})")
    return " · ".join(parts) or "—"


def ligne_tableau(ln: dict, cote: str) -> str:
    icone = "📉" if cote == "haut" else "📈"
    pos = ln["position"]
    pos_txt = f"{pos:.2f}".replace(".", ",") if not math.isnan(pos) else ""
    return (f"| {icone} **{ln['actif']}** | {ln['ut']} | {mr.MODELE[cote]} | {ln[f'{cote}_touches']} | "
            f"{_date(ln.get(f'{cote}_alerte_ts')) or '—'} | {_p(ln['haut'])} | {_p(ln['milieu'])} | {_p(ln['bas'])} | "
            f"{_p(ln['ext_haut'])} / {_p(ln['ext_bas'])} | {_zone_txt(ln, cote)} | {_p(ln['cours'])} ({pos_txt}) | "
            f"{ln['debut_impulsion']} → {ln['fin_impulsion']} |")


ENTETE = ("| Actif | UT | Modèle | Touches | Alerte | Haut (0) | Milieu (0,5) | Bas (1) | -0,3 / 1,3 | "
          "Zone · liquidité | Cours (position) | Impulsion |\n|---|---|---|---|---|---|---|---|---|---|---|---|")


def rapport(lignes: list[dict], nouvelles: list[dict], maintenant: pd.Timestamp, n_actifs: int,
            erreurs: list[str], recent_jours: int = JOURS_ALERTE) -> str:
    vivants = [ln for ln in lignes if ln["vivant"]]
    a_signaler = alertes(lignes, maintenant, recent_jours)
    ids_nouvelles = {a["id"] for a in nouvelles}
    attente = []      # 2e touche faite, retour au milieu attendu
    actives = []      # alerte plus ancienne, range toujours vivant
    for ln in vivants:
        for cote in mr.COTES:
            t, arme, al = ln[f"{cote}_touches"], ln[f"{cote}_retour_milieu"], ln[f"{cote}_alerte_ts"]
            if t == 2 and not arme:
                attente.append((ln, cote))
            elif al is not None and al < maintenant - pd.Timedelta(days=recent_jours):
                actives.append((ln, cote))
    out = [
        "# Ranges — modèles d'accumulation et de distribution",
        "",
        f"_Mis à jour le {maintenant.tz_convert('Europe/Paris').strftime('%d/%m/%Y à %H h %M')} (Paris) — "
        f"{n_actifs} actifs (S&P 500 + cryptos), ranges en W, D et 4h. Données Alpaca : actions ajustées des "
        f"dividendes, séance de New York ; seules les bougies terminées comptent._",
        "",
        f"> {AVERTISSEMENT}",
        "",
        f"## 🔔 Alertes : 2 touches + retour au milieu ({recent_jours} derniers jours)",
        "",
    ]
    if a_signaler:
        out += [ENTETE] + [ligne_tableau(a, a["cote"]) + (" 🆕" if a["id"] in ids_nouvelles else "")
                           for a in a_signaler]
    else:
        out.append("Aucune.")
    out += ["", "## ⏳ 2e touche faite, retour au milieu attendu", "",
            "_Pas encore d'alerte : elle partira si le prix revient au 0,5 sans tuer le range._", ""]
    out += ([ENTETE] + [ligne_tableau(ln, c) for ln, c in sorted(attente, key=lambda x: x[0]["actif"])]
            if attente else ["Aucun."])
    out += ["", "## Modèles déjà signalés, range toujours vivant", ""]
    out += ([ENTETE] + [ligne_tableau(ln, c) for ln, c in sorted(actives, key=lambda x: (x[0]["ut"], x[0]["actif"]))]
            if actives else ["Aucun."])
    par_ut = {ut: sum(1 for ln in vivants if ln["ut"] == ut) for ut in UTS}
    out += ["", "## Synthèse", "",
            f"- Ranges vivants : {par_ut['W']} en W, {par_ut['D']} en D, {par_ut['4h']} en 4h "
            f"(détail de tous les ranges dans `ranges.csv`).",
            f"- Alertes des {recent_jours} derniers jours : {len(a_signaler)} (dont {len(nouvelles)} nouvelle(s)).",
            f"- 2e touche en attente du retour au milieu : {len(attente)}."]
    if erreurs:
        out += [f"- ⚠️ Actifs non analysés ({len(erreurs)}) : {', '.join(erreurs[:30])}"
                + (" …" if len(erreurs) > 30 else "")]
    out += ["", "## Comment lire", "",
            "- **Haut (0), Milieu (0,5), Bas (1)** : niveaux du retracement de Fibonacci du range, après "
            "décalage éventuel du haut ou du bas. **-0,3 / 1,3** : une clôture au-delà tue le range.",
            "- **Position** : où est le dernier cours dans le range (0 = au haut, 1 = au bas).",
            "- **Zone** : zone d'offre (distribution) ou de demande (accumulation) de la 1re touche, avec "
            "l'unité de temps où elle a été trouvée. **Liquidité** : premier sommet (ou creux) intermédiaire "
            "entre la touche et le retour au milieu, situé entre 0 et 0,25 (ou 0,75 et 1).",
            "- **Impulsion** : dates du bas et du haut (ou du haut et du bas) qui ont formé le range, "
            "à reprendre dans l'outil Fibonacci de TradingView.",
            "- Règles complètes : `docs/SCANNER_RANGES.md`.", ""]
    return "\n".join(out)


def message_notification(nouvelles: list[dict], lien: Optional[str], max_lignes: int = 35) -> tuple[str, str]:
    """Titre et texte de la notification : une ligne par alerte, regroupées par unité de temps."""
    titre = (f"Ranges : {len(nouvelles)} modèle à 2 touches" if len(nouvelles) == 1
             else f"Ranges : {len(nouvelles)} modèles à 2 touches")
    blocs = []
    montrees = 0
    for ut in UTS:
        du_ut = sorted((a for a in nouvelles if a["ut"] == ut), key=lambda a: (a["cote"], a["actif"]))
        if not du_ut:
            continue
        lignes = [f"**{ut}**"]
        for a in du_ut:
            if montrees == max_lignes:
                break
            icone = "📉" if a["cote"] == "haut" else "📈"
            lignes.append(f"{icone} {a['actif']} {a['modele']} · haut {_p(a['haut'])} · milieu {_p(a['milieu'])} · "
                          f"bas {_p(a['bas'])} · cours {_p(a['cours'])}")
            montrees += 1
        blocs.append("\n".join(lignes))
    if len(nouvelles) > montrees:
        blocs.append(f"… et {len(nouvelles) - montrees} autres dans le rapport.")
    if lien:
        blocs.append(f"Rapport complet (zones, liquidité, dates) : {lien}")
    return titre, "\n\n".join(blocs)


def lire_etat(chemin: Optional[Path]) -> dict:
    if chemin is None or not Path(chemin).exists():
        return {}
    try:
        return json.loads(Path(chemin).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def nouvelles_alertes(alertes_recentes: list[dict], etat: dict) -> list[dict]:
    deja = set((etat.get("alertes_envoyees") or {}).keys())
    return [a for a in alertes_recentes if a["id"] not in deja]


def etat_suivant(etat: dict, envoyees: list[dict], maintenant: pd.Timestamp) -> dict:
    garde = maintenant - pd.Timedelta(days=GARDE_ALERTES)
    memo = {k: v for k, v in (etat.get("alertes_envoyees") or {}).items() if pd.Timestamp(v, tz="UTC") >= garde}
    for a in envoyees:
        memo[a["id"]] = maintenant.strftime("%Y-%m-%d")
    return {"genere_le": maintenant.strftime("%Y-%m-%dT%H:%M:%SZ"), "alertes_envoyees": memo}


# =============================================================================
# Programme
# =============================================================================

def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Scanner des ranges (modèles d'accumulation / distribution)")
    ap.add_argument("--symboles", help="liste séparée par des virgules (ex. JNJ,AAPL,BTC/USD) ; défaut : tout l'univers")
    ap.add_argument("--cryptos", type=int, default=NB_CRYPTOS, help="nombre de cryptos de l'univers")
    ap.add_argument("--etat", type=Path, help="état du passage précédent (alertes déjà envoyées)")
    ap.add_argument("--sortie", type=Path, default=OUT)
    ap.add_argument("--notifier", action="store_true", help="envoie les nouvelles alertes sur le téléphone (ntfy)")
    ap.add_argument("--lien", help="adresse du rapport, ajoutée à la notification")
    ap.add_argument("--jours", type=int, default=JOURS_ALERTE, help="ancienneté maximale d'une alerte envoyée")
    args = ap.parse_args(argv)

    maintenant = pd.Timestamp.now(tz="UTC").floor("min")
    if args.symboles:
        demandes = [s.strip().upper() for s in args.symboles.split(",") if s.strip()]
        actions = [s for s in demandes if "/" not in s]
        cryptos = [s for s in demandes if "/" in s]
    else:
        actions = univers_actions()
        cryptos = univers_crypto(n=args.cryptos)
    print(f"Univers : {len(actions)} actions, {len(cryptos)} cryptos ({', '.join(cryptos)})", flush=True)

    actifs = charger_actifs(actions, cryptos, maintenant)
    lignes: list[dict] = []
    erreurs: list[str] = []
    debut = time.monotonic()

    def analyser(actif: Actif) -> tuple[list[dict], Optional[str]]:
        actif.http = http_du_fil()
        try:
            return analyser_actif(actif, args.jours), None
        except Exception:  # noqa: BLE001 — un actif en échec ne doit pas arrêter le scanner
            return [], traceback.format_exc()
        finally:
            actif._cinq.clear()

    with ThreadPoolExecutor(TRAVAILLEURS) as ex:
        for i, (actif, (res, err)) in enumerate(zip(actifs, ex.map(analyser, actifs)), 1):
            lignes += res
            if err:
                erreurs.append(actif.symbole)
                print(f"{actif.symbole} : échec de l'analyse\n{err}", file=sys.stderr)
            if i % 50 == 0 or i == len(actifs):
                print(f"Analyse : {i}/{len(actifs)} ({time.monotonic() - debut:.0f} s)", flush=True)
    erreurs += sorted(set(actions + cryptos) - {a.symbole for a in actifs})

    recentes = alertes(lignes, maintenant, args.jours)
    etat = lire_etat(args.etat)
    nouvelles = nouvelles_alertes(recentes, etat)
    args.sortie.mkdir(parents=True, exist_ok=True)
    (args.sortie / "rapport_ranges.md").write_text(
        rapport(lignes, nouvelles, maintenant, len(actifs), erreurs, args.jours), encoding="utf-8")
    cols = [c for c in (lignes[0].keys() if lignes else []) if not c.endswith("_ts")]
    pd.DataFrame(lignes, columns=cols or None).to_csv(args.sortie / "ranges.csv", index=False)
    envoyees: list[dict] = []
    if nouvelles:
        titre, texte = message_notification(nouvelles, args.lien)
        (args.sortie / "notification.md").write_text(f"# {titre}\n\n{texte}\n", encoding="utf-8")
        if args.notifier and notifier.send(titre, texte, click=args.lien):
            envoyees = nouvelles
        elif not args.notifier:
            envoyees = nouvelles   # sans envoi demandé, on considère l'alerte comme vue (rapport)
    (args.sortie / "etat_ranges.json").write_text(
        json.dumps(etat_suivant(etat, envoyees, maintenant), ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(recentes)} alerte(s) récente(s), {len(nouvelles)} nouvelle(s) ; rapport : "
          f"{args.sortie / 'rapport_ranges.md'}")
    # Trop d'actifs en échec : données manquantes, le passage est signalé en échec
    return 1 if len(erreurs) > max(10, 0.1 * (len(actions) + len(cryptos))) else 0


if __name__ == "__main__":
    sys.exit(main())
