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
  prices    cours ajustés, plus hauts, plus bas, volumes et divisions d'actions (Tiingo,
            50 requêtes par heure en gratuit : plusieurs heures)             [clé Tiingo]
  finra     volumes échangés hors bourse et vendus à découvert hors bourse, titre par titre
            (fichiers FINRA « Reg SHO » quotidiens, depuis août 2018) -> radar des grands acteurs
  cot       positions des banques (« Dealer / Intermediary ») sur les contrats à terme
            E-mini S&P 500 et Nasdaq-100 (CFTC, rapport TFF)
  build     liste Smart Money, indice de détention, proxy de flux -> data/phase1/engine/
  names     noms des gérants de la liste Smart Money (SEC)                   [SEC_CONTACT_EMAIL]
  buyers    qui a acheté ou vendu chaque action, trimestre par trimestre -> fiches de trade

Identifiants (jamais dans le code ni dans la conversation) :
  SEC_CONTACT_EMAIL  adresse de contact exigée par la SEC pour tout téléchargement automatique
  TIINGO_API_KEY     clé Tiingo ; inutile si elle est enregistrée dans les « API credentials »
                     de l'environnement (en-tête Authorization: Token … ajouté automatiquement)

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
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Mapping, Optional
from urllib.parse import urlencode, urljoin

import numpy as np
import pandas as pd

import flow_backtest as fb
import smart_money as sm

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


def stage_prices(http: Optional[Http] = None, start: str = "2013-01-01") -> None:
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

    summary = {
        "titres": len(stocks), "titres_sans_cours": len(stock_symbols) - len(stocks),
        "gerants_classes": int(returns["cik"].nunique()), "trimestres_de_liste": int(selection["period_end"].nunique()),
        "lignes_holdings": len(holdings), "etf": sorted(vehicles),
        "hors_bourse": finra is not None,
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
    print(f"build     : {ok(DATA / 'engine' / 'resume.json')}")
    print(f"names     : {ok(DATA / 'names.json')}")
    print(f"buyers    : {ok(DATA / 'engine' / 'smart_money_buyers.csv')}")
    print(f"SEC_CONTACT_EMAIL {'défini' if os.environ.get('SEC_CONTACT_EMAIL') else 'ABSENT'} ; "
          f"TIINGO_API_KEY {'défini' if os.environ.get('TIINGO_API_KEY') else 'absent (identifiants de l environnement ?)'}")


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="Circuit de données réelles de la phase 1")
    parser.add_argument("stage", choices=["status", "all", "sec", "universe", "figi", "sectors", "prices", "finra",
                                          "cot", "build", "names", "buyers"])
    parser.add_argument("--max-symbols", type=int, default=488,
                        help="actions suivies au plus (limite gratuite Tiingo : 500 symboles par mois, ETF compris)")
    args = parser.parse_args(argv)
    DATA.mkdir(parents=True, exist_ok=True)
    stages: dict[str, Callable[[], object]] = {
        "sec": stage_sec, "universe": lambda: stage_universe(args.max_symbols), "figi": stage_figi,
        "sectors": stage_sectors, "prices": stage_prices, "finra": stage_finra, "cot": stage_cot,
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
