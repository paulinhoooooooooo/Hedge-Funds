"""Scanner des ranges : barres (séance de New York, semaines), alertes, mémoire des envois, rapport."""

import numpy as np
import pandas as pd

import modeles_range as mr
import scanner_ranges as sr


def minutes_ny(jour: str, debut: str, fin: str, pas: int = 30) -> pd.DataFrame:
    t = pd.date_range(f"{jour} {debut}", f"{jour} {fin}", freq=f"{pas}min", tz=sr.NY, inclusive="left")
    c = np.arange(len(t), dtype=float) + 100
    return pd.DataFrame({"t": t.tz_convert("UTC"), "open": c, "high": c + 0.5, "low": c - 0.5, "close": c + 0.2})


def test_4h_actions_ancrees_sur_l_ouverture():
    df = sr.seance_actions(minutes_ny("2026-11-05", "04:00", "20:00"))   # pré et post-séance retirés
    h4 = sr.agreger(df, 240, actions=True)
    loc = h4["start"].dt.tz_convert(sr.NY)
    assert list(loc.dt.strftime("%H:%M")) == ["09:30", "13:30"]
    assert list(h4["end"].dt.tz_convert(sr.NY).dt.strftime("%H:%M")) == ["13:30", "16:00"]
    assert h4["open"].iloc[0] == df["open"].iloc[0] and h4["close"].iloc[-1] == df["close"].iloc[-1]
    assert h4["high"].iloc[0] == df["high"].iloc[:8].max()


def test_1h_actions_9h30_10h30():
    df = sr.seance_actions(minutes_ny("2026-11-05", "09:30", "16:00", pas=5))
    h1 = sr.agreger(df, 60, actions=True)
    assert len(h1) == 7 and h1["start"].dt.tz_convert(sr.NY).dt.strftime("%H:%M").iloc[1] == "10:30"


def test_4h_crypto_en_utc():
    t = pd.date_range("2026-11-05", periods=48, freq="30min", tz="UTC")
    df = pd.DataFrame({"t": t, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5})
    h4 = sr.agreger(df, 240, actions=False)
    assert len(h4) == 6 and h4["start"].dt.hour.tolist() == [0, 4, 8, 12, 16, 20]


def test_semaines_terminees_seulement():
    jours = pd.bdate_range("2026-10-26", "2026-11-04")             # lundi 26/10 -> mercredi 4/11
    rows = [{"t": pd.Timestamp(d, tz=sr.NY).tz_convert("UTC").isoformat(), "o": 1, "h": 2, "l": 0.5, "c": 1.5}
            for d in jours]
    jour = sr.journalier_actions(rows)
    sem = sr.hebdomadaire(jour, actions=True)
    assert len(sem) == 2
    maintenant = pd.Timestamp("2026-11-05 02:00", tz="UTC")
    assert len(sr.terminees(sem, maintenant)) == 1                   # la semaine en cours est exclue
    assert sr.terminees(sem, maintenant)["end"].iloc[0] == pd.Timestamp("2026-10-30 16:00", tz=sr.NY)


def ligne(actif="JNJ", ut="D", alerte=None, vivant=True, touches=2, arme=True):
    base = {"actif": actif, "classe": "action", "ut": ut, "debut_impulsion": "2026-06-22", "prix_debut": 226.4,
            "fin_impulsion": "2026-07-07", "prix_fin": 268.1, "valide_le": "2026-07-15", "haut": 282.0,
            "milieu": 254.2, "bas": 226.4, "ext_haut": 298.7, "ext_bas": 209.7, "vivant": vivant, "mort_le": "",
            "sens_mort": "", "cours": 276.2, "position": 0.1, "derniere_bougie": "2026-11-06"}
    for cote in mr.COTES:
        a = alerte if cote == "haut" else None
        base.update({f"{cote}_touches": touches if cote == "haut" else 1, f"{cote}_retour_milieu": arme,
                     f"{cote}_alerte_le": a.strftime("%Y-%m-%d") if a is not None else "", f"{cote}_alerte_ts": a,
                     f"{cote}_zone_bas": 265.9, f"{cote}_zone_haut": 268.1, f"{cote}_zone_ut": "D",
                     f"{cote}_liquidite": np.nan, f"{cote}_liquidite_ut": ""})
    return base


MAINTENANT = pd.Timestamp("2026-11-08 01:30", tz="UTC")


def test_alertes_recentes_et_ranges_vivants_seulement():
    recente = ligne(alerte=MAINTENANT - pd.Timedelta(days=2))
    ancienne = ligne(actif="AAPL", alerte=MAINTENANT - pd.Timedelta(days=20))
    morte = ligne(actif="MSFT", alerte=MAINTENANT - pd.Timedelta(days=1), vivant=False)
    sans = ligne(actif="NVDA")
    out = sr.alertes([recente, ancienne, morte, sans], MAINTENANT)
    assert [a["actif"] for a in out] == ["JNJ"]
    assert out[0]["modele"] == "distribution" and out[0]["id"] == "JNJ|D|haut|2026-06-22|2026-07-07"


def test_une_alerte_n_est_envoyee_qu_une_fois():
    a = sr.alertes([ligne(alerte=MAINTENANT - pd.Timedelta(days=1))], MAINTENANT)
    assert sr.nouvelles_alertes(a, {}) == a
    etat = sr.etat_suivant({}, a, MAINTENANT)
    assert sr.nouvelles_alertes(a, etat) == []
    # au bout de 90 jours la mémoire est purgée
    plus_tard = sr.etat_suivant(etat, [], MAINTENANT + pd.Timedelta(days=sr.GARDE_ALERTES + 1))
    assert plus_tard["alertes_envoyees"] == {}


def test_rapport_et_notification():
    al = ligne(alerte=MAINTENANT - pd.Timedelta(days=1))
    attente = ligne(actif="AAPL", ut="4h", touches=2, arme=False)
    nouvelles = sr.alertes([al], MAINTENANT)
    texte = sr.rapport([al, attente], nouvelles, MAINTENANT, 2, [])
    assert "Alertes : 2 touches + retour au milieu" in texte
    assert "**JNJ** | D | distribution" in texte and "🆕" in texte
    assert "**AAPL** | 4h | distribution" in texte                   # 2e touche, retour au milieu attendu
    assert "zone 265,90–268,10 (D)" in texte
    titre, corps = sr.message_notification(nouvelles, "https://exemple/rapport")
    assert titre == "Ranges : 1 modèle à 2 touches"
    assert "JNJ" in corps and "milieu 254,20" in corps and "https://exemple/rapport" in corps


def test_prix_lisibles():
    assert sr._p(126262.032) == "126 262,03"
    assert sr._p(0.00001234) == "0,00001234"
