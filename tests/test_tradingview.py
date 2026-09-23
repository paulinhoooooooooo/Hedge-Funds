# -*- coding: utf-8 -*-
"""Tests de la passerelle TradingView (watchlist, Pine) et du récepteur webhook."""

import json
import re
import threading
import urllib.error
import urllib.request

import pandas as pd
import pytest

import flow_backtest as fb
import tradingview_bridge as tv
import webhook_receiver as wr


@pytest.fixture(scope="module")
def run():
    data = fb.generate_synthetic_market(seed=5)
    data.assets["tv_symbol"] = ""
    data.assets.loc["FX_EURUSD", "tv_symbol"] = "FX:EURUSD"
    result = fb.run_backtest(data, fb.StrategyConfig())
    return result, fb.review_positions(result)


def test_watchlist_format(run):
    result, review = run
    text = tv.build_watchlist(result, review)
    assert text.startswith("###LIGNES ACTIVES,")
    tokens = text.strip().split(",")
    active = [a for a in result.assets.index if result.scales[a].iloc[-1] > 0]
    for a in active:
        assert tv.tv_symbol(result.assets, a) in tokens


def test_tv_symbol_mapping(run):
    result, _ = run
    assert tv.tv_symbol(result.assets, "FX_EURUSD") == "FX:EURUSD"
    assert tv.tv_symbol(result.assets, "CRY_BTC") == "CRY_BTC"  # repli sur le code interne


def test_pine_overlay_arrays_are_consistent(run):
    result, review = run
    asset = review["asset"].iloc[0]
    script = tv.build_pine_overlay(result, review, asset, synthetic=True)
    assert script.startswith("//@version=6")
    assert "DONNÉES SYNTHÉTIQUES" in script
    n_events = min(len(result.journal[result.journal["asset"] == asset]), tv.MAX_PINE_EVENTS)
    dates = re.search(r"evDate = array\.from\(([^)]*)\)", script).group(1).split(",")
    codes = re.search(r"evCode = array\.from\(([^)]*)\)", script).group(1).split(",")
    assert len(dates) == len(codes) == n_events
    assert all(len(d.strip()) == 8 for d in dates)


def test_pine_string_escaping():
    assert tv._pine_str('a "b" \\ c') == '"a \\"b\\" \\\\ c"'
    assert len(tv._pine_str("x" * 1000)) <= tv.MAX_TOOLTIP_CHARS + 2


def test_export_writes_one_script_per_active_line(run, tmp_path):
    result, review = run
    out = tv.export_tradingview(result, review, tmp_path, synthetic=True)
    assert (out / "watchlist_fonds.txt").exists()
    assert len(list((out / "pine").glob("*.pine"))) == len(review)


@pytest.fixture()
def server(tmp_path):
    log = tmp_path / "alertes.csv"
    srv = wr.serve("secret-de-test-assez-long", log, host="127.0.0.1", port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/", log
    srv.shutdown()


def _post(url, payload):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status
    except urllib.error.HTTPError as err:
        return err.code


def test_webhook_accepts_valid_alert_and_rejects_bad_secret(server):
    url, log = server
    ok = {"secret": "secret-de-test-assez-long", "signal": "DISTRIBUTION_PROXY", "symbol": "=HYPERLINK(\"x\")",
          "cmf": -0.12, "close": 101.5}
    assert _post(url, ok) == 200
    assert _post(url, {**ok, "secret": "mauvais"}) == 403
    rows = pd.read_csv(log)
    assert len(rows) == 1
    assert rows.loc[0, "signal"] == "DISTRIBUTION_PROXY"
    assert rows.loc[0, "symbol"].startswith("'=")  # injection de formule neutralisée
    assert rows.loc[0, "cmf"] == -0.12


def test_webhook_requires_long_secret(tmp_path):
    with pytest.raises(ValueError):
        wr.serve("court", tmp_path / "x.csv", host="127.0.0.1", port=0)
