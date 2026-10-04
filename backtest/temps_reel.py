# -*- coding: utf-8 -*-
"""
temps_reel.py — La page Desk sur votre ordinateur, avec le rendement de chaque action EN DIRECT
=============================================================================================

La page Desk en ligne est mise à jour chaque matin (cours de clôture de la veille). Ce petit
programme ouvre la même page dans votre navigateur, sur votre ordinateur (Windows, Mac, Linux), et
y remplace chaque minute le cours et le rendement de chaque action du portefeuille réel par le
dernier cours échangé (Alpaca, source IEX : gratuite et en direct ; hors séance, dernier cours).

Les clés Alpaca restent sur votre ordinateur : elles sont lues dans les variables d'environnement
ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY, sinon dans le fichier outputs/cles.env (dossier jamais
versionné), une ligne par clé :
    ALPACA_API_KEY_ID=...
    ALPACA_API_SECRET_KEY=...

    python backtest/temps_reel.py              # ouvre http://127.0.0.1:8765 ; Ctrl+C pour arrêter
    python backtest/temps_reel.py --mise-a-jour  # régénère d'abord la page (données du jour)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import phase1_data as p1  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "outputs" / "desk_smart_money.html"
KEYS_FILE = ROOT / "outputs" / "cles.env"
LATEST = "https://data.alpaca.markets/v2/stocks/trades/latest"
SOURCE = "Alpaca, source IEX"
CACHE_SECONDS = 30
MAX_SYMBOLS = 40
SYMBOL = re.compile(r"[A-Z][A-Z0-9.]{0,9}")


def load_local_keys(path: Path = KEYS_FILE) -> None:
    """Clés Alpaca du fichier outputs/cles.env, sans écraser des variables déjà définies."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        name, sep, value = line.partition("=")
        name, value = name.strip(), value.strip().strip('"').strip("'")
        if sep and name in ("ALPACA_API_KEY_ID", "ALPACA_API_SECRET_KEY") and value:
            os.environ.setdefault(name, value)


def clean_symbols(raw: str) -> list[str]:
    """Symboles demandés par la page : format strict, sans doublon, 40 au plus."""
    out: list[str] = []
    for s in raw.upper().split(","):
        s = s.strip()
        if SYMBOL.fullmatch(s) and s not in out:
            out.append(s)
    return out[:MAX_SYMBOLS]


class Quotes:
    """Derniers cours échangés, gardés 30 s pour ne pas solliciter Alpaca à chaque affichage."""

    def __init__(self, fetch=None):
        self._fetch = fetch or self._alpaca
        self._cache: dict[str, tuple[float, dict]] = {}
        self._lock = threading.Lock()
        self._http = None

    def _alpaca(self, symbols: list[str]) -> dict[str, dict]:
        if self._http is None:
            key, secret = p1.alpaca_credentials()
            if not key or not secret:
                raise RuntimeError("clés Alpaca absentes")
            self._http = p1.Http(headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret,
                                          "Accept": "application/json", "User-Agent": "FlowFund-Research/1.0"},
                                 retries=1)
        data = json.loads(self._http.get(LATEST, params={"symbols": ",".join(symbols), "feed": "iex"}))
        return {s: {"prix": float(t["p"]), "heure": t.get("t", "")}
                for s, t in (data.get("trades") or {}).items() if float(t.get("p") or 0) > 0}

    def get(self, symbols: list[str]) -> dict[str, dict]:
        now = time.monotonic()
        with self._lock:
            missing = [s for s in symbols if now - self._cache.get(s, (-1e9, {}))[0] > CACHE_SECONDS]
            if missing:
                fresh = self._fetch(missing)
                for s in missing:
                    if s in fresh:
                        self._cache[s] = (now, fresh[s])
            return {s: self._cache[s][1] for s in symbols if s in self._cache}


def make_handler(page: Path, quotes: Quotes):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: bytes, kind: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 — nom imposé par http.server
            url = urlparse(self.path)
            if url.path in ("/", "/index.html"):
                try:  # relue à chaque affichage : la mise à jour du matin est prise en compte
                    self._send(200, page.read_bytes(), "text/html; charset=utf-8")
                except OSError:
                    self._send(404, "Page Desk absente : lancez d'abord la mise à jour.".encode(),
                               "text/plain; charset=utf-8")
            elif url.path == "/api/cours":
                symbols = clean_symbols(parse_qs(url.query).get("symbols", [""])[0])
                try:
                    body = {"cours": quotes.get(symbols) if symbols else {}, "source": SOURCE}
                    self._send(200, json.dumps(body).encode(), "application/json")
                except Exception as err:  # noqa: BLE001 — la page garde alors les valeurs du matin
                    print(f"Cours en direct indisponibles : {type(err).__name__}", file=sys.stderr)
                    self._send(503, b'{"cours": {}}', "application/json")
            else:
                self._send(404, b"", "text/plain")

        def log_message(self, *args) -> None:  # silence : pas une ligne par requête
            pass

    return Handler


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Page Desk avec les cours en direct")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--page", default=str(PAGE))
    parser.add_argument("--mise-a-jour", action="store_true", help="régénère d'abord la page (données du jour)")
    parser.add_argument("--sans-navigateur", action="store_true")
    args = parser.parse_args(argv)
    load_local_keys()
    page = Path(args.page)
    if args.mise_a_jour:
        subprocess.run([sys.executable, str(ROOT / "backtest" / "mise_a_jour.py")], check=False)
    if not page.exists():
        sys.exit(f"Page Desk absente ({page}) : lancez « python backtest/mise_a_jour.py » ou ajoutez --mise-a-jour.")
    if not all(p1.alpaca_credentials()):
        print("Attention : clés Alpaca absentes ; la page affichera les valeurs du matin (voir outputs/cles.env).")
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(page, Quotes()))  # ordinateur seul
    url = f"http://127.0.0.1:{args.port}/"
    print(f"Page Desk en direct : {url}  (Ctrl+C pour arrêter)")
    if not args.sans_navigateur:
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
