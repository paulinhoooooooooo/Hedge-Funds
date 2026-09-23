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


# ---------------------------------------------------------------------------
# Indices complémentaires du radar (FINRA, SEC)
# ---------------------------------------------------------------------------

def test_ats_weekly_aggregation_by_venue_type():
    base = {"weekStartDate": "2026-08-31", "initialPublishedDate": "2026-09-21"}
    records = [
        {**base, "issueSymbolIdentifier": "BRK.B", "MPID": "UBSA", "totalWeeklyShareQuantity": 300},
        {**base, "issueSymbolIdentifier": "BRK.B", "MPID": "SGMT", "totalWeeklyShareQuantity": 500},
        {**base, "issueSymbolIdentifier": "BRK.B", "MPID": "LQNA", "totalWeeklyShareQuantity": 200},
        {**base, "issueSymbolIdentifier": "BRK.B", "MPID": "INCR", "totalWeeklyShareQuantity": 1000},
        {**base, "issueSymbolIdentifier": "ZZZ", "MPID": "UBSA", "totalWeeklyShareQuantity": 9},
    ]
    out = p1.aggregate_ats(records, ["BRK-B"])
    row = out.iloc[0]
    assert len(out) == 1 and row["asset"] == "BRK-B"
    assert (row["ats_volume"], row["block_volume"], row["bank_volume"]) == (2000, 200, 800)
    assert row["banks"] == "Goldman Sachs, UBS"
    assert row["published"] == pd.Timestamp("2026-09-21")


def test_short_interest_symbols_and_publication_delay():
    records = [{"symbolCode": "BRKB", "settlementDate": "2026-08-31", "currentShortPositionQuantity": 1000,
                "daysToCoverQuantity": 1.5},
               {"symbolCode": "AAPL", "settlementDate": "2026-08-14", "currentShortPositionQuantity": 5}]
    out = p1.parse_short_interest(records, ["BRK-B", "AAPL"])
    brk = out[out["asset"] == "BRK-B"].iloc[0]
    assert brk["available"] == pd.Timestamp("2026-08-31") + pd.Timedelta(days=p1.SHORT_PUBLICATION_DAYS)
    assert p1.short_symbol("BRK-B") == "BRKB" and p1.ats_symbol("BRK-B") == "BRK.B"


def test_earnings_dates_and_filer_name():
    filings = pd.DataFrame({"form": ["8-K", "8-K", "10-Q", "SC 13G"],
                            "items": ["2.02,9.01", "5.02", "", ""],
                            "filingDate": ["2026-07-30", "2026-04-20", "2026-08-01", "2026-05-01"]})
    assert p1.earnings_dates(filings) == ["2026-07-30"]
    headers = ("SUBJECT COMPANY:\n COMPANY CONFORMED NAME: APPLE INC\n CENTRAL INDEX KEY: 0000320193\n"
               "FILED BY:\n COMPANY DATA:\n COMPANY CONFORMED NAME: BERKSHIRE HATHAWAY INC\n"
               " CENTRAL INDEX KEY: 0001067983\n")
    assert p1.filer_from_headers(headers, 320193) == "Berkshire Hathaway"
    own = "FILED BY:\n COMPANY CONFORMED NAME: NVIDIA CORP\n CENTRAL INDEX KEY: 0001045810\n"
    assert p1.filer_from_headers(own, 1045810) == ""


def test_insider_purchases_from_quarterly_zip_and_form4():
    sub = ("ACCESSION_NUMBER\tFILING_DATE\tDOCUMENT_TYPE\tISSUERCIK\n"
           "A1\t03-MAR-2025\t4\t0000320193\nA2\t04-MAR-2025\t4\t0000320193\nA3\t04-MAR-2025\t4\t0000000001\n")
    trans = ("ACCESSION_NUMBER\tTRANS_CODE\tTRANS_SHARES\tTRANS_PRICEPERSHARE\tTRANS_ACQUIRED_DISP_CD\n"
             "A1\tP\t100\t10.0\tA\nA1\tP\t50\t10.0\tA\nA2\tS\t100\t10.0\tD\nA3\tP\t1\t1\tA\n")
    owners = ("ACCESSION_NUMBER\tRPTOWNERNAME\tRPTOWNER_RELATIONSHIP\tRPTOWNER_TITLE\n"
              "A1\tCOOK TIMOTHY\tDirector,Officer\tCEO\nA2\tX\tOfficer\t\nA3\tY\tDirector\t\n")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("SUBMISSION.tsv", sub)
        z.writestr("NONDERIV_TRANS.tsv", trans)
        z.writestr("REPORTINGOWNER.tsv", owners)
    out = p1.insider_purchases_from_zip(buf.getvalue(), {320193: "AAPL"})
    assert len(out) == 1
    row = out.iloc[0]
    assert (row["asset"], row["owner"], row["value"]) == ("AAPL", "Cook Timothy", 1500.0)
    assert row["role"] == "administrateur, dirigeant — CEO"
    xml = b"""<ownershipDocument><issuer><issuerCik>0000320193</issuerCik></issuer><reportingOwner><reportingOwnerId><rptOwnerName>DOE JANE</rptOwnerName>
      </reportingOwnerId><reportingOwnerRelationship><isDirector>1</isDirector></reportingOwnerRelationship>
      </reportingOwner><nonDerivativeTable><nonDerivativeTransaction><transactionCoding><transactionCode>P
      </transactionCode></transactionCoding><transactionAmounts><transactionShares><value>200</value>
      </transactionShares><transactionPricePerShare><value>50</value></transactionPricePerShare>
      <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode></transactionAmounts>
      </nonDerivativeTransaction><nonDerivativeTransaction><transactionCoding><transactionCode>S</transactionCode>
      </transactionCoding></nonDerivativeTransaction></nonDerivativeTable></ownershipDocument>"""
    rows = p1.insider_purchases_from_form4(xml, "AAPL", "2026-09-01", "A9")
    assert rows == [{"asset": "AAPL", "filing_date": pd.Timestamp("2026-09-01"), "owner": "Doe Jane",
                     "role": "administrateur", "value": 10000.0, "accession": "A9"}]
    assert p1.insider_purchases_from_form4(xml, "AAPL", "2026-09-01", "A9", issuer_cik=320193) == rows
    assert p1.insider_purchases_from_form4(xml, "BRK-B", "2026-09-01", "A9", issuer_cik=1067983) == []


class FakeAlpaca:
    """Réplique de l'API Alpaca : pages successives, jeton de page suivante."""

    def __init__(self, pages):
        self.pages, self.calls = list(pages), []

    def get(self, url, params=None, headers=None):
        self.calls.append((url, dict(params or {})))
        return json.dumps(self.pages.pop(0)).encode()


def test_alpaca_pagination_merges_symbols():
    fake = FakeAlpaca([
        {"bars": {"AAPL": [{"t": "2026-09-22T13:00:00Z", "h": 1, "l": 1, "c": 1, "v": 10, "n": 2, "vw": 1}]},
         "next_page_token": "abc"},
        {"bars": {"AAPL": [{"t": "2026-09-22T14:00:00Z", "h": 2, "l": 2, "c": 2, "v": 20, "n": 2, "vw": 2}],
                  "BRK.B": [{"t": "2026-09-22T13:00:00Z", "h": 3, "l": 3, "c": 3, "v": 30, "n": 3, "vw": 3}]},
         "next_page_token": None},
    ])
    raw = p1.alpaca_query(fake, "bars", ["AAPL", "BRK.B"], {"timeframe": "1Hour"})
    assert len(raw["AAPL"]) == 2 and fake.calls[1][1]["page_token"] == "abc"
    assert fake.calls[0][1]["feed"] == "sip"
    bars = p1.bars_frame(raw, ["AAPL", "BRK-B"])
    assert set(bars["asset"]) == {"AAPL", "BRK-B"}
    assert bars["time"].iloc[0] == pd.Timestamp("2026-09-22 09:00")  # heure de New York


def test_hot_minutes_need_big_volume_and_big_trades():
    times = pd.date_range("2026-09-22 09:30", periods=390, freq="min")
    bars = pd.DataFrame({"asset": "AAPL", "time": times, "high": 1.0, "low": 1.0, "close": 1.0,
                         "volume": 1000.0, "trades": 10.0, "vwap": 1.0})
    bars.loc[100, ["volume", "trades"]] = [10_000.0, 10.0]  # peu de transactions, très grosses
    bars.loc[200, ["volume", "trades"]] = [10_000.0, 100.0]  # beaucoup de petites transactions
    hot = p1.hot_minutes(bars)
    assert list(hot["time"]) == [times[100]]


def test_block_trades_filter_conditions_and_infer_side():
    trades = [
        {"t": "2026-09-22T15:00:00Z", "p": 100.0, "s": 100, "x": "Q", "c": ["@"]},
        {"t": "2026-09-22T15:00:01Z", "p": 100.5, "s": 20_000, "x": "D", "c": ["@"]},  # 2 M$ hors bourse, hausse
        {"t": "2026-09-22T15:00:02Z", "p": 100.5, "s": 30_000, "x": "N", "c": ["@", "W"]},  # prix moyen : exclu
        {"t": "2026-09-22T15:00:03Z", "p": 100.1, "s": 15_000, "x": "N", "c": ["@"]},  # 1,5 M$ en baisse
        {"t": "2026-09-22T15:00:04Z", "p": 100.1, "s": 500, "x": "N", "c": ["@"]},
    ]
    blocks = p1.block_trades(trades, "AAPL")
    assert [(b["venue"], b["side"], b["size"]) for b in blocks] == [("hors bourse", 1.0, 20_000.0),
                                                                    ("bourse", -1.0, 15_000.0)]
    assert blocks[0]["time"] == "11:00:01" and blocks[0]["date"] == pd.Timestamp("2026-09-22")


class FakeAlpacaDaily:
    """Barres quotidiennes selon le mode de correction demandé (division 2 pour 1 le 3e jour)."""

    def get(self, url, params=None, headers=None):
        days = ["2026-09-18T04:00:00Z", "2026-09-21T04:00:00Z", "2026-09-22T04:00:00Z"]
        closes = {"raw": [200.0, 202.0, 101.5], "split": [100.0, 101.0, 101.5], "all": [99.0, 100.0, 101.5]}
        rows = [{"t": t, "o": c, "h": c + 1, "l": c - 1, "c": c, "v": 1000, "n": 10, "vw": c}
                for t, c in zip(days, closes[params["adjustment"]])]
        symbols = params["symbols"].split(",")
        return json.dumps({"bars": {s: rows for s in symbols if s != "ZZZZ"}, "next_page_token": None}).encode()


def test_alpaca_daily_prices_in_tiingo_format(tmp_path, monkeypatch):
    monkeypatch.setattr(p1, "DATA", tmp_path)
    monkeypatch.setattr(p1, "cusip_ticker_map", lambda: {"037833100": "AAPL", "999999999": "ZZZZ"})
    p1.stage_prices_alpaca(FakeAlpacaDaily())
    prices = p1.load_prices(["AAPL", "ZZZZ"])
    assert set(prices) == {"AAPL"}
    df = prices["AAPL"]
    assert list(df["adjClose"]) == [99.0, 100.0, 101.5]  # dividendes et divisions corrigés
    assert list(df["splitFactor"]) == [1.0, 1.0, 2.0]  # division le 22/09
    factors = p1.split_factors(prices)["AAPL"]
    assert factors.iloc[-1] == 2.0 and factors.iloc[0] == 1.0


def test_alpaca_keys_swapped_in_settings_are_reordered(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY_ID", "s" * 44)
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", "PK" + "X" * 24)
    assert p1.alpaca_credentials() == ("PK" + "X" * 24, "s" * 44)
    monkeypatch.setenv("ALPACA_API_KEY_ID", "PK" + "Y" * 24)
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", "t" * 44)
    assert p1.alpaca_credentials() == ("PK" + "Y" * 24, "t" * 44)
