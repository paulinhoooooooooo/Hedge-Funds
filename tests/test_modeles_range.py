"""Zig Zag « 2 bougies », ranges, touches et alertes (règles du fondateur)."""

import numpy as np
import pandas as pd
import pytest

import modeles_range as mr
from zigzag import zigzag


def barres(closes, meche=0.2, debut="2026-01-05", freq="D"):
    """Bougies à partir des clôtures : ouverture = clôture précédente, mèches de `meche` de chaque côté."""
    c = np.asarray(closes, float)
    o = np.r_[c[0], c[:-1]]
    start = pd.date_range(debut, periods=len(c), freq=freq, tz="UTC")
    pas = start[1] - start[0] if len(start) > 1 else pd.Timedelta(days=1)
    return pd.DataFrame({"start": start, "end": start + pas, "open": o, "close": c,
                         "high": np.maximum(o, c) + meche, "low": np.minimum(o, c) - meche})


BAISSE = [105, 104, 103, 102, 101, 100]
HAUSSE = [102, 104, 106, 108, 110, 112, 114, 116, 118, 120]
REPLI = [118, 116, 114, 112, 110, 109]            # retour au milieu (110) : range validé
TOUCHE2 = [111, 113, 115, 117, 119, 121]          # mèche au-delà du haut : décalage + 2e touche
RETOUR = [119, 117, 115, 113, 111, 109]           # retour au milieu : ALERTE
SORTIE = [112, 118, 124, 130]                     # clôture au-delà de -0,3 : range mort


def test_zigzag_deux_bougies():
    df = barres(BAISSE + HAUSSE + REPLI)
    piv = zigzag(df.high, df.low, df.close)
    bas = [p for p in piv if p.type == "bas"]
    hauts = [p for p in piv if p.type == "haut"]
    # égalité de mèches (99,8 sur deux bougies) : le pivot va sur la plus récente, comme dans le Pine
    assert bas[0].prix == pytest.approx(99.8) and bas[0].barre == 6
    assert bas[0].confirme == 7                    # deux bougies vertes après le creux
    assert hauts[-1].prix == pytest.approx(120.2) and hauts[-1].barre == 16
    assert hauts[-1].confirme == 17                # deux bougies rouges après le sommet


def test_zigzag_bougie_interieure_ignoree():
    # 3e bougie verte entièrement dans la précédente : ignorée ; une seule rouge ensuite ne valide rien
    df = pd.DataFrame({"open": [10, 10, 11, 11.2, 11.5], "close": [10, 11, 11.5, 11.4, 12.5],
                       "high": [10.1, 11.1, 11.8, 11.6, 12.6], "low": [9.9, 9.9, 10.9, 11.2, 11.4]})
    piv = zigzag(df.high, df.low, df.close)
    assert [p.type for p in piv] == ["bas"]       # une seule phase haussière, aucun sommet validé


def test_range_valide_touches_et_alerte():
    df = barres(BAISSE + HAUSSE + REPLI + TOUCHE2 + RETOUR)
    premier, rng = mr.detecter_ranges(df)
    # le tout premier mouvement (105,2 -> 99,8) forme un petit range, tué par la hausse
    assert premier.mort is not None and premier.sens_mort == "haut"
    assert (rng.a.prix, rng.b.prix) == (pytest.approx(99.8), pytest.approx(120.2))
    assert rng.validation == 20                    # clôture 110, mèche 109,8 <= milieu 110
    assert rng.vivant
    assert rng.haut == pytest.approx(121.2)        # décalé sur la mèche de la 2e touche
    haut = rng.cotes["haut"]
    assert haut.touches == 2 and haut.arme
    assert haut.alerte == len(df) - 1              # retour au milieu (110,5) de la dernière bougie
    assert rng.cotes["bas"].touches == 1 and rng.cotes["bas"].alerte is None


def test_pas_d_alerte_sans_retour_au_milieu():
    df = barres(BAISSE + HAUSSE + REPLI + TOUCHE2 + [120, 119])
    rng = mr.detecter_ranges(df)[-1]
    assert rng.cotes["haut"].touches == 2
    assert not rng.cotes["haut"].arme and rng.cotes["haut"].alerte is None


def test_cloture_au_dela_de_moins_0_3_tue_le_range():
    df = barres(BAISSE + HAUSSE + REPLI + TOUCHE2 + RETOUR + SORTIE)
    rng = mr.detecter_ranges(df)[-1]
    assert rng.mort == len(df) - 1 and rng.sens_mort == "haut"
    assert rng.cotes["haut"].alerte is not None    # l'alerte avait eu lieu avant


def test_meche_au_dela_de_moins_0_3_mais_cloture_dedans_reste_valide():
    # range 99,8 - 120,2 : -0,3 = 126,32 ; mèche à 128 mais clôture à 121
    df = barres(BAISSE + HAUSSE + REPLI + [111, 113, 115, 117, 119])
    meche = pd.DataFrame([{"start": df.start.iloc[-1] + pd.Timedelta(days=1),
                           "end": df.start.iloc[-1] + pd.Timedelta(days=2),
                           "open": 119, "close": 121, "high": 128, "low": 118.8}])
    df = pd.concat([df, meche], ignore_index=True)
    rng = mr.detecter_ranges(df)[-1]
    assert rng.vivant and rng.haut == 128 and rng.cotes["haut"].touches == 2


def test_une_tendance_tue_le_range_meme_si_le_haut_se_decale():
    # montée régulière après le range : chaque mèche décale le haut, mais la limite -0,3 ne suit pas
    df = barres(BAISSE + HAUSSE + REPLI + list(range(111, 135)))
    rng = mr.detecter_ranges(df)[-1]
    assert rng.mort is not None and rng.sens_mort == "haut"


def test_seuil_zone_et_liquidite_avancent_la_touche():
    df = barres(BAISSE + HAUSSE + REPLI + [111, 113, 115, 116.5] + [115, 113, 111, 109])
    sans = mr.detecter_ranges(df)[-1]
    assert sans.cotes["haut"].touches == 1         # 116,7 n'atteint pas le haut (120,2)
    seuil = mr.Seuil(liquidite=mr.Liquidite("4h", 116.0, df.start.iloc[0]))
    avec = mr.detecter_ranges(df, seuils=lambda r: {"haut": seuil})[-1]
    assert avec.cotes["haut"].touches == 2 and avec.cotes["haut"].alerte is not None


def test_seuil_du_mauvais_cote_du_milieu_ignore():
    s = mr.Seuil(zone=mr.Zone("D", bas=105.0, haut=120.0, debut=pd.Timestamp("2026-01-01", tz="UTC")))
    assert s.prix("haut", 120.0, milieu=110.0) == 120.0
    assert s.prix("bas", 100.0, milieu=110.0) == 100.0


# --- Zones d'offre / de demande et liquidité : cascade d'unités de temps ---------------------------

def source(series: dict):
    def barres_ut(ut, debut, fin):
        df = series.get(ut)
        if df is None:
            return pd.DataFrame(columns=["start", "end", "open", "high", "low", "close"])
        return df[(df.start >= debut) & (df.start < fin)].reset_index(drop=True)
    return barres_ut


def test_zone_offre_bougie_verte_avec_grande_meche():
    jour = barres([100, 102, 104, 106])
    jour.loc[3, ["open", "close", "high", "low"]] = [104, 106, 110, 103.9]   # mèche haute 4 / 6,1
    t0, t1 = jour.start[3], jour.end[3]
    z = mr.zone_offre_demande(source({"D": jour}), ["D", "4h"], "haut", t0, t1)
    assert z.ut == "D" and (z.bas, z.haut) == (106, 110)


def test_zone_offre_descend_d_unite_de_temps():
    jour = barres([100, 102, 104, 106])                                       # mèches de 0,2 : trop petites
    t0, t1 = jour.start[3], jour.end[3]
    h4 = barres([104.5, 105, 105.5, 106, 105.8, 106], debut=t0.strftime("%Y-%m-%d"), freq="4h")
    h4.loc[3, ["open", "close", "high", "low"]] = [105.5, 105.8, 106.2, 105.4]  # mèche 0,4 / 0,8 = 50 %
    z = mr.zone_offre_demande(source({"D": jour, "4h": h4}), ["D", "4h"], "haut", t0, t1)
    assert z.ut == "4h" and (z.bas, z.haut) == (105.8, 106.2)


def test_zone_de_demande_bougie_rouge():
    jour = barres([106, 104, 102, 100])
    jour.loc[3, ["open", "close", "high", "low"]] = [102, 100, 102.1, 96]     # mèche basse 4 / 6,1
    z = mr.zone_offre_demande(source({"D": jour}), ["D"], "bas", jour.start[3], jour.end[3])
    assert (z.bas, z.haut) == (96, 100)


def test_liquidite_premier_sommet_intermediaire():
    # 4h : sommet 120 puis creux, rebond à 117 (entre 0 et 0,25 du range 100-120 : >= 115), puis milieu
    h4 = barres([118, 119, 120, 118, 116, 114, 115.5, 117, 115, 113, 111, 109], freq="4h")
    t0, t1 = h4.start[2], h4.end[2]
    liq = mr.niveau_liquidite(source({"4h": h4}), ["4h"], "haut", t0, t1, h4.end.iloc[-1], milieu=110,
                              limite=115)
    assert liq.ut == "4h" and liq.prix == pytest.approx(117.2)


def test_liquidite_trop_basse_on_descend():
    # rebond à 112 seulement (< 0,25) en 4h : rien ; jambe droite en 1h : rien non plus
    h4 = barres([118, 119, 120, 118, 116, 113, 111, 112.5, 113.5, 112, 110, 109], freq="4h")
    t0, t1 = h4.start[2], h4.end[2]
    assert mr.niveau_liquidite(source({"4h": h4}), ["4h", "1h"], "haut", t0, t1, h4.end.iloc[-1],
                               milieu=110, limite=115) is None
