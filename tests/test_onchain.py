import pandas as pd

import onchain


def test_weekend_flows_are_carried_to_the_next_session():
    days = pd.date_range("2026-09-18", "2026-09-22")  # vendredi -> mardi
    flows = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0], index=days)
    sessions = pd.DatetimeIndex(["2026-09-18", "2026-09-21", "2026-09-22"])
    summed = onchain.to_sessions(flows, sessions, "sum")
    assert summed.tolist() == [1.0, 2.0 + 3.0 + 4.0, 5.0]  # samedi et dimanche comptés le lundi
    last = onchain.to_sessions(flows, sessions, "last")
    assert last.tolist() == [1.0, 4.0, 5.0]
