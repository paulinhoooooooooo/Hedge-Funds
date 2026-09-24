import dataclasses

import pandas as pd

import flow_backtest as fb
import mise_a_jour as mj


def test_notification_lists_only_the_last_session_moves():
    data = fb.generate_synthetic_market(start="2018-01-02", end="2020-12-31", seed=3)
    result = fb.run_backtest(data, fb.StrategyConfig())
    last = result.equity.index[-1]
    journal = result.journal.copy()
    journal.loc[journal.index[-1], "date"] = last  # une décision prise à la dernière séance
    result = dataclasses.replace(result, journal=journal)
    text = mj.notification(result, ["finra"], result.benchmark)
    move = journal.iloc[-1]
    assert f"séance du {last:%d/%m/%Y}" in text
    assert "1 mouvement(s)" in text and f"{mj.MOVES[move['action']]} {move['asset']}" in text
    assert "finra" in text


def test_notification_without_moves():
    data = fb.generate_synthetic_market(start="2018-01-02", end="2020-12-31", seed=3)
    result = fb.run_backtest(data, fb.StrategyConfig())
    result = dataclasses.replace(result, journal=result.journal.iloc[0:0])
    assert "Aucun mouvement" in mj.notification(result, [], pd.Series(dtype=float).reindex(result.equity.index))


def _live_result():
    data = fb.generate_synthetic_market(start="2018-01-02", end="2020-12-31", seed=3)
    return fb.run_backtest(data, fb.StrategyConfig())


def test_state_round_trip_through_the_page(tmp_path):
    page = tmp_path / "desk.html"
    page.write_text("<title>Desk</title><p>contenu</p>", encoding="utf-8")
    mj.embed_state(page, {"derniere_seance": "2026-09-24", "pelosi_docs": ["1", "2"]})
    mj.embed_state(page, {"derniere_seance": "2026-09-25", "pelosi_docs": ["1", "2", "3"]})  # remplacé, pas empilé
    assert page.read_text(encoding="utf-8").count("etat-desk") == 1
    assert mj.read_state(page) == {"derniere_seance": "2026-09-25", "pelosi_docs": ["1", "2", "3"]}
    assert mj.read_state(tmp_path / "absente.html") == {}


def test_missed_evening_is_caught_up_without_duplicates():
    result = _live_result()
    cal = result.equity.index
    journal = result.journal.copy()
    journal.loc[journal.index[-2], "date"] = cal[-3]  # mouvement d'un soir manqué
    journal.loc[journal.index[-1], "date"] = cal[-1]
    result = dataclasses.replace(result, journal=journal)
    moves, first = mj.new_moves(result, {"derniere_seance": f"{cal[-4]:%Y-%m-%d}"})
    assert len(moves) == 2 and first == cal[-3]
    moves, _ = mj.new_moves(result, {"derniere_seance": f"{cal[-1]:%Y-%m-%d}"})  # déjà signalé / jour férié
    assert moves.empty


def test_incomplete_update_announces_no_move():
    result = _live_result()
    text = mj.notification(result, ["finra"], result.benchmark, complete=False)
    assert "INCOMPLÈTE" in text and "finra" in text and "ACHAT" not in text


def test_new_pelosi_filings_use_seen_documents():
    index = pd.DataFrame({"doc_id": ["1", "2", "3"], "filing_date": pd.to_datetime(["2026-01-02"] * 3),
                          "readable": [True, True, False], "n_transactions": [1, 0, 0]})
    tx = pd.DataFrame({"doc_id": ["1"], "ticker": ["BE"], "kind": ["ST"], "type": ["BUY"],
                       "tx_date": pd.to_datetime(["2026-01-01"]), "amount": [1e6]})
    fresh, unreadable = mj.new_pelosi(tx, index, {"pelosi_docs": ["1"]}, pd.Timestamp("2026-01-05"))
    assert fresh.empty and set(unreadable["doc_id"]) == {"2", "3"}
    fresh, _ = mj.new_pelosi(tx, index, {}, pd.Timestamp("2026-01-02"))  # premier passage : par date
    assert list(fresh["ticker"]) == ["BE"]


def test_stale_prices_are_detected():
    now = pd.Timestamp("2026-09-24 18:00", tz="America/New_York")
    assert mj.stale_days(pd.Timestamp("2026-09-24"), now) == 0
    assert mj.stale_days(pd.Timestamp("2026-09-18"), now) > mj.MAX_STALE_DAYS


def test_total_failure_is_reported_never_silent(tmp_path, monkeypatch):
    monkeypatch.setattr(mj.p1, "DATA", tmp_path / "vide")  # aucun fichier moteur
    monkeypatch.setattr(mj, "OUT", tmp_path / "outputs")
    monkeypatch.setattr(mj, "PAGE", tmp_path / "outputs" / "desk.html")
    assert mj.main(["--skip-download"]) == 1
    text = (tmp_path / "outputs" / "notification.md").read_text(encoding="utf-8")
    assert "ÉCHEC" in text
    assert '"complet": false' in (tmp_path / "outputs" / "etat_mise_a_jour.json").read_text(encoding="utf-8")
    assert not (tmp_path / "outputs" / "desk.html").exists()  # la page de la veille n'est pas remplacée
