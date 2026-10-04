# -*- coding: utf-8 -*-
"""
notifier.py — Notifications sur le téléphone des fondateurs (application gratuite ntfy)
=====================================================================================

Le fonds publie ses messages sur un « sujet » ntfy (https://ntfy.sh) ; les fondateurs s'y abonnent dans
l'application ntfy (iPhone / Android), sans compte. Le nom du sujet est SECRET : il n'est jamais écrit
dans le dépôt (public) ; il est transmis par la variable d'environnement NTFY_TOPIC.

    NTFY_TOPIC=... python backtest/notifier.py "Titre" "Message"
"""

from __future__ import annotations

import base64
import os
import sys
import time
import urllib.request

SERVER = "https://ntfy.sh"
MAX_BYTES = 3900  # ntfy accepte 4 096 octets par message


def _header(value: str) -> str:
    """En-tête HTTP en UTF-8 encodé (RFC 2047), compris par ntfy : accents, tirets et émojis conservés."""
    return "=?UTF-8?B?" + base64.b64encode(value.encode("utf-8")).decode("ascii") + "?="


def send(title: str, message: str, urgent: bool = False, topic: str | None = None,
         click: str | None = None, retries: int = 6, wait: float = 20.0) -> bool:
    """Envoie la notification ; renvoie False (sans lever d'erreur) si aucun sujet ou en cas d'échec."""
    topic = (topic or os.environ.get("NTFY_TOPIC", "")).strip()
    if not topic:
        return False
    body = message.encode("utf-8")
    if len(body) > MAX_BYTES:
        body = body[:MAX_BYTES].decode("utf-8", "ignore").encode("utf-8") + "\n…(suite sur la page Desk)".encode()
    headers = {"Title": _header(title), "Priority": "high" if urgent else "default",
               "Tags": "warning" if urgent else "chart_with_upwards_trend", "Markdown": "yes"}
    if click:
        headers["Click"] = click
    if os.environ.get("NTFY_TOKEN"):  # compte ntfy (facultatif) : quota propre, indépendant de l'adresse IP
        headers["Authorization"] = f"Bearer {os.environ['NTFY_TOKEN'].strip()}"
    # Le quota gratuit de ntfy.sh se compte par adresse IP ; les machines cloud partagent leurs adresses :
    # en cas de refus (429) ou de panne, on réessaie, la sortie réseau pouvant changer d'adresse.
    for attempt in range(retries):
        req = urllib.request.Request(f"{SERVER}/{topic}", data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                if 200 <= resp.status < 300:
                    return True
        except Exception as err:  # noqa: BLE001 — une notification manquée ne doit jamais casser la mise à jour
            code = getattr(err, "code", type(err).__name__)
            print(f"Notification ntfy non envoyée (essai {attempt + 1}/{retries}) : {code}", file=sys.stderr)
        if attempt < retries - 1:
            time.sleep(wait * (attempt + 1))
    return False


if __name__ == "__main__":
    title, message = (sys.argv[1], sys.argv[2]) if len(sys.argv) > 2 else ("Fonds", " ".join(sys.argv[1:]))
    sys.exit(0 if send(title, message, urgent=title.startswith("⚠")) else 1)
