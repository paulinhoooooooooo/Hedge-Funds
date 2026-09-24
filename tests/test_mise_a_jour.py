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
