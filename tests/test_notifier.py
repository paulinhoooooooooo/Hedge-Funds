import notifier


def test_no_topic_means_no_network_call(monkeypatch):
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    called = []
    monkeypatch.setattr(notifier.urllib.request, "urlopen", lambda *a, **k: called.append(a))
    assert notifier.send("Fonds", "texte") is False and not called


def test_message_is_sent_with_title_priority_and_limited_size(monkeypatch):
    sent = {}

    class Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake(req, timeout):
        sent.update(url=req.full_url, body=req.data, headers=dict(req.header_items()))
        return Resp()

    monkeypatch.setattr(notifier.urllib.request, "urlopen", fake)
    assert notifier.send("⚠️ Fonds — mise à jour INCOMPLÈTE", "é" * 5000, urgent=True, topic="sujet-secret")
    assert sent["url"] == "https://ntfy.sh/sujet-secret"
    assert sent["headers"]["Priority"] == "high"
    assert len(sent["body"]) < 4096
    import base64
    title = sent["headers"]["Title"]
    assert title.startswith("=?UTF-8?B?")
    assert base64.b64decode(title[10:-2]).decode() == "⚠️ Fonds — mise à jour INCOMPLÈTE"


def test_network_failure_never_breaks_the_update(monkeypatch):
    def boom(*a, **k):
        raise OSError("réseau coupé")
    monkeypatch.setattr(notifier.urllib.request, "urlopen", boom)
    assert notifier.send("Fonds", "texte", topic="x") is False
