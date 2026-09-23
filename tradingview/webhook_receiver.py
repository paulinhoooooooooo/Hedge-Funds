#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
webhook_receiver.py — Réception des alertes webhook TradingView -> journal d'alertes du fonds
=============================================================================================

Maillon de l'« outil d'alerte automatisé » du Data Scientist : les alertes de
smart_money_flow_monitor.pine (ou de tout script envoyant le même JSON) sont vérifiées,
horodatées et ajoutées à un CSV lu par l'équipe de recherche lors des revues de position.
Une alerte TradingView est une CONFIRMATION (proxy volume) : elle ne déclenche aucun ordre.

Contraintes TradingView : le webhook doit être joignable en HTTPS sur le port 443
(ou HTTP sur le port 80) et répondre en moins de 3 secondes. En production, placer ce
service derrière un reverse proxy TLS (Caddy, nginx) et restreindre l'accès aux adresses
IP d'émission publiées par TradingView.

Usage :
    TV_WEBHOOK_SECRET="un-secret-long" python tradingview/webhook_receiver.py --port 8080 \
        --log alertes_tradingview.csv

Dépendances : bibliothèque standard uniquement.
"""

from __future__ import annotations

import argparse
import csv
import hmac
import json
import os
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

FIELDS = ["received_at", "signal", "symbol", "timeframe", "close", "cmf", "volume_z", "bar_time", "source"]
MAX_BODY_BYTES = 16_384


def _clean(value) -> str:
    """Valeur tronquée et neutralisée contre l'injection de formules dans un tableur."""
    text = str(value)[:200].replace("\r", " ").replace("\n", " ")
    return "'" + text if text[:1] in ("=", "+", "@", "\t") else text


def make_handler(secret: str, log_path: Path) -> type[BaseHTTPRequestHandler]:
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def _reply(self, status: int, payload: dict) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:  # noqa: N802 (nom imposé par http.server)
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_BODY_BYTES:
                return self._reply(413, {"error": "taille de message invalide"})
            try:
                payload = json.loads(self.rfile.read(length))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return self._reply(400, {"error": "JSON invalide"})
            if not isinstance(payload, dict) or not hmac.compare_digest(str(payload.get("secret", "")), secret):
                return self._reply(403, {"error": "secret invalide"})
            row = {k: _clean(payload.get(k, "")) for k in FIELDS}
            row["received_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            with lock:
                new_file = not log_path.exists()
                with log_path.open("a", newline="", encoding="utf-8") as fh:
                    writer = csv.DictWriter(fh, fieldnames=FIELDS)
                    if new_file:
                        writer.writeheader()
                    writer.writerow(row)
            return self._reply(200, {"status": "ok"})

        def log_message(self, fmt: str, *args) -> None:  # journal HTTP silencieux
            pass

    return Handler


def serve(secret: str, log_path: Path, host: str = "0.0.0.0", port: int = 8080) -> ThreadingHTTPServer:
    if len(secret) < 16:
        raise ValueError("TV_WEBHOOK_SECRET doit contenir au moins 16 caractères")
    return ThreadingHTTPServer((host, port), make_handler(secret, Path(log_path)))


def main() -> None:
    parser = argparse.ArgumentParser(description="Récepteur des alertes webhook TradingView")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--log", default="alertes_tradingview.csv")
    args = parser.parse_args()
    secret = os.environ.get("TV_WEBHOOK_SECRET", "")
    server = serve(secret, Path(args.log), args.host, args.port)
    print(f"Écoute sur {args.host}:{args.port} -> {args.log}")
    server.serve_forever()


if __name__ == "__main__":
    main()
