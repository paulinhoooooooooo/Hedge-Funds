import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for sub in ("backtest", "tradingview"):
    sys.path.insert(0, str(ROOT / sub))

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _pas_de_vraie_notification(monkeypatch, tmp_path):
    """Les tests n'écrivent jamais sur le téléphone du fondateur, même si un sujet ntfy est configuré."""
    import notifier
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    monkeypatch.setattr(notifier, "TOPIC_FILE", tmp_path / "sujet_ntfy_absent.txt")
