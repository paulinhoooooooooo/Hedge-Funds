# -*- coding: utf-8 -*-
"""Tests du serveur MCP du fonds (outils en lecture seule)."""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp_server"))

import serveur_fonds as sf  # noqa: E402


@pytest.fixture(scope="module")
def state():
    return sf.load_state(seed=3)


def test_every_answer_flags_synthetic_data(state):
    held = state.review["asset"].iloc[0]
    answers = [
        sf.etat_du_fonds(state), sf.revue_des_positions(state), sf.analyser_actif(state, held),
        sf.empreinte_grands_acteurs(state, held), sf.candidats_a_l_achat(state),
        sf.journal_des_decisions(state, held, 5), sf.lister_actifs(state),
    ]
    assert all("SYNTHÉTIQUES" in a for a in answers)


def test_footprint_answer_explains_where_big_players_are(state):
    text = sf.empreinte_grands_acteurs(state, "eq_tech_1")  # insensible à la casse
    assert "zone" in text.lower() or "non disponible" in text.lower()
    assert "EQ_TECH_1" in text


def test_unknown_asset_is_reported_clearly(state):
    with pytest.raises(ValueError, match="Actif inconnu"):
        sf.analyser_actif(state, "PAS_UN_ACTIF")


def test_server_exposes_all_tools(state):
    pytest.importorskip("mcp")
    server = sf.build_server(lambda: state)
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert names == set(sf.TOOLS)
    result = asyncio.run(server.call_tool("empreinte_grands_acteurs", {"actif": "EQ_TECH_1"}))
    text = result.content[0].text if hasattr(result, "content") else str(result)
    assert "EQ_TECH_1" in text
