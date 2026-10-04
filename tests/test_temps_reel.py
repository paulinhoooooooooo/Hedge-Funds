import json
import os
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import temps_reel as tr


def test_symbols_are_strictly_filtered():
    assert tr.clean_symbols("adi, VEEV,ADI,../etc,BRK.B,<x>,") == ["ADI", "VEEV", "BRK.B"]
    assert len(tr.clean_symbols(",".join(f"A{i}" for i in range(100)))) == tr.MAX_SYMBOLS


def test_quotes_are_cached_between_calls():
    calls = []

    def fetch(symbols):
        calls.append(list(symbols))
        return {s: {"prix": 10.0, "heure": ""} for s in symbols if s != "ZZZ"}

    q = tr.Quotes(fetch)
    assert q.get(["ADI", "ZZZ"]) == {"ADI": {"prix": 10.0, "heure": ""}}
    q.get(["ADI"])
    assert calls == [["ADI", "ZZZ"]]


def test_local_keys_file_never_overrides_environment(monkeypatch, tmp_path):
    f = tmp_path / "cles.env"
    f.write_text("ALPACA_API_KEY_ID=fichier\nALPACA_API_SECRET_KEY='secret'\nAUTRE=x\n", encoding="utf-8")
    monkeypatch.setenv("ALPACA_API_KEY_ID", "env")
    monkeypatch.delenv("ALPACA_API_SECRET_KEY", raising=False)
    monkeypatch.delenv("AUTRE", raising=False)
    tr.load_local_keys(f)
    assert os.environ["ALPACA_API_KEY_ID"] == "env"
    assert os.environ["ALPACA_API_SECRET_KEY"] == "secret"
    assert "AUTRE" not in os.environ


def test_server_serves_page_and_quotes_and_survives_source_failure(tmp_path):
    page = tmp_path / "desk.html"
    page.write_text("<p>Desk</p>", encoding="utf-8")

    def fetch(symbols):
        if "FAIL" in symbols:
            raise RuntimeError("panne")
        return {s: {"prix": 5.0, "heure": ""} for s in symbols}

    server = ThreadingHTTPServer(("127.0.0.1", 0), tr.make_handler(page, tr.Quotes(fetch)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        assert urllib.request.urlopen(base + "/").read() == b"<p>Desk</p>"
        data = json.loads(urllib.request.urlopen(base + "/api/cours?symbols=ADI,bad!").read())
        assert data["cours"] == {"ADI": {"prix": 5.0, "heure": ""}}
        try:
            urllib.request.urlopen(base + "/api/cours?symbols=FAIL")
            raise AssertionError("503 attendu")
        except urllib.error.HTTPError as err:
            assert err.code == 503
    finally:
        server.shutdown()
        server.server_close()
