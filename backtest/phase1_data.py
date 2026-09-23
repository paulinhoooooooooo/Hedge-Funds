#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
phase1_data.py — Circuit de données réelles de la phase 1 (actions américaines, budget nul)
==========================================================================================

Construit, à partir de sources officielles et gratuites, les quatre fichiers attendus par
flow_backtest.load_market_from_csv (assets.csv, prices.csv, holdings.csv, flows.csv).

Étapes (chacune met ses résultats en cache dans data/phase1/ et reprend là où elle s'est
arrêtée ; `all` les enchaîne) :

  sec       jeux « Form 13F Data Sets » de la SEC -> positions de chaque gérant, trimestre
            par trimestre, avec les règles point-in-time du moteur        [SEC_CONTACT_EMAIL]
  universe  univers point-in-time : les plus grosses lignes 13F de chaque trimestre, dans la
            limite gratuite de Tiingo (500 symboles par mois, ETF compris)
  figi      code CUSIP -> symbole boursier (API OpenFIGI, gratuite)
  sectors   secteur d'activité (code SIC publié par la SEC) -> ETF sectoriel SPDR
                                                                            [SEC_CONTACT_EMAIL]
  prices    cours ajustés, plus hauts, plus bas, volumes et divisions d'actions : Alpaca si ses
            clés sont présentes (quelques minutes, historique depuis 2016), sinon Tiingo (50
            requêtes par heure en gratuit : plusieurs heures, depuis 2013)   [clés Alpaca ou Tiingo]
  finra     volumes échangés hors bourse et vendus à découvert hors bourse, titre par titre
            (fichiers FINRA « Reg SHO » quotidiens, depuis août 2018) -> radar des grands acteurs
  cot       positions des banques (« Dealer / Intermediary ») sur les contrats à terme
            E-mini S&P 500 et Nasdaq-100 (CFTC, rapport TFF)
  ats       bourses privées titre par titre et plateforme par plateforme (FINRA, hebdomadaire,
            depuis 2022) : blocs institutionnels et plateformes des banques
  short     positions vendeuses déclarées, deux fois par mois (FINRA, depuis 2019)
  events    dates de résultats (8-K, rubrique 2.02) et franchissements de 5 % du capital
            (13D / 13G) avec le nom du déclarant                               [SEC_CONTACT_EMAIL]
  insiders  achats des dirigeants sur le marché (Form 4, code P) : jeux trimestriels de la SEC,
            complétés par les Form 4 déposés depuis                          [SEC_CONTACT_EMAIL]
  alpaca    barres horaires de toutes les bourses (unité de temps « heure » du radar) et gros
            blocs d'au moins 1 M$ repérés dans le détail des transactions, 15 minutes après
                                                    [ALPACA_API_KEY_ID, ALPACA_API_SECRET_KEY]
  build     liste Smart Money, indice de détention, proxy de flux -> data/phase1/engine/
  names     noms des gérants de la liste Smart Money (SEC)                   [SEC_CONTACT_EMAIL]
  buyers    qui a acheté ou vendu chaque action, trimestre par trimestre -> fiches de trade

Identifiants (jamais dans le code ni dans la conversation) :
  SEC_CONTACT_EMAIL  adresse de contact exigée par la SEC pour tout téléchargement automatique
  TIINGO_API_KEY     clé Tiingo ; inutile si elle est enregistrée dans les « API credentials »
                     de l'environnement (en-tête Authorization: Token … ajouté automatiquement)
  ALPACA_API_KEY_ID, ALPACA_API_SECRET_KEY   clés du compte d'essai (« Paper ») gratuit Alpaca

Usage :
    python backtest/phase1_data.py status
    python backtest/phase1_data.py all
    python backtest/flow_backtest.py --data-dir data/phase1/engine --entry-flow 0 --exit-flow -0.05
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import time
import http.client
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Mapping, Optional
from urllib.parse import urlencode, urljoin

import numpy as np
import pandas as pd

import flow_backtest as fb
import smart_money as sm
from institutional_radar import BANK_VENUES, BLOCK_VENUES

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "phase1"

SEC_13F_PAGE = "https://www.sec.gov/data-research/sec-markets-data/form-13f-data-sets"
SEC_TICKERS = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
OPENFIGI_URL = "https://api.openfigi.com/v3/mapping"
TIINGO_PRICES = "https://api.tiingo.com/tiingo/daily/{ticker}/prices"
CFTC_TFF = "https://publicreporting.cftc.gov/resource/gpe5-46if.json"
FINRA_DAILY = "https://cdn.finra.org/equity/regsho/daily/CNMSshvol{day:%Y%m%d}.txt"
FINRA_START = "2018-08-01"  # fichiers consolidés « CNMS » disponibles depuis cette date
FINRA_API = "https://api.finra.org/data/group/otcMarket/name/{name}"
ATS_START = "2022-01-03"  # bourses privées titre par titre : historique de l'API FINRA
SHORT_START = "2019-01-01"
SHORT_PUBLICATION_DAYS = 11  # positions arrêtées au 15 et en fin de mois, publiées ~7 jours ouvrés après
SEC_INSIDER_PAGE = "https://www.sec.gov/data-research/sec-markets-data/insider-transactions-data-sets"
SEC_ARCHIVES = "https://www.sec.gov/Archives/edgar/data/{cik}/{folder}/{document}"
EVENTS_START = "2019-01-01"
ALPACA_DATA = "https://data.alpaca.markets/v2/stocks/{kind}"
ALPACA_HISTORY_START = "2016-01-01"  # historique des barres Alpaca : depuis 2016
BLOCK_MIN_NOTIONAL = 1e6  # un « gros bloc » : au moins 1 M$ en une seule transaction
# Transactions exclues : prix moyen ou dérivé d'un autre produit, prix antérieur, hors séquence,
# ouvertures et clôtures officielles (enchères mécaniques)
BLOCK_EXCLUDED_CONDITIONS = {"B", "W", "4", "P", "Z", "U", "M", "Q", "O", "6", "5", "9"}
FIVE_PCT_FORMS = {"SC 13D", "SC 13G", "SCHEDULE 13D", "SCHEDULE 13G"}  # déclarations initiales uniquement

# ETF sectoriels SPDR (jambe rapide) et marché large ; un ETF lancé tardivement est prolongé
# dans le passé par l'ETF qui couvrait ce secteur avant lui (le Chaikin Money Flow est sans unité).
SECTOR_ETFS = ["XLK", "XLV", "XLF", "XLE", "XLI", "XLY", "XLP", "XLU", "XLB", "XLRE", "XLC"]
MARKET_ETF = "SPY"
ETF_PREDECESSOR = {"XLRE": "XLF", "XLC": "XLK"}

# Code SIC (SEC) -> ETF sectoriel. Correspondance approximative, documentée : le SIC est une
# nomenclature d'activité, pas la classification GICS des ETF.
SIC_TO_ETF: list[tuple[int, int, str]] = [
    (100, 999, "XLP"), (1000, 1299, "XLB"), (1300, 1399, "XLE"), (1400, 1499, "XLB"),
    (1500, 1799, "XLI"), (2000, 2199, "XLP"), (2200, 2399, "XLY"), (2400, 2499, "XLB"),
    (2500, 2599, "XLY"), (2600, 2699, "XLB"), (2700, 2799, "XLC"), (2800, 2829, "XLB"),
    (2830, 2836, "XLV"), (2837, 2839, "XLB"), (2840, 2844, "XLP"), (2845, 2899, "XLB"),
    (2900, 2999, "XLE"), (3000, 3399, "XLB"), (3400, 3569, "XLI"), (3570, 3579, "XLK"),
    (3580, 3659, "XLI"), (3660, 3679, "XLK"), (3680, 3709, "XLI"), (3710, 3716, "XLY"),
    (3717, 3829, "XLI"), (3830, 3851, "XLV"), (3852, 3999, "XLY"), (4000, 4799, "XLI"),
    (4800, 4899, "XLC"), (4900, 4999, "XLU"), (5000, 5199, "XLI"), (5200, 5399, "XLY"),
    (5400, 5499, "XLP"), (5500, 5999, "XLY"), (6000, 6499, "XLF"), (6500, 6553, "XLRE"),
    (6554, 6797, "XLF"), (6798, 6798, "XLRE"), (6799, 6799, "XLF"), (7000, 7299, "XLY"),
    (7300, 7369, "XLI"), (7370, 7379, "XLK"), (7380, 7799, "XLI"), (7800, 7899, "XLC"),
    (7900, 7999, "XLY"), (8000, 8099, "XLV"), (8100, 8999, "XLI"),
]

# Contrats à terme suivis dans le rapport TFF (code CFTC -> libellé)
COT_MARKETS = {"13874A": "E-mini S&P 500", "209742": "E-mini Nasdaq-100"}


def sic_to_etf(sic: Optional[int]) -> str:
    if sic is None or pd.isna(sic):
        return MARKET_ETF
    for lo, hi, etf in SIC_TO_ETF:
        if lo <= int(sic) <= hi:
            return etf
    return MARKET_ETF


def to_naive_dates(values) -> pd.Series:
    """Dates avec ou sans fuseau horaire -> datetime64[ns] sans fuseau."""
    ts = pd.to_datetime(pd.Series(values), utc=True)
    return ts.dt.tz_convert(None).astype("datetime64[ns]").reset_index(drop=True)


# La SEC a changé l'unité du champ VALUE : milliers de dollars jusqu'au 2 janvier 2023, dollars ensuite.
VALUE_IN_DOLLARS_FROM = pd.Timestamp("2023-01-03")


def value_in_dollars(value: pd.Series, filing_date: pd.Series) -> pd.Series:
    return value.where(filing_date >= VALUE_IN_DOLLARS_FROM, value * 1000.0)


# Fonds indiciels cotés : ils ne sont pas des « actions » de l'univers (les ETF sectoriels
# servent déjà de jambe rapide) et ne doivent pas consommer le quota de symboles.
FUND_NAME = re.compile(r"\bETF\b|SPDR|ISHARES|POWERSHARES|VANGUARD .*(?:INDEX|FD|FUND|ETF)|SELECT SECTOR|QQQ|INDEX FD",
                       re.I)


def is_equity_line(cusip: pd.Series, name: pd.Series) -> pd.Series:
    """Actions ordinaires seulement. Les positions 7 et 8 d'un CUSIP (numéro d'émission) sont
    numériques pour une action et alphabétiques pour une dette : des obligations convertibles
    déclarées par erreur en « SH » sont ainsi écartées, comme les fonds indiciels cotés."""
    issue = cusip.str[6:8]
    return issue.str.fullmatch(r"\d\d").fillna(False) & ~name.fillna("").str.contains(FUND_NAME)


ISSUER_NOISE = {"INC", "CORP", "CORPORATION", "CO", "COMPANY", "PLC", "LTD", "LIMITED", "HOLDING", "HOLDINGS",
                "HLDG", "HLDGS", "HLDNGS", "GROUP", "GRP", "NV", "N", "V", "SA", "S", "A", "AG", "SE", "LP", "THE",
                "NEW", "DEL", "DE", "CL", "CLASS", "COM", "IRELAND", "BERMUDA", "NETHERLANDS", "MASS", "PL"}
ISSUER_ABBREV = {"INTL": "INTERNATIONAL", "TECH": "TECHNOLOGY", "TECHNOLOGIES": "TECHNOLOGY", "BK": "BANK",
                 "SYS": "SYSTEMS", "PHARMA": "PHARMACEUTICALS", "COMMUNICATIONS": "COMMUNICATION"}


def normalize_issuer(name: str) -> str:
    """« Exxon Mobil Corp. » -> « EXXONMOBIL » : clé de rapprochement par nom avec la liste de la
    SEC (abréviations développées, formes juridiques et pays retirés, espaces supprimés)."""
    words = re.sub(r"[^A-Z0-9 ]", " ", str(name).upper().replace("&", " AND ")).split()
    return "".join(ISSUER_ABBREV.get(w, w) for w in words if w not in ISSUER_NOISE)


def tiingo_symbol(ticker: str) -> str:
    """BRK/B (OpenFIGI) -> BRK-B (Tiingo)."""
    return ticker.strip().upper().replace("/", "-").replace(".", "-")


# =============================================================================
# Accès réseau (bibliothèque standard, débit limité, reprises sur erreur)
# =============================================================================

class HttpError(RuntimeError):
    def __init__(self, status: int, url: str, body: str = ""):
        super().__init__(f"HTTP {status} sur {url} {body[:200]}")
        self.status = status
        self.body = body


@dataclass
class Http:
    """Client HTTP minimal : en-têtes fixes, intervalle minimal entre deux appels, reprises."""

    headers: dict = field(default_factory=dict)
    min_interval: float = 0.0
    retries: int = 4
    _last: float = 0.0

    def request(self, url: str, data: Optional[bytes] = None, headers: Optional[dict] = None,
                method: Optional[str] = None) -> bytes:
        for attempt in range(self.retries + 1):
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            req = urllib.request.Request(url, data=data, headers={**self.headers, **(headers or {})}, method=method)
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    return resp.read()
            except urllib.error.HTTPError as err:
                body = err.read().decode("utf-8", "replace") if err.fp else ""
                if err.code in (429, 500, 502, 503, 504) and attempt < self.retries:
                    time.sleep(min(600, 30 * 2 ** attempt))
                    continue
                raise HttpError(err.code, url, body) from None
            except (urllib.error.URLError, http.client.HTTPException, ConnectionError, TimeoutError):
                # Coupure réseau, y compris une réponse tronquée (IncompleteRead) : on recommence.
                if attempt < self.retries:
                    time.sleep(10 * 2 ** attempt)
                    continue
                raise
        raise RuntimeError("inaccessible")

    def get(self, url: str, params: Optional[dict] = None, headers: Optional[dict] = None) -> bytes:
        if params:
            url = f"{url}?{urlencode(params)}"
        return self.request(url, headers=headers)

    def post_json(self, url: str, payload) -> object:
        body = self.request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"},
                            method="POST")
        return json.loads(body)


def sec_http() -> Http:
    email = os.environ.get("SEC_CONTACT_EMAIL", "").strip()
    if "@" not in email:
        sys.exit("SEC_CONTACT_EMAIL absent : la SEC exige une adresse de contact (réglages de l'environnement).")
    # Politique d'accès de la SEC : un User-Agent qui identifie le demandeur, 10 requêtes/s au plus.
    return Http(headers={"User-Agent": f"FlowFund-Research {email}", "Accept-Encoding": "identity"},
                min_interval=0.15)


def tiingo_http() -> Http:
    headers = {"User-Agent": "FlowFund-Research/1.0", "Content-Type": "application/json"}
    key = os.environ.get("TIINGO_API_KEY", "").strip()
    if key:
        headers["Authorization"] = f"Token {key}"
    # Offre gratuite : 50 requêtes par heure -> une toutes les 75 secondes.
    return Http(headers=headers, min_interval=75.0)


# =============================================================================
# Étape « sec » : jeux 13F -> fichiers intermédiaires par archive
# =============================================================================

def list_13f_zips(http: Http, page: str = SEC_13F_PAGE) -> list[str]:
    html = http.get(page).decode("utf-8", "replace")
    urls = [urljoin(page, h) for h in re.findall(r'href="([^"]+\.zip)"', html, flags=re.I)]
    return list(dict.fromkeys(u for u in urls if "13f" in u.lower()))


INFOTABLE_COLUMNS = {"ACCESSION_NUMBER", "NAMEOFISSUER", "CUSIP", "VALUE", "SSHPRNAMT", "SSHPRNAMTTYPE", "PUTCALL"}


def _read_member(archive: zipfile.ZipFile, suffix: str, columns: Optional[set[str]] = None) -> pd.DataFrame:
    names = [n for n in archive.namelist() if n.upper().endswith(suffix)]
    if not names:
        raise ValueError(f"{suffix} introuvable dans l'archive")
    usecols = (lambda c: c in columns) if columns else None
    with archive.open(names[0]) as fh:
        return pd.read_csv(fh, sep="\t", dtype=str, usecols=usecols, quoting=3, on_bad_lines="skip")


def process_13f_zip(path: Path, statutory_lag_days: int = 45) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Une archive SEC -> (lignes de portefeuille filtrées, statistiques par gérant)."""
    with zipfile.ZipFile(path) as archive:
        sub = _read_member(archive, "SUBMISSION.TSV")
        info = _read_member(archive, "INFOTABLE.TSV", INFOTABLE_COLUMNS)
    rows = fb.filter_13f_filings(sub, info, statutory_lag_days)
    rows = (rows.groupby(["cik", "cusip", "period_end", "filing_date", "accession"], as_index=False)
            .agg(shares=("shares", "sum"), value=("value", "sum"), name=("name", "first")))
    stats = (rows.groupby(["cik", "period_end", "accession", "filing_date"], as_index=False)
             .agg(n_positions=("cusip", "nunique"), total_value=("value", "sum")))
    return rows, stats


def stage_sec(http: Optional[Http] = None, keep_zips: bool = False) -> None:
    http = http or sec_http()
    raw, inter = DATA / "sec" / "raw", DATA / "sec" / "intermediate"
    raw.mkdir(parents=True, exist_ok=True)
    inter.mkdir(parents=True, exist_ok=True)
    urls = list_13f_zips(http)
    print(f"{len(urls)} archives 13F publiées par la SEC")
    for k, url in enumerate(urls, start=1):
        name = Path(url).stem
        done = inter / f"{name}.rows.parquet"
        if done.exists():
            continue
        zpath = raw / f"{name}.zip"
        if not zpath.exists():
            print(f"[{k}/{len(urls)}] téléchargement {name}")
            zpath.write_bytes(http.get(url))
        rows, stats = process_13f_zip(zpath)
        stats.to_parquet(inter / f"{name}.managers.parquet", index=False)
        rows.to_parquet(done, index=False)
        print(f"[{k}/{len(urls)}] {name} : {len(rows):,} lignes, {stats['cik'].nunique():,} gérants")
        if not keep_zips:
            zpath.unlink()


def _intermediates(kind: str) -> list[Path]:
    return sorted((DATA / "sec" / "intermediate").glob(f"*.{kind}.parquet"))


def first_filing_per_manager(stats: pd.DataFrame) -> pd.DataFrame:
    """Une seule déclaration initiale par gérant et par trimestre, toutes archives confondues."""
    return stats.sort_values("filing_date").drop_duplicates(["cik", "period_end"], keep="first")


# =============================================================================
# Étape « universe » : plus grosses lignes 13F de chaque trimestre
# =============================================================================

def build_universe(rows: "pd.DataFrame | Iterable[pd.DataFrame]", stats: pd.DataFrame, max_symbols: int = 488,
                   store_top: int = 1000, min_filers: int = 20) -> tuple[pd.DataFrame, int]:
    """Classe les titres de chaque trimestre par valeur totale déclarée.

    Retourne (classement des `store_top` premiers titres de chaque trimestre, N retenu) où N est
    le plus grand nombre de titres par trimestre tel que l'union sur toute la période tienne dans
    `max_symbols` (limite gratuite de Tiingo, ETF déduits). Le classement d'un trimestre n'utilise
    que les déclarations de ce trimestre : pas de biais du survivant.

    `rows` peut être une suite de tables (une par archive SEC) : chacune est agrégée à part, ce
    qui évite de charger en mémoire les dizaines de millions de lignes de toute la période.
    Une déclaration (accession) n'apparaît que dans une archive : les sommes et les nombres de
    gérants distincts s'additionnent donc d'une archive à l'autre.
    """
    firsts = first_filing_per_manager(stats)[["cik", "period_end", "accession"]]
    chunks = [rows] if isinstance(rows, pd.DataFrame) else rows
    parts = []
    for chunk in chunks:
        chunk = chunk[is_equity_line(chunk["cusip"], chunk["name"])]
        chunk = chunk.merge(firsts, on=["cik", "period_end", "accession"])
        chunk = chunk.assign(value_usd=value_in_dollars(chunk["value"], chunk["filing_date"]))
        if "shares" in chunk.columns:
            chunk = chunk.assign(price=chunk["value_usd"] / chunk["shares"].where(chunk["shares"] > 0))
        else:
            chunk = chunk.assign(price=np.nan, shares=np.nan)
        parts.append(chunk.groupby(["period_end", "cusip"], as_index=False)
                     .agg(value_usd=("value_usd", "sum"), shares=("shares", "sum"), price=("price", "median"),
                          n_filers=("cik", "nunique"), name=("name", "first")))
    parts = pd.concat(parts, ignore_index=True)
    # Prix médian de l'archive qui compte le plus de déclarants (en pratique : celle du trimestre)
    best = parts.sort_values("n_filers").drop_duplicates(["period_end", "cusip"], keep="last")
    agg = (parts.groupby(["period_end", "cusip"], as_index=False)
           .agg(value_usd=("value_usd", "sum"), shares=("shares", "sum"), n_filers=("n_filers", "sum"),
                name=("name", "first"))
           .merge(best[["period_end", "cusip", "price"]], on=["period_end", "cusip"]))
    # Valeur robuste : actions détenues x prix médian déclaré. Des gérants déclarent parfois en
    # dollars une valeur attendue en milliers (x 1000) : une seule erreur suffisait à placer une
    # obligation convertible ou Apple (5 600 milliards en 2014) en tête du classement.
    robust = agg["shares"] * agg["price"]
    agg["value_usd"] = robust.where(robust.notna(), agg["value_usd"])
    agg = agg.drop(columns=["shares", "price"])
    # Une grande capitalisation est détenue par des centaines de gérants : un titre déclaré par
    # une poignée d'entre eux (code fictif 999999999, action de préférence non cotée, erreur de
    # saisie) ne peut pas figurer parmi les premiers.
    agg = agg[agg["n_filers"] >= min_filers].copy()
    agg["rank"] = agg.groupby("period_end")["value_usd"].rank(ascending=False, method="first").astype(int)
    ranked = agg[agg["rank"] <= store_top].sort_values(["period_end", "rank"])
    n_keep = 1
    for n in range(1, int(ranked["rank"].max() if len(ranked) else 0) + 1):
        if ranked.loc[ranked["rank"] <= n, "cusip"].nunique() > max_symbols:
            break
        n_keep = n
    return ranked, n_keep


def stage_universe(max_symbols: int = 488) -> None:
    stats = pd.concat([pd.read_parquet(p) for p in _intermediates("managers")], ignore_index=True)
    frames = (pd.read_parquet(p, columns=["cik", "cusip", "period_end", "filing_date", "accession", "shares", "value",
                                          "name"])
              for p in _intermediates("rows"))
    ranked, n_keep = build_universe(frames, stats, max_symbols)
    ranked.to_parquet(DATA / "universe_ranked.parquet", index=False)
    first_filing_per_manager(stats).to_parquet(DATA / "managers.parquet", index=False)
    json.dump({"top_per_quarter": n_keep}, open(DATA / "universe.json", "w"))
    n_symbols = ranked.loc[ranked["rank"] <= n_keep, "cusip"].nunique()
    print(f"Univers : {n_keep} premiers titres par trimestre, {n_symbols} titres distincts sur la période")


def universe_cusips(ranked: pd.DataFrame, n_keep: int) -> list[str]:
    return sorted(ranked.loc[ranked["rank"] <= n_keep, "cusip"].unique())


# =============================================================================
# Étape « figi » : CUSIP -> symbole boursier
# =============================================================================

# Codes d'Exchange Bloomberg des places américaines : composite « US » d'abord, puis NYSE,
# Nasdaq, NYSE American, Arca… (sans clé, OpenFIGI omet parfois le composite).
US_EXCH_CODES = ["US", "UN", "UW", "UQ", "UR", "UA", "UP", "UF", "UD", "UT", "UV"]


def pick_us_listing(hits: list[dict]) -> Optional[dict]:
    rank = {code: k for k, code in enumerate(US_EXCH_CODES)}
    us = sorted((h for h in hits if h.get("exchCode") in rank), key=lambda h: rank[h["exchCode"]])
    return us[0] if us else None


def map_cusips(cusips: Iterable[str], http: Http, cache: dict,
               names: Optional[Mapping[str, str]] = None, sec_listing: Optional[Mapping[str, str]] = None) -> dict:
    """OpenFIGI : 10 identifiants par requête, 25 requêtes par minute sans clé.

    Sans cotation américaine chez OpenFIGI, le titre est rapproché par nom d'émetteur de la liste
    des sociétés cotées publiée par la SEC (`sec_listing` : nom normalisé -> symbole), source
    « sec-name ». Seuls les titres encore cotés sont retrouvés ainsi : les radiés restent exclus.
    Un code déjà résolu n'est pas redemandé ; un code non résolu l'est à chaque passage.
    Les codes résolus par OpenFIGI passent avant ceux résolus par le nom.
    """
    todo = [c for c in cusips if cache.get(c) is None]
    for start in range(0, len(todo), 10):
        batch = todo[start:start + 10]
        answer = http.post_json(OPENFIGI_URL, [{"idType": "ID_CUSIP", "idValue": c} for c in batch])
        for cusip, res in zip(batch, answer):
            hit = pick_us_listing(res.get("data", []))
            cache[cusip] = ({"ticker": hit["ticker"], "name": hit.get("name", ""),
                             "type": hit.get("securityType", ""), "source": "openfigi"} if hit else None)
    # Un symbole déjà attribué à un autre CUSIP n'est pas réutilisé : le second code d'un même
    # émetteur est souvent une action de préférence (Bank of America série L…), pas l'ordinaire.
    taken = {v["ticker"] for v in cache.values() if v}
    for cusip in todo:
        if cache.get(cusip) is None and names and sec_listing:
            ticker = sec_listing.get(normalize_issuer(names.get(cusip, "")))
            if ticker and ticker not in taken:
                taken.add(ticker)
                cache[cusip] = {"ticker": ticker, "name": names[cusip], "type": "", "source": "sec-name"}
    return cache


def sec_name_listing(http: Http) -> dict[str, str]:
    """Liste des sociétés cotées de la SEC : nom normalisé -> symbole (noms ambigus écartés)."""
    listing = json.loads(http.get(SEC_TICKERS)).values()
    out: dict[str, Optional[str]] = {}
    for v in listing:
        key = normalize_issuer(v["title"])
        if key:
            # Plusieurs classes d'actions (GOOG/GOOGL) : la première citée par la SEC, la plus liquide.
            out.setdefault(key, v["ticker"])
    return {k: t for k, t in out.items() if t}


def stage_figi(http: Optional[Http] = None) -> None:
    http = http or Http(headers={"User-Agent": "FlowFund-Research/1.0"}, min_interval=2.6)
    ranked = pd.read_parquet(DATA / "universe_ranked.parquet")
    n_keep = json.load(open(DATA / "universe.json"))["top_per_quarter"]
    path = DATA / "figi.json"
    cache = json.load(open(path)) if path.exists() else {}
    cusips = universe_cusips(ranked, n_keep)
    names = ranked.sort_values("period_end").drop_duplicates("cusip", keep="last").set_index("cusip")["name"]
    try:
        listing = sec_name_listing(sec_http())
    except (HttpError, OSError, SystemExit):
        listing = {}
    map_cusips(cusips, http, cache, names.to_dict(), listing)
    json.dump(cache, open(path, "w"), indent=1)
    found = [c for c in cusips if cache.get(c)]
    by_name = sum(cache[c].get("source") == "sec-name" for c in found)
    print(f"OpenFIGI : {len(found)}/{len(cusips)} codes CUSIP reconnus, dont {by_name} par le nom d'émetteur "
          f"(les autres, souvent radiés, sont exclus)")


def cusip_ticker_map() -> dict[str, str]:
    """CUSIP -> symbole, limité à l'univers courant : le cache OpenFIGI garde aussi les codes
    d'univers précédents, qui ne doivent consommer ni le quota Tiingo ni du temps de calcul."""
    cache = json.load(open(DATA / "figi.json"))
    if (DATA / "universe.json").exists() and (DATA / "universe_ranked.parquet").exists():
        ranked = pd.read_parquet(DATA / "universe_ranked.parquet", columns=["cusip", "rank"])
        keep = set(universe_cusips(ranked, json.load(open(DATA / "universe.json"))["top_per_quarter"]))
        cache = {c: v for c, v in cache.items() if c in keep}
    return {c: tiingo_symbol(v["ticker"]) for c, v in cache.items() if v}


# =============================================================================
# Étape « sectors » : code SIC de la SEC -> ETF sectoriel
# =============================================================================

def stage_sectors(http: Optional[Http] = None) -> None:
    http = http or sec_http()
    path = DATA / "sectors.json"
    cache = json.load(open(path)) if path.exists() else {}
    listing = json.loads(http.get(SEC_TICKERS))
    ticker_cik = {tiingo_symbol(v["ticker"]): int(v["cik_str"]) for v in listing.values()}
    for ticker in sorted(set(cusip_ticker_map().values())):
        if ticker in cache:
            continue
        cik = ticker_cik.get(ticker)
        sic = None
        if cik:
            try:
                sic = json.loads(http.get(SEC_SUBMISSIONS.format(cik=cik))).get("sic")
            except HttpError:
                pass
        cache[ticker] = {"cik": cik, "sic": int(sic) if sic else None, "etf": sic_to_etf(int(sic) if sic else None)}
    json.dump(cache, open(path, "w"), indent=1)
    unknown = sum(v["sic"] is None for v in cache.values())
    print(f"Secteurs : {len(cache)} titres, {unknown} sans code SIC (rattachés à {MARKET_ETF})")


# =============================================================================
# Étape « prices » : Tiingo
# =============================================================================

def parse_tiingo_csv(raw: bytes) -> pd.DataFrame:
    df = pd.read_csv(io.BytesIO(raw))
    df["date"] = to_naive_dates(df["date"]).to_numpy()
    return df


def alpaca_keys_present() -> bool:
    return bool(os.environ.get("ALPACA_API_KEY_ID", "").strip() and os.environ.get("ALPACA_API_SECRET_KEY", "").strip())


def alpaca_daily_frame(adjusted: list[dict], split_only: list[dict], raw: list[dict]) -> pd.DataFrame:
    """Barres quotidiennes Alpaca -> format des fichiers Tiingo (adjClose, adjHigh, adjLow, adjVolume,
    splitFactor). Le facteur de division se lit dans le rapport cours brut / cours corrigé des seules
    divisions : il change d'un facteur 2 le jour d'une division 2 pour 1."""
    def frame(rows):
        df = pd.DataFrame(rows)
        df.index = pd.DatetimeIndex(new_york_time(df["t"]).dt.normalize().to_numpy())
        return df
    adj, spl, raw_df = frame(adjusted), frame(split_only), frame(raw)
    ratio = (raw_df["c"] / spl["c"]).reindex(adj.index).ffill().bfill()
    factor = (ratio.shift(1) / ratio).fillna(1.0)
    factor = factor.where((factor - 1.0).abs() > 1e-3, 1.0).round(6)
    return pd.DataFrame({"date": adj.index.strftime("%Y-%m-%d"), "close": raw_df["c"].reindex(adj.index).to_numpy(),
                         "adjOpen": adj["o"].to_numpy(), "adjHigh": adj["h"].to_numpy(), "adjLow": adj["l"].to_numpy(),
                         "adjClose": adj["c"].to_numpy(), "adjVolume": adj["v"].to_numpy(),
                         "splitFactor": factor.to_numpy()})


def stage_prices_alpaca(http: Optional[Http] = None, start: str = ALPACA_HISTORY_START) -> None:
    """Cours quotidiens de toutes les bourses (Alpaca, historique depuis 2016) : quelques minutes pour
    tout l'univers, réécrits à chaque passage pour intégrer les dernières séances."""
    http = http or alpaca_http()
    folder = DATA / "prices"
    folder.mkdir(parents=True, exist_ok=True)
    symbols = [MARKET_ETF, *SECTOR_ETFS, *sorted(set(cusip_ticker_map().values()))]
    written, absent = 0, []
    for k in range(0, len(symbols), 100):
        chunk = symbols[k:k + 100]
        names = [ats_symbol(s) for s in chunk]
        pulls = {adj: alpaca_query(http, "bars", names, {"timeframe": "1Day", "adjustment": adj, "start": start})
                 for adj in ("all", "split", "raw")}
        # La pagination multi-titres d'Alpaca omet parfois un titre (ex. DOW) : on le redemande seul.
        for name in [n for n in names if not pulls["all"].get(n) and re.fullmatch(r"[A-Z][A-Z.]*", n)]:
            for adj in pulls:
                pulls[adj].update(alpaca_query(http, "bars", [name], {"timeframe": "1Day", "adjustment": adj,
                                                                      "start": start}))
        for s, name in zip(chunk, names):
            if not pulls["all"].get(name):
                absent.append(s)
                continue
            df = alpaca_daily_frame(pulls["all"][name], pulls["split"].get(name, pulls["all"][name]),
                                    pulls["raw"].get(name, pulls["all"][name]))
            df.to_csv(folder / f"{s}.csv", index=False)
            written += 1
    print(f"Cours (Alpaca, depuis {start[:4]}) : {written}/{len(symbols)} symboles ; absents : {len(absent)} "
          f"{', '.join(absent[:15])}{'…' if len(absent) > 15 else ''}")


def stage_prices(http: Optional[Http] = None, start: str = "2013-01-01") -> None:
    if http is None and alpaca_keys_present():
        stage_prices_alpaca()  # rapide ; Tiingo reste utilisable sans clés Alpaca (historique depuis 2013)
        return
    http = http or tiingo_http()
    folder = DATA / "prices"
    folder.mkdir(parents=True, exist_ok=True)
    missing_path = DATA / "prices_missing.json"
    missing = set(json.load(open(missing_path))) if missing_path.exists() else set()
    symbols = [MARKET_ETF, *SECTOR_ETFS, *sorted(set(cusip_ticker_map().values()))]
    todo = [s for s in symbols if not (folder / f"{s}.csv").exists() and s not in missing]
    print(f"Cours : {len(symbols) - len(todo)}/{len(symbols)} déjà téléchargés ; "
          f"{len(todo)} restants (environ {len(todo) * 75 / 3600:.1f} h au rythme gratuit)")
    for k, symbol in enumerate(todo, start=1):
        try:
            raw = http.get(TIINGO_PRICES.format(ticker=symbol), params={"startDate": start, "format": "csv"})
        except HttpError as err:
            if err.status in (401, 403):
                sys.exit("Tiingo refuse l'accès : clé absente ou invalide (réglages de l'environnement).")
            if err.status == 404:
                missing.add(symbol)
                json.dump(sorted(missing), open(missing_path, "w"))
                continue
            raise
        if not raw.strip() or raw.lstrip()[:1] in (b"{", b"["):
            missing.add(symbol)
            json.dump(sorted(missing), open(missing_path, "w"))
            continue
        (folder / f"{symbol}.csv").write_bytes(raw)
        print(f"[{k}/{len(todo)}] {symbol}")


def load_prices(symbols: Iterable[str]) -> dict[str, pd.DataFrame]:
    out = {}
    for s in symbols:
        path = DATA / "prices" / f"{s}.csv"
        if path.exists():
            df = parse_tiingo_csv(path.read_bytes())
            if len(df):
                out[s] = df.set_index("date").sort_index()
    return out


# =============================================================================
# Étape « cot » : positions des banques (CFTC, Traders in Financial Futures)
# =============================================================================

def stage_cot(http: Optional[Http] = None) -> None:
    http = http or Http(headers={"User-Agent": "FlowFund-Research/1.0"}, min_interval=1.0)
    frames = []
    for code, label in COT_MARKETS.items():
        data = json.loads(http.get(CFTC_TFF, params={
            "$where": f"cftc_contract_market_code='{code}'", "$limit": 50000,
            "$order": "report_date_as_yyyy_mm_dd",
        }))
        df = pd.DataFrame(data)
        if df.empty:
            continue
        out = pd.DataFrame({
            "market": label,
            "report_date": to_naive_dates(df["report_date_as_yyyy_mm_dd"]).to_numpy(),
            "open_interest": pd.to_numeric(df["open_interest_all"]),
            "dealer_long": pd.to_numeric(df["dealer_positions_long_all"]),
            "dealer_short": pd.to_numeric(df["dealer_positions_short_all"]),
        })
        out["dealer_net_pct_oi"] = (out["dealer_long"] - out["dealer_short"]) / out["open_interest"]
        out["available_date"] = out["report_date"] + pd.Timedelta(days=4)  # mardi -> publié le vendredi, utilisable lundi
        frames.append(out)
    result = pd.concat(frames, ignore_index=True)
    result.to_csv(DATA / "cot_dealers.csv", index=False)
    # Les banques couvrent les achats de leurs clients : elles sont presque toujours vendeuses
    # nettes sur ces contrats. Seul l'écart à leur habitude (rang dans l'historique) est informatif.
    for market, grp in result.sort_values("report_date").groupby("market"):
        last = grp.iloc[-1]
        rank = (grp["dealer_net_pct_oi"] <= last["dealer_net_pct_oi"]).mean()
        habit = grp["dealer_net_pct_oi"].median()
        tone = ("plus vendeuses que d'habitude" if last["dealer_net_pct_oi"] < habit
                else "moins vendeuses que d'habitude")
        print(f"Banques sur {market} au {last['report_date']:%d/%m/%Y} : position nette "
              f"{last['dealer_net_pct_oi']:+.1%} des positions ouvertes (habituellement {habit:+.1%}) — "
              f"{tone} ; rang historique {rank:.0%}")


# =============================================================================
# Étape « finra » : échanges hors bourse (bourses privées, internalisation)
# =============================================================================

def parse_finra_daily(raw: bytes, symbols: set[str]) -> pd.DataFrame:
    """Fichier FINRA « Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market » -> titres suivis."""
    # keep_default_na=False : le symbole « NA » est une vraie action, pas une valeur manquante.
    df = pd.read_csv(io.BytesIO(raw), sep="|", dtype=str, keep_default_na=False)
    df = df[df["Date"].str.fullmatch(r"\d{8}", na=False)]
    df = df.assign(asset=df["Symbol"].map(tiingo_symbol))
    df = df[df["asset"].isin(symbols)]
    return pd.DataFrame({
        "date": pd.to_datetime(df["Date"], format="%Y%m%d").astype("datetime64[ns]"),
        "asset": df["asset"], "total_volume": pd.to_numeric(df["TotalVolume"], errors="coerce"),
        "short_volume": pd.to_numeric(df["ShortVolume"], errors="coerce"),
    })


def stage_finra(http: Optional[Http] = None, start: str = FINRA_START) -> None:
    http = http or Http(headers={"User-Agent": "FlowFund-Research/1.0"}, min_interval=0.2)
    folder = DATA / "finra"
    folder.mkdir(parents=True, exist_ok=True)
    symbols = set(cusip_ticker_map().values())
    today = pd.Timestamp.today().normalize()
    for month_start in pd.date_range(start, today, freq="MS"):
        path = folder / f"{month_start:%Y-%m}.parquet"
        month_end = month_start + pd.offsets.MonthEnd(0)
        if path.exists() and month_end < today - pd.Timedelta(days=3):
            continue  # mois complet déjà en cache
        frames = []
        for day in pd.bdate_range(month_start, min(month_end, today)):
            try:
                frames.append(parse_finra_daily(http.get(FINRA_DAILY.format(day=day)), symbols))
            except HttpError as err:
                if err.status not in (403, 404):  # jour férié : pas de fichier
                    raise
        if frames:
            pd.concat(frames, ignore_index=True).to_parquet(path, index=False)
            print(f"FINRA {month_start:%Y-%m} : {len(frames)} séances")


def load_finra() -> Optional[pd.DataFrame]:
    files = sorted((DATA / "finra").glob("*.parquet")) if (DATA / "finra").exists() else []
    return pd.concat([pd.read_parquet(f) for f in files], ignore_index=True) if files else None


# =============================================================================
# Étapes « names » et « buyers » : qui achète, qui vend
# =============================================================================

def stage_names(http: Optional[Http] = None) -> None:
    http = http or sec_http()
    path = DATA / "names.json"
    names = json.load(open(path)) if path.exists() else {}
    selection = pd.read_csv(DATA / "smart_money_selection.csv", dtype={"cik": str})
    for cik in sorted(set(selection["cik"]) - set(names)):
        try:
            names[cik] = json.loads(http.get(SEC_SUBMISSIONS.format(cik=int(cik)))).get("name", "")
        except HttpError:
            names[cik] = ""
    json.dump(names, open(path, "w"), indent=1, ensure_ascii=False)
    print(f"Noms : {sum(bool(v) for v in names.values())}/{len(names)} gérants identifiés")


def pretty_name(name: str) -> str:
    """« BERKSHIRE HATHAWAY INC » -> « Berkshire Hathaway »."""
    words = [w for w in re.split(r"\s+", str(name).strip()) if w]
    drop = {"INC", "INC.", "LLC", "L.P.", "LP", "LTD", "CORP", "CO", "CO.", "/DE/", "/ADV", "/MD/", "/NY/", "/CA/"}
    kept = [w for w in words if w.upper().strip(",") not in drop]
    return " ".join(w.capitalize() if w.isupper() else w for w in kept).strip(" ,")


def smart_money_buyers(positions: pd.DataFrame, selection: pd.DataFrame, names: Optional[dict] = None,
                       statutory_lag_days: int = 45, top: int = 3) -> pd.DataFrame:
    """Pour chaque action et chaque trimestre : combien de gérants de la liste ont acheté ou vendu,
    et les principaux acheteurs. Les actions sont déjà corrigées des divisions ; la donnée n'est
    utilisable qu'après l'échéance légale de publication (fin de trimestre + 45 jours + 1)."""
    names = names or {}
    pos = positions.assign(period_end=sm._quarter_end(positions["period_end"]))
    rows = []
    for q in sorted(selection["period_end"].unique()):
        managers = set(selection.loc[selection["period_end"] == q, "cik"].astype(str))
        cur = pos[(pos["period_end"] == q) & pos["cik"].astype(str).isin(managers)]
        prev = pos[(pos["period_end"] == q - sm.QUARTER) & pos["cik"].astype(str).isin(managers)]
        both = (cur.set_index(["cik", "asset"])["shares"].rename("cur").to_frame()
                .join(prev.set_index(["cik", "asset"])["shares"].rename("prev"), how="outer").fillna(0.0))
        both["delta"] = both["cur"] - both["prev"]
        value = cur.set_index(["cik", "asset"])["value"] if "value" in cur.columns else None
        for asset, grp in both.groupby(level="asset"):
            buyers = grp[grp["delta"] > 0]
            if value is not None:
                order = value.reindex(buyers.index).fillna(0).sort_values(ascending=False).index
            else:
                order = buyers.sort_values("delta", ascending=False).index
            top_names = [pretty_name(names.get(str(cik), "")) for cik, _ in list(order)[:top]]
            rows.append({
                "asset": asset, "period_end": q,
                "available_date": pd.Timestamp(q) + pd.Timedelta(days=statutory_lag_days + 1),
                "n_managers": len(managers), "n_buyers": int((grp["delta"] > 0).sum()),
                "n_sellers": int((grp["delta"] < 0).sum()),
                "top_buyers": ", ".join(n for n in top_names if n),
            })
    return pd.DataFrame(rows)


def stage_buyers() -> None:
    pos = pd.read_parquet(DATA / "positions_tracked.parquet")
    selection = pd.read_csv(DATA / "smart_money_selection.csv", dtype={"cik": str}, parse_dates=["period_end"])
    names = json.load(open(DATA / "names.json")) if (DATA / "names.json").exists() else {}
    out = smart_money_buyers(pos, selection, names)
    out.to_csv(DATA / "engine" / "smart_money_buyers.csv", index=False)
    print(f"Acheteurs et vendeurs : {len(out)} lignes (action x trimestre)")


# =============================================================================
# Étapes « ats » et « short » : bourses privées et positions vendeuses (API FINRA)
# =============================================================================

def finra_http() -> Http:
    return Http(headers={"User-Agent": "FlowFund-Research/1.0", "Accept": "application/json"}, min_interval=0.5)


def finra_query(http: Http, name: str, compare: list[dict], domain: Optional[dict] = None,
                fields: Optional[list[str]] = None, limit: int = 5000) -> list[dict]:
    """Toutes les lignes d'une requête FINRA, page par page (5 000 lignes au plus par page)."""
    rows, offset = [], 0
    while True:
        body = {"compareFilters": compare, "limit": limit, "offset": offset}
        if domain:
            body["domainFilters"] = [{"fieldName": k, "values": v} for k, v in domain.items()]
        if fields:
            body["fields"] = fields
        raw = http.request(FINRA_API.format(name=name), data=json.dumps(body).encode(),
                           headers={"Content-Type": "application/json"}, method="POST")
        page = json.loads(raw) if raw.strip() else []
        rows += page
        if len(page) < limit:
            return rows
        offset += limit


def ats_symbol(asset: str) -> str:
    """BRK-B (Tiingo) -> BRK.B (FINRA, bourses privées)."""
    return asset.replace("-", ".")


def short_symbol(asset: str) -> str:
    """BRK-B (Tiingo) -> BRKB (FINRA, positions vendeuses)."""
    return asset.replace("-", "").replace(".", "")


def aggregate_ats(records: list[dict], assets: Iterable[str]) -> pd.DataFrame:
    """Lignes FINRA (titre x plateforme x semaine) -> une ligne par titre et par semaine : volume des
    bourses privées, des plateformes de blocs et des plateformes bancaires, banques les plus actives."""
    cols = ["asset", "week_start", "published", "ats_volume", "block_volume", "bank_volume", "banks"]
    if not records:
        return pd.DataFrame(columns=cols)
    back = {ats_symbol(a): a for a in assets}
    df = pd.DataFrame(records)
    df = df.assign(asset=df["issueSymbolIdentifier"].map(back),
                   qty=pd.to_numeric(df["totalWeeklyShareQuantity"], errors="coerce").fillna(0.0))
    df = df.dropna(subset=["asset"])
    df["bank"] = df["MPID"].map(BANK_VENUES)
    df["is_block"] = df["MPID"].isin(BLOCK_VENUES)
    out = []
    for (asset, week), grp in df.groupby(["asset", "weekStartDate"]):
        banks = grp.dropna(subset=["bank"]).groupby("bank")["qty"].sum().sort_values(ascending=False)
        out.append({
            "asset": asset, "week_start": pd.Timestamp(week), "published": pd.Timestamp(grp["initialPublishedDate"].max()),
            "ats_volume": grp["qty"].sum(), "block_volume": grp.loc[grp["is_block"], "qty"].sum(),
            "bank_volume": banks.sum(), "banks": ", ".join(banks.index[:3]),
        })
    return pd.DataFrame(out, columns=cols)


def stage_ats(http: Optional[Http] = None, start: str = ATS_START) -> None:
    http = http or finra_http()
    folder = DATA / "ats"
    folder.mkdir(parents=True, exist_ok=True)
    assets = sorted(set(cusip_ticker_map().values()))
    symbols = [ats_symbol(a) for a in assets]
    today = pd.Timestamp.today().normalize()
    for week in pd.date_range(start, today - pd.Timedelta(days=14), freq="W-MON"):
        path = folder / f"{week:%Y-%m-%d}.parquet"
        if path.exists() and week < today - pd.Timedelta(days=45):
            continue  # semaine complète (niveaux 1 et 2 publiés) déjà en cache
        records = []
        for tier in ("T1", "T2"):
            records += finra_query(http, "weeklySummary", [
                {"compareType": "EQUAL", "fieldName": "weekStartDate", "fieldValue": f"{week:%Y-%m-%d}"},
                {"compareType": "EQUAL", "fieldName": "tierIdentifier", "fieldValue": tier},
                {"compareType": "EQUAL", "fieldName": "summaryTypeCode", "fieldValue": "ATS_W_SMBL_FIRM"},
            ], domain={"issueSymbolIdentifier": symbols}, fields=[
                "issueSymbolIdentifier", "MPID", "totalWeeklyShareQuantity", "weekStartDate", "initialPublishedDate"])
        if records:
            aggregate_ats(records, assets).to_parquet(path, index=False)
            print(f"Bourses privées, semaine du {week:%d/%m/%Y} : {len(records)} lignes")


def parse_short_interest(records: list[dict], assets: Iterable[str],
                         publication_days: int = SHORT_PUBLICATION_DAYS) -> pd.DataFrame:
    back = {short_symbol(a): a for a in assets}
    df = pd.DataFrame(records)
    if df.empty:
        return pd.DataFrame(columns=["asset", "settlement", "available", "short_qty", "days_to_cover"])
    out = pd.DataFrame({
        "asset": df["symbolCode"].map(back),
        "settlement": pd.to_datetime(df["settlementDate"]).astype("datetime64[ns]"),
        "short_qty": pd.to_numeric(df["currentShortPositionQuantity"], errors="coerce"),
        "days_to_cover": pd.to_numeric(df.get("daysToCoverQuantity"), errors="coerce"),
    }).dropna(subset=["asset", "short_qty"])
    out["available"] = out["settlement"] + pd.Timedelta(days=publication_days)
    return (out.drop_duplicates(["asset", "settlement"], keep="last").sort_values(["asset", "settlement"])
            [["asset", "settlement", "available", "short_qty", "days_to_cover"]].reset_index(drop=True))


def stage_short(http: Optional[Http] = None, start: str = SHORT_START) -> None:
    http = http or finra_http()
    assets = sorted(set(cusip_ticker_map().values()))
    records = []
    for k in range(0, len(assets), 100):
        chunk = [short_symbol(a) for a in assets[k:k + 100]]
        records += finra_query(http, "consolidatedShortInterest",
                               [{"compareType": "GREATER", "fieldName": "settlementDate", "fieldValue": start}],
                               domain={"symbolCode": chunk},
                               fields=["symbolCode", "settlementDate", "currentShortPositionQuantity",
                                       "daysToCoverQuantity"])
    out = parse_short_interest(records, assets)
    out.to_csv(DATA / "short_interest.csv", index=False)
    print(f"Positions vendeuses : {len(out):,} rapports, {out['asset'].nunique()} titres")


# =============================================================================
# Étapes « events » et « insiders » : résultats, seuils de 5 %, achats des dirigeants (SEC)
# =============================================================================

def submissions_frame(sub: dict) -> pd.DataFrame:
    """Bloc « recent » (ou fichier d'archive) de data.sec.gov/submissions -> tableau des dépôts."""
    block = sub.get("filings", {}).get("recent", sub)
    keys = ["accessionNumber", "filingDate", "form", "items", "primaryDocument", "acceptanceDateTime", "fileNumber"]
    n = len(block.get("accessionNumber", []))
    return pd.DataFrame({k: block.get(k, [""] * n) for k in keys})


def load_submissions(http: Http, cik: int, since: str = EVENTS_START, max_age_hours: float = 20.0) -> pd.DataFrame:
    """Tous les dépôts d'un émetteur depuis `since` (bloc récent + archives), en cache pour la journée."""
    folder = DATA / "submissions"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{cik}.parquet"
    if path.exists() and time.time() - path.stat().st_mtime < max_age_hours * 3600:
        cached = pd.read_parquet(path)
        if "fileNumber" in cached.columns:
            return cached
    sub = json.loads(http.get(SEC_SUBMISSIONS.format(cik=cik)))
    frames = [submissions_frame(sub)]
    for extra in sub.get("filings", {}).get("files", []):
        if extra.get("filingTo", "9999") >= since:
            frames.append(submissions_frame(json.loads(http.get(f"https://data.sec.gov/submissions/{extra['name']}"))))
    df = pd.concat(frames, ignore_index=True)
    df = df[df["filingDate"] >= since].drop_duplicates("accessionNumber").reset_index(drop=True)
    df.to_parquet(path, index=False)
    return df


def earnings_dates(filings: pd.DataFrame) -> list[str]:
    """Communiqués de résultats : 8-K comportant la rubrique 2.02."""
    is_8k = filings["form"].isin(["8-K", "8-K/A"])
    has_202 = filings["items"].fillna("").str.split(",").apply(lambda items: "2.02" in [i.strip() for i in items])
    return sorted(set(filings.loc[is_8k & has_202, "filingDate"]))


def filer_from_headers(text: str, issuer_cik: int) -> str:
    """Nom du déclarant dans l'en-tête EDGAR d'une 13D / 13G (hors émetteur, qui dépose parfois pour
    le compte de ses dirigeants)."""
    for block in re.split(r"FILED BY:", text)[1:]:
        name = re.search(r"COMPANY CONFORMED NAME:\s*([^\n<]+)", block)
        cik = re.search(r"CENTRAL INDEX KEY:\s*(\d+)", block)
        if name and (not cik or int(cik.group(1)) != issuer_cik):
            return pretty_name(name.group(1).replace("&amp;", "&").strip())
    return ""


def stage_events(http: Optional[Http] = None) -> None:
    http = http or sec_http()
    sectors = json.load(open(DATA / "sectors.json"))
    filers_path = DATA / "filers.json"
    filers = json.load(open(filers_path)) if filers_path.exists() else {}
    earnings, five = [], []
    for asset, info in sorted(sectors.items()):
        cik = info.get("cik")
        if not cik:
            continue
        try:
            filings = load_submissions(http, int(cik))
        except HttpError:
            continue
        earnings += [(asset, d) for d in earnings_dates(filings)]
        # Dans la liste d'un émetteur figurent aussi les 13G qu'IL dépose sur d'autres sociétés (banques,
        # gérants) : seules les déclarations portant son numéro de dossier « 005- » le concernent.
        about = filings["form"].isin(FIVE_PCT_FORMS) & filings["fileNumber"].fillna("").str.startswith("005-")
        for r in filings[about].itertuples():
            if r.accessionNumber not in filers:
                url = SEC_ARCHIVES.format(cik=int(cik), folder=r.accessionNumber.replace("-", ""),
                                          document=f"{r.accessionNumber}-index-headers.html")
                try:
                    filers[r.accessionNumber] = filer_from_headers(http.get(url).decode("utf-8", "replace"), int(cik))
                except HttpError:
                    filers[r.accessionNumber] = ""
            five.append((asset, r.filingDate, r.form.replace("SCHEDULE", "SC"), filers[r.accessionNumber]))
    json.dump(filers, open(filers_path, "w"), indent=0, ensure_ascii=False)
    pd.DataFrame(earnings, columns=["asset", "date"]).to_csv(DATA / "earnings.csv", index=False)
    pd.DataFrame(five, columns=["asset", "filing_date", "form", "filer"]).to_csv(DATA / "filings_5pct.csv", index=False)
    print(f"Événements : {len(earnings):,} publications de résultats, {len(five)} franchissements de 5 %")


ROLE_WORDS = {"Director": "administrateur", "Officer": "dirigeant", "TenPercentOwner": "actionnaire de plus de 10 %",
              "Other": "autre"}


def role_text(relationship: str, title: str = "") -> str:
    parts = [ROLE_WORDS.get(r.strip(), "") for r in str(relationship or "").split(",")]
    text = ", ".join(p for p in parts if p)
    title = str(title or "").strip()
    return f"{text} — {title}" if title and title.lower() != "nan" else text


def insider_purchases_from_zip(raw: bytes, cik_to_asset: dict[int, str]) -> pd.DataFrame:
    """Jeu trimestriel « Insider Transactions » de la SEC -> achats sur le marché (code P) des titres suivis."""
    z = zipfile.ZipFile(io.BytesIO(raw))
    read = lambda name, cols: pd.read_csv(z.open(next(n for n in z.namelist() if n.endswith(name))), sep="\t",
                                          dtype=str, usecols=cols, keep_default_na=False)
    sub = read("SUBMISSION.tsv", ["ACCESSION_NUMBER", "FILING_DATE", "DOCUMENT_TYPE", "ISSUERCIK"])
    sub = sub[sub["DOCUMENT_TYPE"].isin(["4", "4/A"])]
    sub = sub.assign(asset=pd.to_numeric(sub["ISSUERCIK"], errors="coerce").map(cik_to_asset)).dropna(subset=["asset"])
    trans = read("NONDERIV_TRANS.tsv", ["ACCESSION_NUMBER", "TRANS_CODE", "TRANS_SHARES", "TRANS_PRICEPERSHARE",
                                        "TRANS_ACQUIRED_DISP_CD"])
    trans = trans[(trans["TRANS_CODE"] == "P") & (trans["TRANS_ACQUIRED_DISP_CD"] == "A")]
    owners = read("REPORTINGOWNER.tsv", ["ACCESSION_NUMBER", "RPTOWNERNAME", "RPTOWNER_RELATIONSHIP", "RPTOWNER_TITLE"])
    owners = owners.drop_duplicates("ACCESSION_NUMBER")
    df = trans.merge(sub, on="ACCESSION_NUMBER").merge(owners, on="ACCESSION_NUMBER", how="left")
    value = pd.to_numeric(df["TRANS_SHARES"], errors="coerce") * pd.to_numeric(df["TRANS_PRICEPERSHARE"], errors="coerce")
    out = pd.DataFrame({
        "asset": df["asset"], "filing_date": pd.to_datetime(df["FILING_DATE"], format="%d-%b-%Y"),
        "owner": df["RPTOWNERNAME"].map(pretty_name),
        "role": [role_text(r, t) for r, t in zip(df["RPTOWNER_RELATIONSHIP"], df["RPTOWNER_TITLE"])],
        "value": value, "accession": df["ACCESSION_NUMBER"],
    }).dropna(subset=["value"])
    return (out.groupby(["asset", "filing_date", "owner", "role", "accession"], as_index=False)["value"].sum()
            [["asset", "filing_date", "owner", "role", "value", "accession"]])


def _xml_text(node, path: str) -> str:
    found = node.find(path)
    return (found.text or "").strip() if found is not None and found.text else ""


def insider_purchases_from_form4(xml: bytes, asset: str, filing_date: str, accession: str,
                                 issuer_cik: Optional[int] = None) -> list[dict]:
    """Form 4 au format XML -> achats sur le marché (code P). Un Form 4 déposé PAR la société sur une
    autre société (elle-même actionnaire de plus de 10 %) est écarté grâce au code de l'émetteur."""
    root = ET.fromstring(xml)
    if issuer_cik is not None:
        found = _xml_text(root, "issuer/issuerCik")
        if not found.isdigit() or int(found) != int(issuer_cik):
            return []
    owner = root.find("reportingOwner")
    name = pretty_name(_xml_text(owner, "reportingOwnerId/rptOwnerName")) if owner is not None else ""
    rel = owner.find("reportingOwnerRelationship") if owner is not None else None
    flags = []
    if rel is not None:
        for tag, word in (("isDirector", "Director"), ("isOfficer", "Officer"), ("isTenPercentOwner", "TenPercentOwner")):
            if _xml_text(rel, tag).lower() in ("1", "true"):
                flags.append(word)
    role = role_text(",".join(flags), _xml_text(rel, "officerTitle") if rel is not None else "")
    rows = []
    for t in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        if _xml_text(t, "transactionCoding/transactionCode") != "P":
            continue
        if _xml_text(t, "transactionAmounts/transactionAcquiredDisposedCode/value") != "A":
            continue
        try:
            value = float(_xml_text(t, "transactionAmounts/transactionShares/value")) * float(
                _xml_text(t, "transactionAmounts/transactionPricePerShare/value"))
        except ValueError:
            continue
        rows.append({"asset": asset, "filing_date": pd.Timestamp(filing_date), "owner": name, "role": role,
                     "value": value, "accession": accession})
    return rows


def stage_insiders(http: Optional[Http] = None, since: str = EVENTS_START) -> None:
    http = http or sec_http()
    sectors = json.load(open(DATA / "sectors.json"))
    cik_to_asset = {int(v["cik"]): a for a, v in sectors.items() if v.get("cik")}
    folder = DATA / "insiders"
    folder.mkdir(parents=True, exist_ok=True)
    # 1. Historique : jeux trimestriels (un fichier par trimestre, publié après la fin du trimestre)
    html = http.get(SEC_INSIDER_PAGE).decode("utf-8", "replace")
    urls = [urljoin(SEC_INSIDER_PAGE, h) for h in re.findall(r'href="([^"]+_form345\.zip)"', html, flags=re.I)]
    covered = pd.Timestamp(since) - pd.Timedelta(days=1)
    for url in sorted(set(urls)):
        m = re.search(r"(\d{4})q(\d)_form345", url)
        if not m:
            continue
        quarter_end = pd.Period(f"{m.group(1)}Q{m.group(2)}", freq="Q").end_time.normalize()
        if quarter_end < pd.Timestamp(since):
            continue
        path = folder / f"{m.group(1)}q{m.group(2)}.parquet"
        if not path.exists():
            insider_purchases_from_zip(http.get(url), cik_to_asset).to_parquet(path, index=False)
            print(f"Dirigeants {m.group(1)} T{m.group(2)} : jeu trimestriel traité")
        covered = max(covered, quarter_end)
    # 2. Depuis le dernier jeu publié : Form 4 déposés, lus un par un (en cache)
    seen_path = folder / "recent_seen.json"
    seen = set(json.load(open(seen_path))) if seen_path.exists() else set()
    recent_path = folder / "recent.parquet"
    recent = pd.read_parquet(recent_path).to_dict("records") if recent_path.exists() else []
    for asset, info in sorted(sectors.items()):
        cik = info.get("cik")
        if not cik:
            continue
        try:
            filings = load_submissions(http, int(cik))
        except HttpError:
            continue
        new = filings[(filings["form"] == "4") & (pd.to_datetime(filings["filingDate"]) > covered)
                      & ~filings["accessionNumber"].isin(seen)]
        for r in new.itertuples():
            document = re.sub(r"^xslF345X\d+/", "", r.primaryDocument)
            url = SEC_ARCHIVES.format(cik=int(cik), folder=r.accessionNumber.replace("-", ""), document=document)
            try:
                recent += insider_purchases_from_form4(http.get(url), asset, r.filingDate, r.accessionNumber, int(cik))
            except (HttpError, ET.ParseError) as err:  # document illisible : ignoré, signalé
                print(f"Form 4 ignoré ({asset}, {r.accessionNumber}) : {str(err)[:80]}")
            seen.add(r.accessionNumber)
    json.dump(sorted(seen), open(seen_path, "w"))
    recent_df = pd.DataFrame(recent, columns=["asset", "filing_date", "owner", "role", "value", "accession"])
    recent_df = recent_df[pd.to_datetime(recent_df["filing_date"]) > covered]
    recent_df.to_parquet(recent_path, index=False)
    history = [pd.read_parquet(f) for f in sorted(folder.glob("*q*.parquet"))]
    allp = pd.concat(history + [recent_df], ignore_index=True).drop_duplicates(["accession", "owner", "value"])
    allp.to_csv(DATA / "insiders.csv", index=False)
    print(f"Achats des dirigeants : {len(allp):,} achats sur le marché, {allp['asset'].nunique()} titres "
          f"(jeux trimestriels jusqu'au {covered:%d/%m/%Y}, Form 4 lus ensuite)")


# =============================================================================
# Étape « alpaca » : heure par heure et gros blocs (toutes les bourses, 15 minutes après)
# =============================================================================

def alpaca_credentials() -> tuple[str, str]:
    """(identifiant, secret). Un identifiant Alpaca commence par PK (essai) ou AK (réel) et est plus
    court que le secret : deux clés saisies à l'envers dans les réglages sont remises dans l'ordre."""
    key = os.environ.get("ALPACA_API_KEY_ID", "").strip()
    secret = os.environ.get("ALPACA_API_SECRET_KEY", "").strip()
    looks_like_id = lambda v: v[:2] in ("PK", "AK") and len(v) <= 32
    if key and secret and looks_like_id(secret) and not looks_like_id(key):
        key, secret = secret, key
    return key, secret


def alpaca_http() -> Http:
    key, secret = alpaca_credentials()
    if not key or not secret:
        sys.exit("ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY absents (variables d'environnement).")
    # Offre gratuite : 200 requêtes par minute.
    return Http(headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret, "Accept": "application/json",
                         "User-Agent": "FlowFund-Research/1.0"}, min_interval=0.32)


def alpaca_query(http: Http, kind: str, symbols: list[str], params: dict) -> dict[str, list[dict]]:
    """Barres ou transactions de plusieurs titres, page par page (10 000 lignes au plus par page).
    Un symbole refusé (« invalid symbol », ex. un code CUSIP resté sans symbole boursier) est retiré de
    la liste au lieu de faire échouer tout le lot ; il est simplement absent du résultat."""
    out: dict[str, list[dict]] = {}
    symbols = [s for s in symbols if re.fullmatch(r"[A-Z][A-Z0-9.]{0,9}", s) and not s[1:].isdigit()]
    token = None
    while symbols:
        query = {"symbols": ",".join(symbols), "limit": 10000, "feed": "sip", **params}
        if token:
            query["page_token"] = token
        try:
            page = json.loads(http.get(ALPACA_DATA.format(kind=kind), params=query))
        except HttpError as err:
            bad = re.search(r"invalid symbol: ([^\"\s]+)", getattr(err, "body", "") or "")
            if err.status != 400 or not bad or bad.group(1) not in symbols:
                raise
            symbols = [s for s in symbols if s != bad.group(1)]
            continue
        for sym, rows in (page.get(kind) or {}).items():
            out.setdefault(sym, []).extend(rows)
        token = page.get("next_page_token")
        if not token:
            return out
    return out


def new_york_time(values) -> pd.Series:
    """Horodatage UTC (RFC 3339) -> heure de New York, sans fuseau."""
    ts = pd.to_datetime(pd.Series(values), utc=True).dt.tz_convert("America/New_York")
    return ts.dt.tz_localize(None).astype("datetime64[ns]")


def bars_frame(raw: dict[str, list[dict]], assets: Iterable[str]) -> pd.DataFrame:
    back = {ats_symbol(a): a for a in assets}
    frames = []
    for sym, rows in raw.items():
        if rows and sym in back:
            df = pd.DataFrame(rows)
            frames.append(pd.DataFrame({"asset": back[sym], "time": new_york_time(df["t"]).to_numpy(),
                                        "high": df["h"].to_numpy(), "low": df["l"].to_numpy(),
                                        "close": df["c"].to_numpy(), "volume": df["v"].to_numpy(),
                                        "trades": df.get("n", pd.Series(np.nan, index=df.index)).to_numpy(),
                                        "vwap": df.get("vw", pd.Series(np.nan, index=df.index)).to_numpy()}))
    cols = ["asset", "time", "high", "low", "close", "volume", "trades", "vwap"]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=cols)


def regular_hours(bars: pd.DataFrame) -> pd.DataFrame:
    t = pd.to_datetime(bars["time"])
    minutes = t.dt.hour * 60 + t.dt.minute
    return bars[(minutes >= 9 * 60 + 30) & (minutes < 16 * 60)]


def hot_minutes(minute_bars: pd.DataFrame, per_asset: int = 3, volume_x: float = 5.0,
                size_x: float = 3.0) -> pd.DataFrame:
    """Minutes suspectes : volume au moins 5 fois la minute habituelle de la journée ET taille moyenne
    des transactions au moins 3 fois l'habitude (peu de transactions, mais très grosses). Les
    `per_asset` plus fortes par titre et par jour."""
    b = regular_hours(minute_bars).copy()
    if b.empty:
        return b
    b["day"] = pd.to_datetime(b["time"]).dt.normalize()
    b["avg_size"] = b["volume"] / b["trades"].where(b["trades"] > 0)
    g = b.groupby(["asset", "day"])
    b["vol_ratio"] = b["volume"] / g["volume"].transform("median")
    b["size_ratio"] = b["avg_size"] / g["avg_size"].transform("median")
    hot = b[(b["vol_ratio"] >= volume_x) & (b["size_ratio"] >= size_x)]
    return (hot.sort_values("vol_ratio", ascending=False).groupby(["asset", "day"]).head(per_asset)
            .sort_values(["asset", "time"]).reset_index(drop=True))


def block_trades(trades: list[dict], asset: str, min_notional: float = BLOCK_MIN_NOTIONAL) -> list[dict]:
    """Transactions d'une fenêtre -> gros blocs. Sens par la règle du dernier écart de prix (achat si
    le prix monte par rapport à la transaction précédente de prix différent, vente s'il baisse). Les
    transactions notées « D » passent par un système de déclaration FINRA : hors bourse."""
    rows, last_price, last_side = [], None, 0
    for tr in sorted(trades, key=lambda x: x["t"]):
        price, size = float(tr["p"]), float(tr["s"])
        if last_price is not None and price != last_price:
            last_side = 1 if price > last_price else -1
        conditions = set(tr.get("c") or [])
        if price * size >= min_notional and not conditions & BLOCK_EXCLUDED_CONDITIONS:
            t = new_york_time([tr["t"]]).iloc[0]
            rows.append({"asset": asset, "date": t.normalize(), "time": f"{t:%H:%M:%S}", "price": price,
                         "size": size, "notional": price * size,
                         "venue": "hors bourse" if tr.get("x") == "D" else "bourse", "side": float(last_side)})
        last_price = price
    return rows


def market_sessions(http: Http, days: int) -> list[pd.Timestamp]:
    """Dernières séances complètes (barres quotidiennes de SPY), la séance du jour une fois close."""
    today = pd.Timestamp.now(tz="America/New_York")
    raw = alpaca_query(http, "bars", [MARKET_ETF], {"timeframe": "1Day",
                                                   "start": f"{today - pd.Timedelta(days=days * 2 + 10):%Y-%m-%d}"})
    dates = sorted({pd.Timestamp(t).normalize() for t in new_york_time([r["t"] for r in raw.get(MARKET_ETF, [])])})
    closed = today.tz_localize(None) >= today.tz_localize(None).normalize() + pd.Timedelta(hours=16, minutes=20)
    dates = [d for d in dates if d < today.tz_localize(None).normalize() or closed]
    return [pd.Timestamp(d) for d in dates[-days:]]


def stage_alpaca(http: Optional[Http] = None, days: int = 20, hourly_sessions: int = 30) -> None:
    if http is None and not (os.environ.get("ALPACA_API_KEY_ID") and os.environ.get("ALPACA_API_SECRET_KEY")):
        print("Alpaca : clés absentes, étape ignorée (ALPACA_API_KEY_ID, ALPACA_API_SECRET_KEY).")
        return
    http = http or alpaca_http()
    assets = sorted(set(cusip_ticker_map().values()))
    symbols = [ats_symbol(a) for a in assets]
    chunks = [symbols[k:k + 100] for k in range(0, len(symbols), 100)]
    sessions = market_sessions(http, max(days, hourly_sessions))
    if not sessions:
        print("Alpaca : aucune séance trouvée")
        return
    # 1. Barres horaires des dernières séances (unité de temps « heure » du radar)
    start = sessions[-hourly_sessions] if len(sessions) >= hourly_sessions else sessions[0]
    raw: dict[str, list[dict]] = {}
    for chunk in chunks:
        for sym, rows in alpaca_query(http, "bars", chunk, {"timeframe": "1Hour", "adjustment": "split",
                                                            "start": f"{start:%Y-%m-%d}"}).items():
            raw.setdefault(sym, []).extend(rows)
    hourly = bars_frame(raw, assets)
    hourly = hourly[pd.to_datetime(hourly["time"]).dt.hour.between(9, 15)]
    hourly.drop(columns=["trades", "vwap"]).to_csv(DATA / "hourly.csv", index=False)
    print(f"Heure par heure : {len(hourly):,} barres, {hourly['asset'].nunique()} titres")
    # 2. Gros blocs : minutes suspectes, puis détail des transactions de ces minutes seulement
    folder = DATA / "blocks"
    folder.mkdir(parents=True, exist_ok=True)
    for day in sessions[-days:]:
        path = folder / f"{day:%Y-%m-%d}.parquet"
        if path.exists():
            continue
        raw = {}
        for chunk in chunks:
            for sym, rows in alpaca_query(http, "bars", chunk, {
                    "timeframe": "1Min", "start": f"{day:%Y-%m-%d}",
                    "end": f"{day + pd.Timedelta(days=1):%Y-%m-%d}"}).items():
                raw.setdefault(sym, []).extend(rows)
        hot = hot_minutes(bars_frame(raw, assets))
        found = []
        for r in hot.itertuples():
            begin = pd.Timestamp(r.time).tz_localize("America/New_York").tz_convert("UTC")
            trades = alpaca_query(http, "trades", [ats_symbol(r.asset)], {
                "start": begin.isoformat().replace("+00:00", "Z"),
                "end": (begin + pd.Timedelta(seconds=59.999)).isoformat().replace("+00:00", "Z")})
            found += block_trades(trades.get(ats_symbol(r.asset), []), r.asset)
        cols = ["asset", "date", "time", "price", "size", "notional", "venue", "side"]
        pd.DataFrame(found, columns=cols).to_parquet(path, index=False)
        print(f"Gros blocs du {day:%d/%m/%Y} : {len(hot)} minutes suspectes, {len(found)} blocs d'au moins 1 M$")
    files = sorted(folder.glob("*.parquet"))
    allb = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True) if files else pd.DataFrame()
    allb.to_csv(DATA / "blocks.csv", index=False)


def copy_radar_extras(stocks: Iterable[str], out_dir: Path) -> list[str]:
    """Recopie dans le dossier du moteur les indices complémentaires disponibles, titres suivis seulement."""
    stocks = set(stocks)
    written = []
    ats_files = sorted((DATA / "ats").glob("*.parquet")) if (DATA / "ats").exists() else []
    if ats_files:
        ats = pd.concat([pd.read_parquet(f) for f in ats_files], ignore_index=True)
        ats[ats["asset"].isin(stocks)].to_csv(out_dir / "ats.csv", index=False)
        written.append("ats")
    for table in ("short_interest", "insiders", "filings_5pct", "earnings", "blocks", "hourly"):
        src = DATA / f"{table}.csv"
        if src.exists():
            df = pd.read_csv(src, keep_default_na=False, na_values=[""])
            df = df[df["asset"].isin(stocks)].drop(columns=["accession"], errors="ignore")
            df.to_csv(out_dir / f"{table}.csv", index=False)
            written.append(table)
    return written


# =============================================================================
# Étape « build » : fichiers du moteur
# =============================================================================

def split_factors(prices: dict[str, pd.DataFrame]) -> dict[str, pd.Series]:
    """Facteur cumulé de division d'actions (Tiingo splitFactor) : 1 avant la première division."""
    out = {}
    for s, df in prices.items():
        f = df["splitFactor"] if "splitFactor" in df.columns else pd.Series(1.0, index=df.index)
        out[s] = f.fillna(1.0).replace(0, 1.0).cumprod()
    return out


def adjust_shares_for_splits(positions: pd.DataFrame, factors: dict[str, pd.Series]) -> pd.DataFrame:
    """Exprime les actions déclarées en unités d'avant division : une division 2 pour 1 entre deux
    déclarations ne doit pas passer pour un doublement des achats."""
    adj = positions.copy()
    adj["shares"] = adj["shares"].astype(float)  # une division donne des fractions d'action
    for asset, grp in adj.groupby("asset"):
        f = factors.get(asset)
        if f is None or f.empty:
            continue
        idx = f.index.searchsorted(fb._as_ns(grp["period_end"]), side="right") - 1
        fac = np.where(idx >= 0, f.to_numpy()[np.clip(idx, 0, None)], 1.0)
        adj.loc[grp.index, "shares"] = grp["shares"].to_numpy() / fac
    return adj


def splice_etf(prices: dict[str, pd.DataFrame], etf: str) -> Optional[pd.DataFrame]:
    """Prolonge dans le passé un ETF lancé tardivement par son prédécesseur sectoriel."""
    df = prices.get(etf)
    pred = prices.get(ETF_PREDECESSOR.get(etf, ""))
    if df is None:
        return pred
    if pred is None:
        return df
    return pd.concat([pred[pred.index < df.index.min()], df])


def build_engine_files(out_dir: Optional[Path] = None, lookback: int = 8, top_n: int = 50) -> dict:
    out_dir = out_dir or DATA / "engine"
    ranked = pd.read_parquet(DATA / "universe_ranked.parquet")
    n_keep = json.load(open(DATA / "universe.json"))["top_per_quarter"]
    managers = pd.read_parquet(DATA / "managers.parquet")
    managers = managers.assign(period_end=sm._quarter_end(managers["period_end"]))
    tickers = cusip_ticker_map()
    sectors = json.load(open(DATA / "sectors.json")) if (DATA / "sectors.json").exists() else {}
    stock_symbols = sorted(set(tickers[c] for c in universe_cusips(ranked, n_keep) if c in tickers))
    prices = load_prices([MARKET_ETF, *SECTOR_ETFS, *stock_symbols])
    stocks = [s for s in stock_symbols if s in prices]

    # Positions des gérants sur les titres suivis (plus grosses lignes, quel que soit le trimestre)
    tracked = set(ranked["cusip"]) & set(tickers)
    rows = []
    for path in _intermediates("rows"):
        df = pd.read_parquet(path, columns=["cik", "cusip", "period_end", "accession", "filing_date", "shares", "value"])
        rows.append(df[df["cusip"].isin(tracked)])
    pos = pd.concat(rows, ignore_index=True)
    pos = pos.assign(period_end=sm._quarter_end(pos["period_end"])).merge(
        managers[["cik", "period_end", "accession"]], on=["cik", "period_end", "accession"])
    pos = pos.assign(asset=pos["cusip"].map(tickers))
    pos = pos[pos["asset"].isin(stocks)]
    pos = (pos.groupby(["cik", "asset", "period_end"], as_index=False)
           .agg(shares=("shares", "sum"), value=("value", "sum"), filing_date=("filing_date", "max")))

    close = pd.DataFrame({s: prices[s]["adjClose"] for s in stocks})
    returns = sm.manager_quarterly_returns(pos, close, weights="value")
    counts = managers.set_index(["period_end", "cik"])["n_positions"]
    selection = sm.select_smart_money(returns, pos, lookback=lookback, top_n=top_n, position_counts=counts)
    adjusted = adjust_shares_for_splits(pos, split_factors(prices))
    adjusted.to_parquet(DATA / "positions_tracked.parquet", index=False)
    holdings = sm.smart_money_holdings_index(adjusted, selection)

    vehicles = {}
    for etf in [MARKET_ETF, *SECTOR_ETFS]:
        df = splice_etf(prices, etf)
        if df is not None:
            vehicles[etf] = df
    ohlcv = pd.concat([pd.DataFrame({"date": df.index, "vehicle": etf, "high": df["adjHigh"].to_numpy(),
                                     "low": df["adjLow"].to_numpy(), "close": df["adjClose"].to_numpy(),
                                     "volume": df["adjVolume"].to_numpy()})
                       for etf, df in vehicles.items()], ignore_index=True)
    flows, aum = sm.flows_from_ohlcv(ohlcv)

    assets = pd.DataFrame({
        "asset": stocks,
        "asset_class": "EQUITY",
        "flow_vehicle": [sectors.get(s, {}).get("etf", MARKET_ETF) for s in stocks],
        "tv_symbol": stocks,
    })
    assets.loc[~assets["flow_vehicle"].isin(vehicles), "flow_vehicle"] = MARKET_ETF

    out_dir.mkdir(parents=True, exist_ok=True)
    assets.to_csv(out_dir / "assets.csv", index=False)
    long_px = pd.concat([pd.DataFrame({"date": prices[s].index, "asset": s, "close": prices[s]["adjClose"].to_numpy(),
                                       "high": prices[s]["adjHigh"].to_numpy(), "low": prices[s]["adjLow"].to_numpy(),
                                       "volume": prices[s]["adjVolume"].to_numpy()}) for s in stocks],
                        ignore_index=True)
    long_px.to_csv(out_dir / "prices.csv", index=False)
    holdings[["asset", "period_end", "filing_date", "value"]].to_csv(out_dir / "holdings.csv", index=False)
    fl = flows.rename_axis("date").reset_index().melt(id_vars="date", var_name="vehicle", value_name="net_flow")
    au = aum.rename_axis("date").reset_index().melt(id_vars="date", var_name="vehicle", value_name="aum")
    fl.merge(au, on=["date", "vehicle"]).dropna(subset=["net_flow"]).to_csv(out_dir / "flows.csv", index=False)
    selection.to_csv(DATA / "smart_money_selection.csv", index=False)
    finra = load_finra()
    if finra is not None:
        finra[finra["asset"].isin(stocks)].to_csv(out_dir / "offexchange.csv", index=False)
    extras = copy_radar_extras(stocks, out_dir)

    summary = {
        "titres": len(stocks), "titres_sans_cours": len(stock_symbols) - len(stocks),
        "gerants_classes": int(returns["cik"].nunique()), "trimestres_de_liste": int(selection["period_end"].nunique()),
        "lignes_holdings": len(holdings), "etf": sorted(vehicles),
        "hors_bourse": finra is not None, "indices_radar": extras,
    }
    json.dump(summary, open(out_dir / "resume.json", "w"), indent=1, ensure_ascii=False)
    return summary


# =============================================================================
# Ligne de commande
# =============================================================================

def status() -> None:
    def ok(p: Path) -> str:
        return "fait" if p.exists() else "à faire"
    n_rows = len(_intermediates("rows")) if (DATA / "sec").exists() else 0
    print(f"sec       : {n_rows} archive(s) traitée(s)")
    print(f"universe  : {ok(DATA / 'universe.json')}")
    print(f"figi      : {ok(DATA / 'figi.json')}")
    print(f"sectors   : {ok(DATA / 'sectors.json')}")
    n_px = len(list((DATA / 'prices').glob('*.csv'))) if (DATA / 'prices').exists() else 0
    print(f"prices    : {n_px} fichier(s) de cours")
    n_finra = len(list((DATA / 'finra').glob('*.parquet'))) if (DATA / 'finra').exists() else 0
    print(f"finra     : {n_finra} mois")
    print(f"cot       : {ok(DATA / 'cot_dealers.csv')}")
    n_ats = len(list((DATA / 'ats').glob('*.parquet'))) if (DATA / 'ats').exists() else 0
    print(f"ats       : {n_ats} semaine(s)")
    print(f"short     : {ok(DATA / 'short_interest.csv')}")
    print(f"events    : {ok(DATA / 'filings_5pct.csv')}")
    print(f"insiders  : {ok(DATA / 'insiders.csv')}")
    n_blocks = len(list((DATA / 'blocks').glob('*.parquet'))) if (DATA / 'blocks').exists() else 0
    print(f"alpaca    : {ok(DATA / 'hourly.csv')} (heure par heure) ; gros blocs : {n_blocks} séance(s)")
    print(f"build     : {ok(DATA / 'engine' / 'resume.json')}")
    print(f"names     : {ok(DATA / 'names.json')}")
    print(f"buyers    : {ok(DATA / 'engine' / 'smart_money_buyers.csv')}")
    print(f"SEC_CONTACT_EMAIL {'défini' if os.environ.get('SEC_CONTACT_EMAIL') else 'ABSENT'} ; "
          f"TIINGO_API_KEY {'défini' if os.environ.get('TIINGO_API_KEY') else 'absent (identifiants de l environnement ?)'} ; "
          f"ALPACA {'défini' if os.environ.get('ALPACA_API_KEY_ID') and os.environ.get('ALPACA_API_SECRET_KEY') else 'ABSENT'}")


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="Circuit de données réelles de la phase 1")
    parser.add_argument("stage", choices=["status", "all", "sec", "universe", "figi", "sectors", "prices", "finra",
                                          "cot", "ats", "short", "events", "insiders", "alpaca", "build", "names",
                                          "buyers"])
    parser.add_argument("--max-symbols", type=int, default=488,
                        help="actions suivies au plus (limite gratuite Tiingo : 500 symboles par mois, ETF compris)")
    args = parser.parse_args(argv)
    DATA.mkdir(parents=True, exist_ok=True)
    stages: dict[str, Callable[[], object]] = {
        "sec": stage_sec, "universe": lambda: stage_universe(args.max_symbols), "figi": stage_figi,
        "sectors": stage_sectors, "prices": stage_prices, "finra": stage_finra, "cot": stage_cot,
        "ats": stage_ats, "short": stage_short, "events": stage_events, "insiders": stage_insiders,
        "alpaca": stage_alpaca,
        "build": lambda: print(json.dumps(build_engine_files(), indent=1, ensure_ascii=False)),
        "names": stage_names, "buyers": stage_buyers,
    }
    if args.stage == "status":
        status()
    elif args.stage == "all":
        for name, fn in stages.items():
            print(f"=== {name} ===")
            fn()
    else:
        stages[args.stage]()


if __name__ == "__main__":
    main()
