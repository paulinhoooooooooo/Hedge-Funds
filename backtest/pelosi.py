# -*- coding: utf-8 -*-
"""
pelosi.py — Copier les transactions déclarées de Nancy Pelosi (backtest et suivi réel)
======================================================================================

Source officielle et gratuite : les déclarations de transactions (« Periodic Transaction Reports »,
STOCK Act) publiées par le greffe de la Chambre des représentants :
  * index annuel  https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{année}FD.zip
  * déclarations  https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{année}/{DocID}.pdf

Règles du portefeuille « copie » (documentées pour les fondateurs) :
  * une transaction n'est connue qu'à la date de DÉPÔT de la déclaration (jusqu'à 45 jours après) ;
    la copie achète ou vend à la clôture de la séance suivante ;
  * montant = milieu de la fourchette déclarée (ex. 1 000 001 – 5 000 000 $ -> 3 000 000 $) ;
  * achat (P) : on ajoute ce montant ; vente totale (S) : on sort toute la ligne ; vente partielle :
    on en sort la moitié ; échanges et exercices (E) : ignorés ;
  * options d'achat ([OP]) : copiées comme des actions du même titre. C'est une approximation :
    l'effet de levier et les options expirées sans valeur (pertes totales) ne sont pas reproduits ;
    la variante « actions seulement » les exclut ;
  * frais : 0,10 % du montant échangé ; la trésorerie de la copie est investie à 100 % dans les
    lignes détenues (aucun cash, sauf quand le portefeuille est vide).

La variante « date de la transaction » montre ce qu'a gagné Pelosi elle-même : elle n'est PAS
copiable (on ne connaît la transaction qu'au dépôt).

    python backtest/pelosi.py              # télécharge, calcule, écrit outputs/pelosi/
"""

from __future__ import annotations

import io
import re
import sys
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import flow_backtest as fb  # noqa: E402
import phase1_data as p1  # noqa: E402

DATA = p1.ROOT / "data" / "pelosi"
OUT = p1.ROOT / "outputs" / "pelosi"
INDEX_URL = "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.zip"
PTR_URL = "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{doc}.pdf"
FIRST_YEAR = 2014
COST = 0.001
TRANSACTION = re.compile(
    r"\(([A-Za-z.]{1,6})\)\s*\[(ST|OP)\]\s*(P|S\s*\(partial\)|S|E)\s+(\d\d/\d\d/\d{4})\s+(\d\d/\d\d/\d{4})\s+"
    r"\$([\d,.]+)(?:\s*-\s*\$([\d,]+))?", re.I)


# =============================================================================
# Téléchargement et lecture des déclarations
# =============================================================================

def filings_index(xml_texts: list[str], last_name: str = "Pelosi") -> pd.DataFrame:
    """Déclarations de transactions (FilingType P) d'un élu, depuis les index XML annuels."""
    rows = []
    for text in xml_texts:
        for member in ET.fromstring(text):
            d = {c.tag: (c.text or "").strip() for c in member}
            if d.get("Last", "").lower() == last_name.lower() and d.get("FilingType") == "P":
                rows.append({"doc_id": d["DocID"], "filing_date": pd.to_datetime(d["FilingDate"])})
    return pd.DataFrame(rows, columns=["doc_id", "filing_date"]).drop_duplicates("doc_id")


def parse_ptr(text: str) -> list[dict]:
    """Transactions sur titres cotés d'une déclaration (texte extrait du PDF)."""
    text = re.sub(r"\s+", " ", text)
    out = []
    for m in TRANSACTION.finditer(text):
        ticker, kind, tx, tx_date, _notif, low, high = m.groups()
        low_v = float(low.replace(",", ""))
        amount = (low_v + float(high.replace(",", ""))) / 2 if high else low_v
        tx = re.sub(r"\s+", " ", tx.upper())
        out.append({"ticker": ticker.upper().replace(".", "-"), "kind": kind.upper(),
                    "type": {"P": "BUY", "S": "SELL", "S (PARTIAL)": "SELL_PARTIAL"}.get(tx, "OTHER"),
                    "tx_date": pd.to_datetime(tx_date, format="%m/%d/%Y"), "amount": amount})
    return out


def download(http: Optional[p1.Http] = None, years: Optional[range] = None) -> pd.DataFrame:
    """Télécharge index et déclarations (déjà présentes : non retéléchargées), renvoie les transactions."""
    import pypdf
    http = http or p1.Http(headers={"User-Agent": "FlowFund-Research/1.0"}, min_interval=0.2)
    (DATA / "ptr").mkdir(parents=True, exist_ok=True)
    this_year = pd.Timestamp.now().year
    texts = []
    for year in years or range(FIRST_YEAR, this_year + 1):
        path = DATA / f"{year}FD.xml"
        if not path.exists() or year >= this_year - 1:  # l'année en cours change chaque jour
            raw = http.get(INDEX_URL.format(year=year))
            path.write_bytes(zipfile.ZipFile(io.BytesIO(raw if isinstance(raw, bytes) else raw.encode("latin-1")))
                             .read(f"{year}FD.xml"))
        texts.append(path.read_text(encoding="utf-8", errors="ignore"))
    index = filings_index(texts)
    if index.empty:
        raise RuntimeError("aucune déclaration de Nancy Pelosi dans l'index du greffe (format changé ?)")
    rows, readable = [], []
    for f in index.itertuples():
        pdf = DATA / "ptr" / f"{f.doc_id}.pdf"
        if not pdf.exists():
            try:
                raw = http.get(PTR_URL.format(year=f.filing_date.year, doc=f.doc_id))
            except p1.HttpError:
                readable.append(False)  # listée mais pas encore en ligne : retentée au prochain passage
                continue
            if not raw.startswith(b"%PDF"):
                readable.append(False)  # page d'erreur au lieu du PDF : non conservée
                continue
            pdf.write_bytes(raw)
        try:
            text = " ".join(page.extract_text() or "" for page in pypdf.PdfReader(pdf).pages)
        except Exception:  # noqa: BLE001 — déclaration illisible (scan)
            readable.append(False)
            continue
        readable.append(True)
        rows += [{**r, "doc_id": f.doc_id, "filing_date": f.filing_date} for r in parse_ptr(text)]
    index = index.assign(readable=readable)
    index = index.assign(n_transactions=index["doc_id"].map(pd.Series([r["doc_id"] for r in rows]).value_counts())
                         .fillna(0).astype(int))
    index.to_csv(DATA / "index.csv", index=False)
    tx = pd.DataFrame(rows, columns=["ticker", "kind", "type", "tx_date", "amount", "doc_id", "filing_date"])
    tx["doc_id"] = tx["doc_id"].astype(str)
    tx = tx.sort_values(["filing_date", "tx_date"]).reset_index(drop=True)
    tx.to_csv(DATA / "transactions.csv", index=False)
    return tx


def load_prices(tickers: list[str], start: str = p1.ALPACA_HISTORY_START) -> pd.DataFrame:
    """Cours de clôture ajustés (Alpaca), [date x titre] ; les titres introuvables sont absents."""
    http = p1.alpaca_http()
    names = {t: p1.ats_symbol(t) for t in tickers}
    closes = {}
    for k in range(0, len(tickers), 100):
        chunk = tickers[k:k + 100]
        raw = p1.alpaca_query(http, "bars", [names[t] for t in chunk],
                              {"timeframe": "1Day", "adjustment": "all", "start": start})
        for t in chunk:
            rows = raw.get(names[t])
            if rows:
                idx = p1.new_york_time([r["t"] for r in rows]).dt.normalize()
                closes[t] = pd.Series([r["c"] for r in rows], index=pd.DatetimeIndex(idx))
    return pd.DataFrame(closes).sort_index()


# =============================================================================
# Portefeuille copie
# =============================================================================

@dataclass
class CopyResult:
    equity: pd.Series      # valeur base 100
    positions: pd.Series   # nombre de lignes détenues
    trades: pd.DataFrame   # date, ticker, action, montant
    holdings: pd.Series    # poids des lignes à la dernière séance
    skipped: list[str]     # titres sans cours


def copy_portfolio(tx: pd.DataFrame, prices: pd.DataFrame, when: str = "filing",
                   start: Optional[pd.Timestamp] = None, stocks_only: bool = False) -> CopyResult:
    """Rejoue les transactions : when="filing" (copiable) ou "transaction" (non copiable).
    start : aucune transaction avant cette date (suivi réel qui démarre vide)."""
    cal = prices.index
    tx = tx[tx["type"] != "OTHER"]
    if stocks_only:
        tx = tx[tx["kind"] == "ST"]
    skipped = sorted(set(tx["ticker"]) - set(prices.columns))
    tx = tx[tx["ticker"].isin(prices.columns)].copy()
    # Date d'exécution : séance suivant le dépôt (copie) ou séance de la transaction (Pelosi elle-même)
    ref = tx["filing_date"] + pd.Timedelta(days=1) if when == "filing" else tx["tx_date"]
    pos = cal.searchsorted(ref.to_numpy())
    tx = tx.assign(exec_i=pos)[pos < len(cal)]
    if start is not None:
        tx = tx[cal[tx["exec_i"]] >= start]
    px = prices.ffill()
    rets = px.pct_change().fillna(0.0).to_numpy()
    value = np.zeros(len(prices.columns))  # valeur (en $) de chaque ligne
    col = {c: j for j, c in enumerate(prices.columns)}
    by_day = {i: g for i, g in tx.groupby("exec_i")}
    level, eq, npos, trades = 100.0, [], [], []
    for i in range(len(cal)):
        total = value.sum()
        if i > 0 and total > 0:
            growth = value * rets[i]
            level *= 1.0 + growth.sum() / total
            value = value + growth
        if i in by_day:
            before = value.sum()
            traded = 0.0
            for r in by_day[i].itertuples():
                j = col[r.ticker]
                if r.type == "BUY":
                    value[j] += r.amount
                    traded += r.amount
                    trades.append((cal[i], r.ticker, "ACHAT", r.amount))
                elif value[j] > 0:
                    cut = value[j] if r.type == "SELL" else value[j] / 2
                    value[j] -= cut
                    traded += cut
                    trades.append((cal[i], r.ticker, "VENTE" if r.type == "SELL" else "VENTE PARTIELLE", cut))
            base = max(before, value.sum())
            if base > 0:
                level *= 1.0 - COST * traded / base
        eq.append(level)
        npos.append(int((value > 0).sum()))
    total = value.sum()
    holdings = pd.Series(value / total if total > 0 else value, index=prices.columns)
    return CopyResult(equity=pd.Series(eq, index=cal, name="Pelosi"),
                      positions=pd.Series(npos, index=cal, name="positions"),
                      trades=pd.DataFrame(trades, columns=["date", "ticker", "action", "montant"]),
                      holdings=holdings[holdings > 0].sort_values(ascending=False), skipped=skipped)


def period_stats(equity: pd.Series, start: pd.Timestamp, end: Optional[pd.Timestamp] = None) -> dict:
    eq = equity[(equity.index >= start) & ((equity.index < end) if end is not None else True)].dropna()
    m = fb.compute_metrics(eq, risk_free_rate=0.02)
    return {"cagr": m["cagr"], "volatility": m["volatility"], "sharpe": m["sharpe"], "max_dd": m["max_drawdown"],
            "total": m["total_return"]}


def main(argv: Optional[list[str]] = None) -> dict:
    import smart_money as sm
    OUT.mkdir(parents=True, exist_ok=True)
    tx = download()
    tickers = sorted(set(tx["ticker"]) | {p1.MARKET_ETF})
    prices = load_prices(tickers)
    spy = prices[p1.MARKET_ETF]
    stock_prices = prices.drop(columns=[p1.MARKET_ETF])
    variants = {
        "Pelosi elle-même (date de transaction, non copiable)": copy_portfolio(tx, stock_prices, "transaction"),
        "Copie Pelosi (date de publication)": copy_portfolio(tx, stock_prices, "filing"),
        "Copie Pelosi, actions seulement": copy_portfolio(tx, stock_prices, "filing", stocks_only=True),
    }
    data = fb.load_market_from_csv(p1.DATA / "engine")
    fund = fb.run_backtest(data, sm.phase1_config(data)).equity
    curves = {name: r.equity for name, r in variants.items()}
    curves["Notre fonds (Smart Money)"] = fund
    curves["S&P 500 (SPY)"] = spy
    first = variants["Copie Pelosi (date de publication)"].positions
    start = max(first[first > 0].index[0], fund.index[0])
    rows = []
    for name, eq in curves.items():
        eq = eq.reindex(prices.index).ffill()
        full, a, b = period_stats(eq, start), period_stats(eq, start, pd.Timestamp("2022-07-01")), \
            period_stats(eq, pd.Timestamp("2022-07-01"))
        rows.append({"portefeuille": name, **full, "cagr_1": a["cagr"], "cagr_2": b["cagr"]})
    table = pd.DataFrame(rows)
    table.to_csv(OUT / "comparaison.csv", index=False)
    pd.DataFrame({k: v.reindex(prices.index).ffill() for k, v in curves.items()}).to_csv(OUT / "courbes.csv")
    tx.to_csv(OUT / "transactions.csv", index=False)
    live = copy_portfolio(tx, stock_prices, "filing", start=pd.Timestamp(sm.LIVE_START))
    live.trades.to_csv(OUT / "suivi_reel_mouvements.csv", index=False)
    print(f"Transactions lues : {len(tx)} ({tx['doc_id'].nunique()} déclarations) ; titres sans cours : "
          f"{', '.join(variants['Copie Pelosi (date de publication)'].skipped) or 'aucun'}")
    print(f"Période commune : {start:%d/%m/%Y} → {prices.index[-1]:%d/%m/%Y}")
    with pd.option_context("display.width", 200):
        print(table.to_string(index=False, float_format=lambda v: f"{v:.3f}"))
    print(f"Suivi réel depuis le {sm.LIVE_START} : {len(live.trades)} mouvement(s)")
    return {"table": table, "start": start, "variants": variants, "live": live, "tx": tx, "spy": spy,
            "index": pd.read_csv(DATA / "index.csv", parse_dates=["filing_date"], dtype={"doc_id": str})}


if __name__ == "__main__":
    main()
