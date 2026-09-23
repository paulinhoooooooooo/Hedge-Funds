# -*- coding: utf-8 -*-
"""Tests du circuit de données réelles de la phase 1, sur des répliques des formats officiels."""

import io
import json
import zipfile

import numpy as np
import pandas as pd
import pytest

import flow_backtest as fb
import phase1_data as p1


# ---------------------------------------------------------------------------
# Répliques des formats (SEC, OpenFIGI, Tiingo)
# ---------------------------------------------------------------------------

def sec_date(ts) -> str:
    return pd.Timestamp(ts).strftime("%d-%b-%Y").upper()


def make_13f_zip(path, filings, holdings):
    """filings : (accession, cik, type, filing_date, period) ; holdings : (accession, cusip, name, value, shares[, type, putcall])."""
    sub = ["ACCESSION_NUMBER\tFILING_DATE\tSUBMISSIONTYPE\tCIK\tPERIODOFREPORT"]
    sub += [f"{a}\t{sec_date(f)}\t{t}\t{c:010d}\t{sec_date(p)}" for a, c, t, f, p in filings]
    info = ["ACCESSION_NUMBER\tINFOTABLE_SK\tNAMEOFISSUER\tTITLEOFCLASS\tCUSIP\tVALUE\tSSHPRNAMT\tSSHPRNAMTTYPE\tPUTCALL"]
    for k, h in enumerate(holdings):
        a, cusip, name, value, shares = h[:5]
        kind = h[5] if len(h) > 5 else "SH"
        putcall = h[6] if len(h) > 6 else ""
        info.append(f"{a}\t{k}\t{name}\tCOM\t{cusip}\t{value}\t{shares}\t{kind}\t{putcall}")
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("2020q1_form13f/SUBMISSION.tsv", "\n".join(sub) + "\n")
        z.writestr("2020q1_form13f/INFOTABLE.tsv", "\n".join(info) + "\n")


def tiingo_csv(dates, close, split=None, volume=1e6) -> bytes:
    close = np.asarray(close, float)
    split = np.ones(len(dates)) if split is None else np.asarray(split, float)
    df = pd.DataFrame({
        "date": [d.strftime("%Y-%m-%d") for d in dates], "close": close, "high": close * 1.01,
        "low": close * 0.99, "open": close, "volume": volume, "adjClose": close, "adjHigh": close * 1.01,
        "adjLow": close * 0.99, "adjOpen": close, "adjVolume": volume, "divCash": 0.0, "splitFactor": split,
    })
    return df.to_csv(index=False).encode()


class FakeHttp:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url, params=None, headers=None):
        self.calls.append(url)
        for key, value in self.routes.items():
            if key in url:
                return value(url, params) if callable(value) else value
        raise p1.HttpError(404, url)

    def post_json(self, url, payload):
        self.calls.append(url)
        return self.routes["openfigi"](payload)


# ---------------------------------------------------------------------------
# Briques unitaires
# ---------------------------------------------------------------------------

def test_sic_to_sector_etf():
    assert p1.sic_to_etf(3674) == "XLK"  # semi-conducteurs
    assert p1.sic_to_etf(2834) == "XLV"  # pharmacie
    assert p1.sic_to_etf(6022) == "XLF"  # banques
    assert p1.sic_to_etf(6798) == "XLRE"  # foncières (REIT)
    assert p1.sic_to_etf(None) == "SPY"


def test_value_unit_change_2023():
    v = p1.value_in_dollars(pd.Series([5.0, 5000.0]), pd.Series(pd.to_datetime(["2022-11-14", "2023-02-14"])))
    assert list(v) == [5000.0, 5000.0]


def test_ticker_format_for_tiingo():
    assert p1.tiingo_symbol("BRK/B") == "BRK-B"


def test_list_13f_zips_resolves_relative_links():
    html = b'<a href="/files/structureddata/data/form-13f-data-sets/01mar2024-31may2024_form13f.zip">x</a>' \
           b'<a href="/other/report.zip">y</a>'
    http = FakeHttp({"form-13f-data-sets": html})
    urls = p1.list_13f_zips(http)
    assert urls == ["https://www.sec.gov/files/structureddata/data/form-13f-data-sets/01mar2024-31may2024_form13f.zip"]


def test_process_zip_applies_point_in_time_rules(tmp_path):
    q = pd.Timestamp("2020-03-31")
    make_13f_zip(tmp_path / "a.zip", [
        ("A1", 1, "13F-HR", "2020-05-10", q),
        ("A2", 2, "13F-HR", "2020-06-30", q),  # tardif : exclu
        ("A3", 1, "13F-HR/A", "2020-05-20", q),  # amendement : exclu
    ], [
        ("A1", "111111111", "ALPHA", 100, 10), ("A1", "111111111", "ALPHA", 50, 5),  # deux lignes du même titre
        ("A1", "222222222", "BETA", 30, 3, "SH", "Call"),  # option : exclue
        ("A1", "333333333", "BOND", 30, 3, "PRN"),  # obligation : exclue
        ("A2", "111111111", "ALPHA", 999, 99), ("A3", "111111111", "ALPHA", 999, 99),
    ])
    rows, stats = p1.process_13f_zip(tmp_path / "a.zip")
    assert len(rows) == 1
    assert rows.iloc[0]["shares"] == 15 and rows.iloc[0]["cik"] == "1"
    assert stats.iloc[0]["n_positions"] == 1


def test_universe_ranks_by_dollar_value_and_respects_symbol_budget():
    rows = pd.DataFrame({
        "cik": ["1", "1", "1", "2"], "cusip": ["AAAAAA101", "BBBBBB102", "CCCCCC103", "CCCCCC103"],
        "period_end": pd.to_datetime(["2022-09-30"] * 3 + ["2022-12-31"]),
        "filing_date": pd.to_datetime(["2022-11-10"] * 3 + ["2023-02-10"]),
        "accession": ["X", "X", "X", "Y"], "value": [30.0, 20.0, 10.0, 5000.0], "name": ["a", "b", "c", "c"],
    })
    stats = rows.groupby(["cik", "period_end", "accession", "filing_date"], as_index=False).size()
    ranked, n_keep = p1.build_universe(rows, stats, max_symbols=2, store_top=10, min_filers=1)
    first = ranked[ranked["period_end"] == "2022-09-30"].sort_values("rank")
    assert list(first["cusip"]) == ["AAAAAA101", "BBBBBB102", "CCCCCC103"]
    assert first["value_usd"].iloc[0] == 30_000.0  # milliers de dollars avant 2023
    assert ranked[ranked["period_end"] == "2022-12-31"]["value_usd"].iloc[0] == 5000.0
    assert n_keep == 1  # 2 premiers par trimestre -> A, B, C : dépasse le budget de 2 symboles


def test_universe_drops_bonds_and_funds_and_resists_unit_errors():
    # Trimestre 2020 : VALUE en milliers. Le gérant 3 déclare par erreur en dollars (x 1000).
    rows = pd.DataFrame({
        "cik": ["1", "2", "3", "1", "2"],
        "cusip": ["AAAAAA101", "AAAAAA101", "AAAAAA101", "BBBBBBAB1", "78462F103"],
        "period_end": pd.to_datetime(["2020-03-31"] * 5), "filing_date": pd.to_datetime(["2020-05-10"] * 5),
        "accession": ["X1", "X2", "X3", "X1", "X2"],
        "shares": [100, 100, 100, 10_000, 1_000],
        "value": [10.0, 10.0, 10_000.0, 999_999.0, 300.0],  # 100 actions à 100 $ = 10 milliers
        "name": ["ALPHA INC", "ALPHA INC", "ALPHA INC", "ALPHA INC NOTE 1% 2025", "SPDR S&P 500 ETF TR"],
    })
    stats = rows.groupby(["cik", "period_end", "accession", "filing_date"], as_index=False).size()
    ranked, _ = p1.build_universe(rows, stats, max_symbols=10, min_filers=1)
    assert p1.build_universe(rows, stats, max_symbols=10, min_filers=4)[0].empty
    assert list(ranked["cusip"]) == ["AAAAAA101"]  # obligation (numéro d'émission AB) et ETF exclus
    assert ranked["value_usd"].iloc[0] == 30_000.0  # 300 actions x prix médian 100 $


def test_issuer_name_fallback_when_openfigi_has_no_us_listing():
    http = FakeHttp({"openfigi": lambda payload: [
        {"data": [{"ticker": "XOM", "exchCode": "OU"}]},  # cotation étrangère seulement
        {"data": [{"ticker": "CELG", "exchCode": "SE"}, {"ticker": "CELG", "exchCode": "UW"}]},
        {"warning": "No identifier found."}]})
    cache = p1.map_cusips(["30231G102", "151020104", "999999109"], http, {},
                          names={"30231G102": "EXXON MOBIL CORP", "999999109": "GONE CORP"},
                          sec_listing={"EXXONMOBIL": "XOM"})
    assert cache["30231G102"]["ticker"] == "XOM" and cache["30231G102"]["source"] == "sec-name"
    assert cache["151020104"]["ticker"] == "CELG"  # Nasdaq (UW) accepté sans composite
    assert cache["999999109"] is None
    # Second code d'un émetteur déjà résolu (action de préférence) : pas de rapprochement par nom
    http = FakeHttp({"openfigi": lambda payload: [{"warning": "No identifier found."}] * len(payload)})
    cache = p1.map_cusips(["060505682"], http, {"060505104": {"ticker": "BAC"}},
                          names={"060505682": "BK OF AMERICA CORP"}, sec_listing={"BANKOFAMERICA": "BAC"})
    assert cache["060505682"] is None
    assert p1.normalize_issuer("Exxon Mobil Corp.") == p1.normalize_issuer("EXXONMOBIL CORP") == "EXXONMOBIL"
    assert p1.normalize_issuer("HONEYWELL INTL INC") == p1.normalize_issuer("Honeywell International Inc")
    assert p1.normalize_issuer("ACCENTURE PLC IRELAND") == p1.normalize_issuer("Accenture plc")


def test_openfigi_mapping_keeps_us_listing_and_caches():
    def figi(payload):
        out = []
        for job in payload:
            if job["idValue"] == "084670702":
                out.append({"data": [{"ticker": "BRK/B", "exchCode": "US", "name": "BERKSHIRE"},
                                     {"ticker": "BRK/B", "exchCode": "UN", "name": "BERKSHIRE"}]})
            else:
                out.append({"warning": "No identifier found."})
        return out
    http = FakeHttp({"openfigi": figi})
    cache = p1.map_cusips(["084670702", "38259P508"], http, {})
    assert cache["084670702"]["ticker"] == "BRK/B" and cache["38259P508"] is None
    p1.map_cusips(["084670702"], http, cache)
    assert len(http.calls) == 1  # déjà en cache : aucune nouvelle requête


def test_split_adjustment_neutralises_share_splits():
    dates = pd.bdate_range("2020-01-01", "2020-12-31")
    split = np.where(dates == pd.Timestamp("2020-08-31"), 4.0, 1.0)  # division 4 pour 1
    prices = {"AAA": p1.parse_tiingo_csv(tiingo_csv(dates, np.full(len(dates), 100.0), split)).set_index("date")}
    pos = pd.DataFrame({"cik": ["1", "1"], "asset": ["AAA", "AAA"],
                        "period_end": pd.to_datetime(["2020-06-30", "2020-09-30"]), "shares": [100.0, 400.0]})
    adj = p1.adjust_shares_for_splits(pos, p1.split_factors(prices))
    assert adj["shares"].tolist() == [100.0, 100.0]  # aucun achat : la division ne compte pas


def test_prices_stage_skips_cached_and_records_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(p1, "DATA", tmp_path)
    (tmp_path / "figi.json").write_text(json.dumps({"1": {"ticker": "AAA"}, "2": {"ticker": "ZZZ"}}))
    dates = pd.bdate_range("2020-01-01", periods=5)
    http = FakeHttp({"/AAA/": tiingo_csv(dates, [1, 2, 3, 4, 5]), "/SPY/": tiingo_csv(dates, [1, 2, 3, 4, 5])})
    p1.stage_prices(http)
    assert (tmp_path / "prices" / "AAA.csv").exists()
    assert "ZZZ" in json.loads((tmp_path / "prices_missing.json").read_text())
    n_calls = len(http.calls)
    p1.stage_prices(http)
    assert len(http.calls) == n_calls  # tout est en cache ou déjà marqué absent


# ---------------------------------------------------------------------------
# Circuit complet : archives SEC -> fichiers du moteur -> backtest
# ---------------------------------------------------------------------------

def test_end_to_end_build_feeds_the_engine(tmp_path, monkeypatch):
    monkeypatch.setattr(p1, "DATA", tmp_path)
    rng = np.random.default_rng(0)
    quarters = pd.date_range("2016-03-31", "2019-12-31", freq="QE")
    stocks = {"AAA": "100000001", "BBB": "100000002", "CCC": "100000003"}
    fillers = [f"9{k:08d}" for k in range(22)]
    filings, holdings = [], []
    for qi, q in enumerate(quarters):
        for m in range(1, 31):
            acc = f"{m}-{qi}"
            filings.append((acc, m, "13F-HR", q + pd.Timedelta(days=30), q))
            for t, cusip in stocks.items():
                trend = 1 + (0.03 * qi if (t == "AAA" and m <= 10) else 0.0)
                holdings.append((acc, cusip, t, int(1000 * trend), int(100 * trend * (1 + rng.uniform(0, 0.01)))))
            holdings += [(acc, f, "FILLER", 10, 10) for f in fillers]
    (tmp_path / "sec" / "intermediate").mkdir(parents=True)
    make_13f_zip(tmp_path / "all.zip", filings, holdings)
    rows, stats = p1.process_13f_zip(tmp_path / "all.zip")
    rows.to_parquet(tmp_path / "sec" / "intermediate" / "all.rows.parquet", index=False)
    stats.to_parquet(tmp_path / "sec" / "intermediate" / "all.managers.parquet", index=False)

    p1.stage_universe(max_symbols=30)
    (tmp_path / "figi.json").write_text(json.dumps({c: {"ticker": t} for t, c in stocks.items()}))
    (tmp_path / "sectors.json").write_text(json.dumps({t: {"cik": 1, "sic": 3674, "etf": "XLK"} for t in stocks}))
    (tmp_path / "prices").mkdir()
    dates = pd.bdate_range("2015-06-01", "2020-12-31")
    for t in [*stocks, "SPY", "XLK"]:
        path = np.cumprod(1 + rng.normal(0.0004, 0.01, len(dates))) * 100
        (tmp_path / "prices" / f"{t}.csv").write_bytes(
            tiingo_csv(dates, path, volume=rng.uniform(5e5, 2e6, len(dates))))

    summary = p1.build_engine_files()
    assert summary["titres"] == 3 and summary["trimestres_de_liste"] > 0
    data = fb.load_market_from_csv(tmp_path / "engine")
    assert set(data.assets["flow_vehicle"]) == {"XLK"}
    holdings_aaa = data.holdings[data.holdings["asset"] == "AAA"].sort_values("period_end")
    assert holdings_aaa["value"].iloc[-1] > holdings_aaa["value"].iloc[0]  # les gérants accumulent AAA
    result = fb.run_backtest(data, fb.StrategyConfig(entry_flow_threshold=0.0, exit_flow_threshold=-0.05))
    assert np.isfinite(result.metrics["sharpe"])
