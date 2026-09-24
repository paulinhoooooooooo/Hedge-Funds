#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
flow_backtest.py — Backtest « Moyen/Long-Term Flow Trading » (Smart Money & Exit Management)
============================================================================================

Objet
-----
Valider la stratégie de suivi des flux institutionnels à moyen/long terme, sur toute
classe d'actifs (actions, devises, matières premières, crypto-monnaies) :

* ACHAT  : convergence de deux « jambes » de flux
    - jambe LENTE (positionnement) : hausse de la détention des institutions de référence
      (13F pour les actions, COT pour FX / matières premières, on-chain pour la crypto) ;
    - jambe RAPIDE (flux de fonds) : flux net positif sur le véhicule associé
      (ETF sectoriel, fonds EPFR, ETP, exchange netflows) sur 30 jours.
* VENTE  : inversion des flux, indépendamment des stop-loss de prix, en trois niveaux
    - niveau 1 « alerte distribution » : une seule jambe se retourne -> allègement progressif ;
    - niveau 2 « distribution confirmée » : les deux jambes se retournent (ou liquidation
      institutionnelle massive) -> sortie totale ;
    - niveau 3 « risque » (Risk Manager) : stop de perte critique par ligne ou coupe-circuit
      de drawdown du portefeuille -> exécution accélérée.
* BIAIS  : chaque donnée n'est visible qu'à sa date de publication légale/technique
  (13F : fin de trimestre + 45 j ; COT : mardi + 3 j ; flux ETF / on-chain : J+1), plus un
  jour de traitement. Les ordres sont exécutés au plus tôt le lendemain du signal.
* RISQUE : Sharpe, Sortino, Max Drawdown, Recovery Time, Calmar, VaR / CVaR, turnover.

Architecture (une section par étape)
------------------------------------
  1. Paramétrage   : SourceSpec (par classe d'actifs), StrategyConfig
  2. Données       : MarketData ; generate_synthetic_market() ; load_market_from_csv() ;
                     export_market_to_csv() ; load_13f_positions() et
                     build_13f_holdings_from_sec() (jeux SEC DERA)
  3. Point-in-time : build_pit_institutional_change() applique les délais de publication
  4. Signaux       : compute_signals() — vectorisé, sans état
  5. Moteur        : run_backtest() — boucle quotidienne avec état (positions, exécution
                     fractionnée VWAP/TWAP, contrôles du Risk Manager, journal d'arbitrages)
  6. Métriques     : compute_metrics(), trade_statistics()
  7. Revue         : review_positions() — Matrice prédictive 3 horizons + justification
                     + Exit Signal pour 100 % des lignes actives
  8. Rapport       : print_report(), save_outputs(), plot_results(), main()

Usage
-----
    python backtest/flow_backtest.py                       # démo, données synthétiques
    python backtest/flow_backtest.py --data-dir data/      # vos données (schéma : load_market_from_csv)
    python backtest/flow_backtest.py --export-synthetic data_demo/   # gabarits CSV
    python backtest/flow_backtest.py --tradingview         # + watchlist et indicateurs Pine
    python backtest/flow_backtest.py --multi-seed 8        # robustesse sur 8 mondes synthétiques
    python backtest/flow_backtest.py --help

Dépendances : numpy, pandas (matplotlib optionnel pour le graphique).

AVERTISSEMENT : les résultats obtenus sur données synthétiques valident la mécanique
(absence de biais d'anticipation, déclenchement des sorties, calcul des métriques) ;
ils ne constituent en aucun cas une preuve de performance sur données réelles.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Iterable, Mapping, Optional

import numpy as np
import pandas as pd

from institutional_radar import ALERT_THRESHOLD, BANK_VENUES, Radar, RadarExtras, compute_radar
from market_footprint import Footprint, compute_footprint, describe_footprint


# =============================================================================
# 1. PARAMÉTRAGE
# =============================================================================

@dataclass(frozen=True)
class SourceSpec:
    """Source « Smart Money » (jambe lente) et hypothèses de coût d'une classe d'actifs."""

    name: str
    report_freq_days: int  # périodicité des rapports de détention (jours calendaires)
    statutory_lag_days: int  # délai légal / technique entre la date d'arrêté et la publication
    cost_bps: float  # coût de transaction aller simple estimé (commission + spread + impact)


ASSET_CLASS_SPECS: dict[str, SourceSpec] = {
    # Form 13F : dépôt au plus tard 45 jours après la fin du trimestre civil (Rule 13f-1).
    "EQUITY": SourceSpec("SEC Form 13F", 91, 45, 10.0),
    # Commitments of Traders : positions du mardi, publiées le vendredi par la CFTC.
    "FX": SourceSpec("CFTC COT (Asset Managers / Leveraged Funds)", 7, 3, 2.0),
    "COMMODITY": SourceSpec("CFTC COT (Managed Money)", 7, 3, 5.0),
    # On-chain (Glassnode / CryptoQuant) : la journée J est disponible en J+1.
    "CRYPTO": SourceSpec("On-chain (Long-Term Holders / wallets institutionnels)", 1, 1, 20.0),
}

# Tolérance (jours) pour retrouver le rapport de référence ~io_lookback_days plus tôt :
# les fins de trimestre sont espacées de 90 à 92 jours, les COT de 7 jours.
LOOKBACK_TOLERANCE_DAYS = 5


@dataclass(frozen=True)
class StrategyConfig:
    """Paramètres de la stratégie. Chaque seuil est un choix de gestion à valider par le CIO."""

    # --- Jambe lente : détention des institutions de référence (13F / COT / on-chain)
    io_lookback_days: int = 91  # variation mesurée sur ~1 trimestre, quelle que soit la source
    entry_io_change: float = 0.03  # +3 % de détention => accumulation
    exit_io_change: float = -0.03  # -3 % => les institutions allègent (alerte)
    exit_io_change_hard: float = -0.10  # -10 % => liquidation institutionnelle (sortie seule)
    processing_lag_days: int = 1  # délai de traitement interne après publication
    ignore_publication_lags: bool = False  # True UNIQUEMENT pour mesurer le biais d'anticipation

    # --- Jambe rapide : flux de fonds sur le véhicule associé (ETF, fonds, ETP, exchanges)
    flow_window: str = "30D"  # fenêtre glissante en jours calendaires
    flow_window_short: str = "7D"  # vélocité court terme (Matrice prédictive, horizon jours)
    flow_rule: str = "cumulative"  # "cumulative" : somme 30 j > seuil ; "consecutive" : chaque jour > 0
    entry_flow_threshold: float = 0.0  # flux net 30 j / encours > 0 %
    exit_flow_threshold: float = -0.02  # flux sortant massif : < -2 % de l'encours sur 30 j
    flow_publication_lag: int = 1  # observations (jours de cotation) avant publication des flux

    # --- Troisième jambe : empreinte de marché des grands acteurs (prix / volume, market_footprint.py)
    use_footprint: bool = True  # actif dès que les données contiennent plus haut, plus bas et volume
    # --- Radar des grands acteurs (heure, jour, semaine, mois ; institutional_radar.py)
    use_radar: bool = True  # classe les candidats ; interdit d'acheter pendant une distribution détectée

    # --- Gestion des positions
    min_holding_days: int = 15  # horizon minimal : pas de sortie « flux » avant 15 jours
    reentry_cooldown_days: int = 20  # pas de ré-achat (ni de renforcement) juste après une sortie / un allègement
    partial_exit_fraction: float = 0.5  # niveau 1 : on conserve 50 % de la ligne
    max_positions: int = 12
    sizing: str = "inverse_vol"  # "inverse_vol" (budget de risque) ou "equal"
    risk_budget_per_position: float = 0.02  # vol annualisée cible apportée par chaque ligne
    vol_lookback: int = 63
    max_weight: float = 0.15
    min_weight: float = 0.005
    max_gross: float = 1.0  # long-only, sans levier

    # --- Exécution (Execution Trader) : ordres fractionnés sur plusieurs séances
    execution_days: int = 5
    urgent_execution_days: int = 2
    cost_multiplier: float = 1.0

    # --- Risk Manager : garde-fous indépendants des signaux de flux (None = désactivé)
    position_stop_loss: Optional[float] = 0.25  # perte critique minimale vs prix de revient
    stop_vol_multiple: float = 0.75  # seuil élargi à 0.75 x vol annualisée à l'entrée (crypto : ~50 %)
    max_weight_drift: float = 1.5  # une ligne > 1.5 x max_weight est ramenée à max_weight
    portfolio_dd_limit: Optional[float] = 0.20  # coupe-circuit drawdown du portefeuille
    derisk_fraction: float = 0.5
    derisk_cooldown_days: int = 63

    # --- Divers
    initial_capital: float = 100_000_000.0
    cash_rate: float = 0.02  # rémunération de la trésorerie (annuelle)
    # Trésorerie non investie placée dans l'indice (SPY) plutôt qu'en cash ; exige MarketData.market.
    # Le coupe-circuit drawdown la repasse en cash pendant sa durée.
    idle_cash_in_market: bool = False
    market_cost: float = 0.0002  # coût d'achat / vente de l'indice (2 points de base)
    risk_free_rate: float = 0.02  # taux sans risque pour Sharpe / Sortino

    def __post_init__(self) -> None:
        if self.flow_rule not in ("cumulative", "consecutive"):
            raise ValueError("flow_rule doit valoir 'cumulative' ou 'consecutive'")
        if self.sizing not in ("inverse_vol", "equal"):
            raise ValueError("sizing doit valoir 'inverse_vol' ou 'equal'")
        if not self.exit_io_change_hard <= self.exit_io_change < 0 < self.entry_io_change:
            raise ValueError("seuils attendus : exit_io_change_hard <= exit_io_change < 0 < entry_io_change")
        if not 0 <= self.partial_exit_fraction < 1:
            raise ValueError("partial_exit_fraction doit être dans [0, 1)")
        if self.execution_days < 1 or self.urgent_execution_days < 1:
            raise ValueError("les durées d'exécution doivent être >= 1")


ACTION_LABELS = {
    "ENTRY": "Achat — accumulation détectée",
    "ADD_REACCUMULATION": "Renforcement — ré-accumulation confirmée",
    "REDUCE_DISTRIBUTION_ALERT": "Allègement progressif — alerte distribution (niveau 1)",
    "EXIT_DISTRIBUTION_CONFIRMED": "Sortie — distribution confirmée (niveau 2)",
    "EXIT_INSTITUTIONAL_LIQUIDATION": "Sortie — liquidation institutionnelle (niveau 2)",
    "RISK_STOP": "Sortie urgente — perte critique sur la ligne (niveau 3)",
    "RISK_DRAWDOWN": "Dé-risquage — coupe-circuit drawdown portefeuille (niveau 3)",
    "RISK_TRIM": "Écrêtage — limite de concentration par ligne (Risk Manager)",
}


# =============================================================================
# 2. DONNÉES
# =============================================================================

@dataclass
class MarketData:
    """Conteneur des données d'entrée (toutes les dates en index sont des dates d'observation).

    prices   : DataFrame [date x actif]     cours de clôture (NaN = pas de cotation ce jour)
    holdings : DataFrame long               colonnes asset, period_end, filing_date, value
               value = niveau (> 0) détenu par les institutions de référence à la date
               d'arrêté period_end (actions détenues 13F, positions longues COT, offre LTH...).
               filing_date = date de dépôt effective si connue (NaT sinon).
    flows    : DataFrame [date x véhicule]  flux nets quotidiens (devise)
    aum      : DataFrame [date x véhicule]  encours du véhicule (devise)
    assets   : DataFrame index=actif        colonnes asset_class, flow_vehicle
    high, low, volume : DataFrame [date x actif], optionnels — nécessaires à l'empreinte de marché
    offexchange, offexchange_short : DataFrame [date x actif], optionnels — volume échangé hors
               bourse et volume vendu à découvert hors bourse (fichiers FINRA « Reg SHO »)
    extras   : RadarExtras, optionnel — autres indices gratuits du radar (bourses privées, positions
               vendeuses déclarées, achats des dirigeants, franchissements de 5 %, dates de résultats,
               gros blocs)
    market   : Series [date], optionnel — cours ajusté de l'indice (SPY) où placer la trésorerie
               inutilisée (StrategyConfig.idle_cash_in_market)
    hourly   : DataFrame long, optionnel — asset, time, high, low, close, volume : barres horaires des
               dernières séances (heure de New York), pour l'unité de temps « heure » du radar
    """

    prices: pd.DataFrame
    holdings: pd.DataFrame
    flows: pd.DataFrame
    aum: pd.DataFrame
    assets: pd.DataFrame
    high: Optional[pd.DataFrame] = None
    low: Optional[pd.DataFrame] = None
    volume: Optional[pd.DataFrame] = None
    offexchange: Optional[pd.DataFrame] = None
    offexchange_short: Optional[pd.DataFrame] = None
    extras: Optional[RadarExtras] = None
    hourly: Optional[pd.DataFrame] = None
    market: Optional[pd.Series] = None  # cours de l'indice (SPY), pour placer la trésorerie inutilisée

    @property
    def has_ohlcv(self) -> bool:
        return self.high is not None and self.low is not None and self.volume is not None

    def validate(self) -> None:
        missing_px = set(self.assets.index) - set(self.prices.columns)
        if missing_px:
            raise ValueError(f"Actifs sans prix : {sorted(missing_px)}")
        bad_cls = set(self.assets["asset_class"]) - set(ASSET_CLASS_SPECS)
        if bad_cls:
            raise ValueError(f"Classes d'actifs inconnues : {sorted(bad_cls)} (attendu : {list(ASSET_CLASS_SPECS)})")
        missing_v = set(self.assets["flow_vehicle"]) - set(self.flows.columns)
        if missing_v:
            raise ValueError(f"Véhicules de flux sans données : {sorted(missing_v)}")
        missing_aum = set(self.assets["flow_vehicle"]) - set(self.aum.columns)
        if missing_aum:
            raise ValueError(f"Véhicules de flux sans encours : {sorted(missing_aum)}")
        required = {"asset", "period_end", "filing_date", "value"}
        if not required.issubset(self.holdings.columns):
            raise ValueError(f"holdings doit contenir les colonnes {sorted(required)}")
        if (self.holdings["value"].dropna() <= 0).any():
            raise ValueError("holdings.value doit être strictement positif (niveau de détention)")


def _as_ns(values) -> pd.DatetimeIndex:
    """Normalise des dates en datetime64[ns] (pandas 3 infère parfois la microseconde)."""
    return pd.DatetimeIndex(pd.to_datetime(values)).as_unit("ns")


def _simulate_regimes(rng: np.random.Generator, n_t: int, n_a: int) -> np.ndarray:
    """Régimes latents Smart Money (semi-markoviens) : -1 distribution, 0 neutre, +1 accumulation."""
    mean_duration = {1: 180, 0: 130, -1: 130}  # jours ouvrés
    transitions = {0: ([1, -1], [0.55, 0.45]), 1: ([0, -1], [0.7, 0.3]), -1: ([0, 1], [0.7, 0.3])}
    regimes = np.zeros((n_t, n_a), dtype=np.int8)
    for a in range(n_a):
        state = int(rng.choice([-1, 0, 1], p=[0.25, 0.5, 0.25]))
        t = 0
        while t < n_t:
            duration = int(rng.geometric(1.0 / mean_duration[state]))
            regimes[t:t + duration, a] = state
            t += duration
            nxt, prob = transitions[state]
            state = int(rng.choice(nxt, p=prob))
    return regimes


def generate_synthetic_market(start: str = "2012-01-02", end: str = "2025-06-30", seed: int = 7) -> MarketData:
    """Génère un marché multi-actifs synthétique, reproductible, où le Smart Money a un avantage.

    Processus générateur (volontairement simple et documenté) :
      * un régime latent par actif (accumulation / neutre / distribution, durées de 6 à 9 mois) ;
      * la détention institutionnelle croît pendant l'accumulation et décroît pendant la
        distribution ; elle n'est observée qu'aux dates de rapport (13F trimestriel, COT
        hebdomadaire, on-chain quotidien) ;
      * les flux des véhicules réagissent immédiatement au régime, mais sont très bruités ;
      * les prix réagissent AVEC RETARD (moyenne exponentielle du régime, demi-vie 40 jours) :
        c'est l'hypothèse centrale du flow trading (les flux précèdent les prix) ;
      * les grands acteurs laissent une empreinte dans les volumes (plus de volume les jours
        de hausse en accumulation, les jours de baisse en distribution) et dans les mèches des
        chandeliers. Cette empreinte est tirée d'un générateur aléatoire séparé : les cours de
        clôture restent identiques avec ou sans elle.
    """
    rng = np.random.default_rng(seed)
    cal = _as_ns(pd.bdate_range(start, end))
    n_t = len(cal)

    universe: list[tuple[str, str, str]] = []
    for sector in ("TECH", "SANTE", "ENERGIE"):
        universe += [(f"EQ_{sector}_{k}", "EQUITY", f"ETF_{sector}") for k in range(1, 5)]
    universe += [(f"FX_{p}", "FX", f"FLUX_{p}") for p in ("EURUSD", "USDJPY", "GBPUSD", "AUDUSD")]
    universe += [(f"CMD_{c}", "COMMODITY", f"ETC_{c}") for c in ("OR", "PETROLE", "CUIVRE", "BLE")]
    universe += [(f"CRY_{c}", "CRYPTO", f"ETP_{c}") for c in ("BTC", "ETH", "SOL", "ADA")]
    assets = pd.DataFrame(universe, columns=["asset", "asset_class", "flow_vehicle"]).set_index("asset")
    names = list(assets.index)
    n_a = len(names)

    regimes = _simulate_regimes(rng, n_t, n_a)
    impulse = pd.DataFrame(regimes.astype(float)).ewm(halflife=40).mean().to_numpy()

    # --- Rendements : facteur de classe + idiosyncratique (Student-t, queues épaisses) + prime de flux
    class_params = {  # (drift annuel, vol du facteur de classe, vol idiosyncratique)
        "EQUITY": (0.06, 0.14, 0.22),
        "FX": (0.00, 0.04, 0.07),
        "COMMODITY": (0.02, 0.12, 0.20),
        "CRYPTO": (0.20, 0.50, 0.45),
    }
    edge = 0.6  # prime annuelle en régime établi = 0.6 x vol totale de l'actif
    dof = 4.0

    def student(size) -> np.ndarray:
        return rng.standard_t(dof, size=size) / math.sqrt(dof / (dof - 2.0))

    factors = {c: student(n_t) * p[1] / math.sqrt(252) for c, p in class_params.items()}
    returns = np.empty((n_t, n_a))
    for j, name in enumerate(names):
        cls = assets.at[name, "asset_class"]
        mu, f_vol, i_vol = class_params[cls]
        total_vol = math.hypot(f_vol, i_vol)
        drift = mu / 252 + edge * total_vol / 252 * impulse[:, j]
        returns[:, j] = drift + factors[cls] + student(n_t) * i_vol / math.sqrt(252)
    returns = np.clip(returns, -0.5, 1.0)
    returns[0] = 0.0
    prices = pd.DataFrame(100.0 * np.exp(np.cumsum(np.log1p(returns), axis=0)), index=cal, columns=names)

    # --- Détention institutionnelle latente et rapports publiés
    log_h = np.log(1e8) + np.cumsum(0.0009 * regimes + 0.0015 * rng.standard_normal((n_t, n_a)), axis=0)
    rows = []
    for j, name in enumerate(names):
        cls = assets.at[name, "asset_class"]
        if cls == "EQUITY":
            period_ends = _as_ns(pd.date_range(cal[0], cal[-1], freq="QE"))
            filing_lags = rng.integers(20, 46, size=len(period_ends))  # la plupart déposent tard
        elif cls in ("FX", "COMMODITY"):
            period_ends = _as_ns(pd.date_range(cal[0], cal[-1], freq="W-TUE"))
            filing_lags = np.full(len(period_ends), 3)
        else:
            period_ends = cal
            filing_lags = np.full(len(period_ends), 1)
        idx = cal.searchsorted(period_ends, side="right") - 1
        keep = idx >= 0
        values = np.exp(log_h[idx[keep], j]) * (1 + 0.003 * rng.standard_normal(keep.sum()))
        rows.append(pd.DataFrame({
            "asset": name,
            "period_end": period_ends[keep],
            "filing_date": period_ends[keep] + pd.to_timedelta(filing_lags[keep], unit="D"),
            "value": values,
        }))
    holdings = pd.concat(rows, ignore_index=True)

    # --- Flux quotidiens des véhicules (réagissent au régime moyen de leurs sous-jacents)
    vehicles = list(dict.fromkeys(assets["flow_vehicle"]))
    flows = pd.DataFrame(index=cal, columns=vehicles, dtype=float)
    aum = pd.DataFrame(index=cal, columns=vehicles, dtype=float)
    for v in vehicles:
        members = [names.index(a) for a in assets.index[assets["flow_vehicle"] == v]]
        rbar = regimes[:, members].mean(axis=1)
        r_v = returns[:, members].mean(axis=1)
        noise = np.zeros(n_t)
        eps = 0.0025 * rng.standard_normal(n_t)
        for t in range(1, n_t):
            noise[t] = 0.3 * noise[t - 1] + eps[t]
        ratio = 0.0007 * rbar + noise
        level = np.empty(n_t)
        flow = np.empty(n_t)
        level_prev = 5e9
        for t in range(n_t):
            flow[t] = ratio[t] * level_prev
            level_prev = max(level_prev * (1 + r_v[t]) + flow[t], 1e6)
            level[t] = level_prev
        flows[v] = flow
        aum[v] = level

    # --- Plus hauts, plus bas et volumes : empreinte des grands acteurs
    rng_v = np.random.default_rng(seed + 10_000)
    sigma_d = np.array([math.hypot(*class_params[assets.at[a, "asset_class"]][1:]) for a in names]) / math.sqrt(252)
    reg = regimes.astype(float)
    tilt = 1.0 + 0.15 * reg * np.sign(returns)
    volume = (1e6 * np.exp(0.35 * rng_v.standard_normal((n_t, n_a)))
              * (1.0 + 0.5 * np.abs(returns) / sigma_d) * tilt)
    # Programmes d'achat / de vente des institutions : 3 à 5 séances de très fort volume, plus
    # fréquents en accumulation ou en distribution, et quelques-uns sans lien avec le régime
    # (résultats, échéances d'options) pour produire de fausses alertes.
    program = np.zeros((n_t, n_a))
    start_prob = np.where(reg != 0, 0.012, 0.003)
    starts = rng_v.random((n_t, n_a)) < start_prob
    for t0, j in zip(*np.nonzero(starts)):
        side = reg[t0, j] if reg[t0, j] != 0 else rng_v.choice([-1.0, 1.0])
        length = int(rng_v.integers(3, 6))
        program[t0:t0 + length, j] = side
    volume = volume * np.where(program != 0, rng_v.uniform(2.5, 4.0, (n_t, n_a)), 1.0)
    close = prices.to_numpy()
    open_ = np.vstack([close[:1], close[:-1]])
    tilt_wick = np.clip(reg + 2.0 * program, -3.0, 3.0)
    upper = sigma_d * 0.4 * np.abs(rng_v.standard_normal((n_t, n_a))) * np.clip(1.0 - 0.25 * tilt_wick, 0.1, None)
    lower = sigma_d * 0.4 * np.abs(rng_v.standard_normal((n_t, n_a))) * np.clip(1.0 + 0.25 * tilt_wick, 0.1, None)
    high = pd.DataFrame(np.maximum(open_, close) * (1.0 + upper), index=cal, columns=names)
    low = pd.DataFrame(np.minimum(open_, close) * (1.0 - lower), index=cal, columns=names)
    volume = pd.DataFrame(volume, index=cal, columns=names)

    # Hors bourse (actions uniquement, comme les fichiers FINRA) : les institutions passent davantage
    # par les bourses privées pendant l'accumulation ; les intermédiaires qui les servent vendent à découvert.
    equity = np.array([assets.at[a, "asset_class"] == "EQUITY" for a in names])
    share = np.clip(0.38 + 0.04 * reg + 0.05 * rng_v.standard_normal((n_t, n_a)), 0.05, 0.9)
    short = np.clip(0.46 + 0.04 * reg + 0.05 * rng_v.standard_normal((n_t, n_a)), 0.05, 0.95)
    offexchange = pd.DataFrame(np.where(equity, volume.to_numpy() * share, np.nan), index=cal, columns=names)
    offexchange_short = offexchange * short
    extras = _synthetic_radar_extras(cal, names, equity, reg, volume, seed, program=program, close=prices)

    return MarketData(prices=prices, holdings=holdings, flows=flows, aum=aum, assets=assets,
                      high=high, low=low, volume=volume, offexchange=offexchange,
                      offexchange_short=offexchange_short, extras=extras)


def _synthetic_radar_extras(cal: pd.DatetimeIndex, names: list[str], equity: np.ndarray, reg: np.ndarray,
                            volume: pd.DataFrame, seed: int, program: Optional[np.ndarray] = None,
                            close: Optional[pd.DataFrame] = None) -> RadarExtras:
    """Indices complémentaires fictifs pour les actions (générateur séparé : cours et volumes inchangés).

    Pendant l'accumulation, les institutions passent davantage par les bourses privées et les
    plateformes de blocs, les vendeurs à découvert se retirent, les dirigeants et de grands
    investisseurs achètent plus souvent ; l'inverse en distribution. Chaque donnée porte sa date
    de publication réelle (FINRA : 3 semaines pour les bourses privées, 11 jours pour les positions
    vendeuses ; SEC : 2 jours pour les Form 4, 10 jours pour les 13D / 13G). Les programmes d'achat ou
    de vente des institutions laissent des gros blocs, surtout hors bourse."""
    rng = np.random.default_rng(seed + 20_000)
    banks = sorted(set(BANK_VENUES.values()))
    reg_df = pd.DataFrame(reg, index=cal, columns=names)
    ats, short_rows, insiders, filings, earnings, blocks = [], [], [], [], [], []
    weekly_volume = volume.resample("W-MON", label="left", closed="left").sum()
    settlements = pd.DatetimeIndex(sorted(set(
        list(pd.date_range(cal[0], cal[-1], freq="SMS") + pd.Timedelta(days=14))
        + list(pd.date_range(cal[0], cal[-1], freq="ME")))))
    settlements = cal[np.unique(np.clip(cal.searchsorted(settlements, side="right") - 1, 0, None))]
    quarter_ends = pd.date_range(cal[0], cal[-1], freq="QE")
    for j, name in enumerate(names):
        if not equity[j]:
            continue
        # Bourses privées : part du volume, blocs et plateformes bancaires, semaine par semaine
        r_w = reg_df[name].resample("W-MON", label="left", closed="left").mean()
        for week, total in weekly_volume[name].items():
            r = r_w.get(week, 0.0)
            ats_vol = total * np.clip(0.12 + 0.04 * r + 0.015 * rng.standard_normal(), 0.02, 0.5)
            block = ats_vol * np.clip(0.03 + 0.03 * max(r, 0) + 0.01 * rng.standard_normal(), 0.002, 0.3)
            bank = ats_vol * np.clip(0.35 + 0.10 * abs(r) + 0.05 * rng.standard_normal(), 0.05, 0.9)
            ats.append((name, week, week + pd.Timedelta(days=21), ats_vol, block, bank,
                        ", ".join(rng.choice(banks, size=2, replace=False))))
        # Positions vendeuses déclarées : baissent en accumulation, montent en distribution
        level = 5e6 * np.exp(0.3 * rng.standard_normal())
        for day in settlements:
            level *= math.exp(-0.06 * reg_df.at[day, name] + 0.07 * rng.standard_normal())
            short_rows.append((name, day, day + pd.Timedelta(days=11), level))
        # Achats des dirigeants (Form 4) et franchissements de 5 % (13D / 13G)
        buy_prob = np.where(reg[:, j] > 0, 0.004, np.where(reg[:, j] < 0, 0.0005, 0.0012))
        for t in np.nonzero(rng.random(len(cal)) < buy_prob)[0]:
            k = int(rng.integers(1, 7))
            insiders.append((name, cal[t] + pd.Timedelta(days=2), f"Dirigeant fictif {k}",
                             "administrateur" if k > 4 else "directeur", float(np.exp(rng.normal(12.8, 1.0)))))
        cross_prob = np.where(reg[:, j] > 0, 0.0012, 0.0002)
        for t in np.nonzero(rng.random(len(cal)) < cross_prob)[0]:
            form = "SC 13D" if rng.random() < 0.25 else "SC 13G"
            filings.append((name, cal[t] + pd.Timedelta(days=10), form, f"Fonds fictif {int(rng.integers(1, 20))}"))
        # Résultats trimestriels : 3 à 5 semaines après la fin du trimestre
        for q in quarter_ends:
            earnings.append((name, q + pd.Timedelta(days=int(rng.integers(21, 36)))))
        # Gros blocs : pendant les programmes des institutions, et quelques-uns au hasard
        if program is not None and close is not None:
            dollars = (close[name] * volume[name]).to_numpy()
            noise = rng.random(len(cal)) < 0.02
            for t in np.nonzero((program[:, j] != 0) | noise)[0]:
                side = program[t, j] if program[t, j] != 0 and rng.random() < 0.8 else rng.choice([-1.0, 1.0])
                for _ in range(int(rng.integers(1, 4))):
                    notional = max(1e6, dollars[t] * rng.uniform(0.003, 0.012))
                    price = float(close[name].iat[t])
                    blocks.append((name, cal[t], "15:30:00", price, round(notional / price), notional,
                                   "hors bourse" if rng.random() < 0.6 else "bourse", float(side)))
    return RadarExtras(
        ats=pd.DataFrame(ats, columns=["asset", "week_start", "published", "ats_volume", "block_volume",
                                       "bank_volume", "banks"]),
        short_interest=pd.DataFrame(short_rows, columns=["asset", "settlement", "available", "short_qty"]),
        insiders=pd.DataFrame(insiders, columns=["asset", "filing_date", "owner", "role", "value"]),
        filings_5pct=pd.DataFrame(filings, columns=["asset", "filing_date", "form", "filer"]),
        earnings=pd.DataFrame(earnings, columns=["asset", "date"]),
        blocks=pd.DataFrame(blocks, columns=["asset", "date", "time", "price", "size", "notional", "venue", "side"]),
    )


def export_market_to_csv(data: MarketData, out_dir: str | Path) -> None:
    """Écrit les données au format CSV attendu par load_market_from_csv (sert de gabarit)."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    data.assets.reset_index().to_csv(out / "assets.csv", index=False)
    px = data.prices.rename_axis("date").reset_index().melt(id_vars="date", var_name="asset", value_name="close")
    if data.has_ohlcv:
        for name, frame in (("high", data.high), ("low", data.low), ("volume", data.volume)):
            extra = frame.rename_axis("date").reset_index().melt(id_vars="date", var_name="asset", value_name=name)
            px = px.merge(extra, on=["date", "asset"], how="left")
    px.dropna(subset=["close"]).to_csv(out / "prices.csv", index=False)
    if data.offexchange is not None:
        ox = data.offexchange.rename_axis("date").reset_index().melt(id_vars="date", var_name="asset",
                                                                      value_name="total_volume")
        sx = data.offexchange_short.rename_axis("date").reset_index().melt(id_vars="date", var_name="asset",
                                                                            value_name="short_volume")
        ox.merge(sx, on=["date", "asset"]).dropna(subset=["total_volume"]).to_csv(out / "offexchange.csv", index=False)
    if data.hourly is not None:
        data.hourly.to_csv(out / "hourly.csv", index=False)
    if data.extras is not None:
        for table in RadarExtras.TABLES:
            frame = getattr(data.extras, table)
            if frame is not None:
                frame.to_csv(out / f"{table}.csv", index=False)
    fl = data.flows.rename_axis("date").reset_index().melt(id_vars="date", var_name="vehicle", value_name="net_flow")
    au = data.aum.rename_axis("date").reset_index().melt(id_vars="date", var_name="vehicle", value_name="aum")
    fl.merge(au, on=["date", "vehicle"]).dropna(subset=["net_flow"]).to_csv(out / "flows.csv", index=False)
    data.holdings.to_csv(out / "holdings.csv", index=False)


def load_market_from_csv(data_dir: str | Path) -> MarketData:
    """Charge vos données réelles. Schéma attendu (CSV, dates ISO AAAA-MM-JJ) :

    assets.csv   : asset, asset_class (EQUITY | FX | COMMODITY | CRYPTO), flow_vehicle[, tv_symbol]
    prices.csv   : date, asset, close[, high, low, volume]
                   (high / low / volume activent l'empreinte de marché des grands acteurs)
    holdings.csv : asset, period_end, filing_date (vide si inconnue), value (> 0)
                   - actions   : actions détenues par les institutions de référence (13F agrégés,
                                 cf. build_13f_holdings_from_sec) ;
                   - FX / MP   : positions longues des Asset Managers / Managed Money (COT) ;
                   - crypto    : offre des Long-Term Holders ou solde des wallets institutionnels.
    offexchange.csv (optionnel) : date, asset, total_volume, short_volume (FINRA « Reg SHO »)
    ats.csv, short_interest.csv, insiders.csv, filings_5pct.csv, earnings.csv, blocks.csv (optionnels) :
                   indices complémentaires du radar (colonnes : voir institutional_radar.RadarExtras)
    market.csv (optionnel) : date, close — indice (SPY) où placer la trésorerie inutilisée
    hourly.csv (optionnel) : asset, time, high, low, close, volume (barres horaires, heure de New York)
    flows.csv    : date, vehicle, net_flow, aum
                   - ETF : net_flow = variation des parts en circulation x VL ; aum = encours ;
                   - EPFR : flux nets et encours des fonds du segment ;
                   - crypto : flux des ETP spot ou netflows des exchanges (signe inversé).
    """
    d = Path(data_dir)
    assets = pd.read_csv(d / "assets.csv").set_index("asset")
    px = pd.read_csv(d / "prices.csv")
    px["date"] = _as_ns(px["date"])
    prices = px.pivot_table(index="date", columns="asset", values="close", aggfunc="last").sort_index()
    ohlcv = {}
    if {"high", "low", "volume"}.issubset(px.columns):
        ohlcv = {name: px.pivot_table(index="date", columns="asset", values=name, aggfunc="last")
                 .reindex(index=prices.index, columns=prices.columns) for name in ("high", "low", "volume")}
    fl = pd.read_csv(d / "flows.csv")
    fl["date"] = _as_ns(fl["date"])
    flows = fl.pivot_table(index="date", columns="vehicle", values="net_flow", aggfunc="sum").sort_index()
    aum = fl.pivot_table(index="date", columns="vehicle", values="aum", aggfunc="last").sort_index()
    holdings = pd.read_csv(d / "holdings.csv")
    holdings["period_end"] = _as_ns(holdings["period_end"])
    holdings["filing_date"] = _as_ns(holdings["filing_date"])
    offx = {}
    if (d / "offexchange.csv").exists():
        ox = pd.read_csv(d / "offexchange.csv")
        ox["date"] = _as_ns(ox["date"])
        offx = {"offexchange": ox.pivot_table(index="date", columns="asset", values="total_volume", aggfunc="sum"),
                "offexchange_short": ox.pivot_table(index="date", columns="asset", values="short_volume", aggfunc="sum")}
    tables = {}
    for table in RadarExtras.TABLES:
        if (d / f"{table}.csv").exists():
            frame = pd.read_csv(d / f"{table}.csv", keep_default_na=False, na_values=[""])
            for col in RadarExtras.DATE_COLUMNS[table]:
                frame[col] = _as_ns(frame[col])
            tables[table] = frame
    extras = RadarExtras(**tables) if tables else None
    hourly = pd.read_csv(d / "hourly.csv", keep_default_na=False, na_values=[""]) if (d / "hourly.csv").exists() else None
    market = None
    if (d / "market.csv").exists():
        mk = pd.read_csv(d / "market.csv")
        market = pd.Series(mk["close"].to_numpy(), index=_as_ns(mk["date"]), name="market").sort_index()
    data = MarketData(prices=prices, holdings=holdings, flows=flows, aum=aum, assets=assets, **ohlcv, **offx,
                      extras=extras, hourly=hourly, market=market)
    data.validate()
    return data


def _parse_sec_date(series: pd.Series) -> pd.Series:
    """Dates SEC DERA au format '31-MAR-2024' (repli sur l'analyse générique sinon)."""
    parsed = pd.to_datetime(series, format="%d-%b-%Y", errors="coerce")
    fallback = pd.to_datetime(series[parsed.isna()], errors="coerce")
    return parsed.fillna(fallback).astype("datetime64[ns]")


def filter_13f_filings(
    sub: pd.DataFrame, info: pd.DataFrame, statutory_lag_days: int = 45,
    reference_ciks: Optional[Iterable[str | int]] = None,
) -> pd.DataFrame:
    """Applique les règles point-in-time aux tables SUBMISSION et INFOTABLE des jeux 13F de la SEC.

      * seules les déclarations initiales « 13F-HR » sont retenues : les amendements
        (13F-HR/A), déposés plus tard, réécriraient l'historique ; si un gérant dépose deux
        déclarations initiales pour un même trimestre, seule la première compte ;
      * seules les déclarations déposées au plus tard à l'échéance légale (fin de trimestre
        + 45 jours) sont retenues : un dépôt tardif n'était pas connu du marché à cette date ;
      * seules les actions ordinaires (SSHPRNAMTTYPE = 'SH', hors options PUTCALL) comptent.

    Retourne une ligne par ligne de portefeuille : cik, cusip, name, period_end, filing_date,
    accession, shares (SSHPRNAMT), value (VALUE, en milliers de dollars avant 2023 puis en dollars :
    à n'utiliser que pour pondérer les lignes d'une même déclaration).
    """
    sub = sub[sub["SUBMISSIONTYPE"].str.strip() == "13F-HR"].copy()
    sub["CIK"] = sub["CIK"].astype(str).str.strip().astype(int).astype(str)
    if reference_ciks is not None:
        sub = sub[sub["CIK"].isin({str(int(c)) for c in reference_ciks})]
    sub["FILING_DATE"] = _parse_sec_date(sub["FILING_DATE"])
    sub["PERIODOFREPORT"] = _parse_sec_date(sub["PERIODOFREPORT"])
    deadline = sub["PERIODOFREPORT"] + pd.Timedelta(days=statutory_lag_days)
    sub = sub[sub["FILING_DATE"] <= deadline]
    sub = sub.sort_values("FILING_DATE").drop_duplicates(["CIK", "PERIODOFREPORT"], keep="first")

    info = info[info["SSHPRNAMTTYPE"].str.strip().str.upper() == "SH"]
    if "PUTCALL" in info.columns:
        info = info[info["PUTCALL"].isna() | (info["PUTCALL"].str.strip() == "")]
    info = info.assign(
        cusip=info["CUSIP"].str.strip().str.upper(),
        shares=pd.to_numeric(info["SSHPRNAMT"], errors="coerce"),
        value=pd.to_numeric(info["VALUE"], errors="coerce") if "VALUE" in info.columns else np.nan,
        name=info["NAMEOFISSUER"].str.strip() if "NAMEOFISSUER" in info.columns else "",
    )
    merged = info.merge(sub[["ACCESSION_NUMBER", "CIK", "FILING_DATE", "PERIODOFREPORT"]], on="ACCESSION_NUMBER")
    merged = merged.rename(columns={"CIK": "cik", "PERIODOFREPORT": "period_end", "FILING_DATE": "filing_date",
                                    "ACCESSION_NUMBER": "accession"})
    return merged[["cik", "cusip", "name", "period_end", "filing_date", "accession", "shares", "value"]]


def load_13f_positions(
    dataset_dirs: Iterable[str | Path],
    cusip_to_asset: Optional[Mapping[str, str]] = None,
    reference_ciks: Optional[Iterable[str | int]] = None,
    statutory_lag_days: int = 45,
) -> pd.DataFrame:
    """Lit les « Form 13F Data Sets » de la SEC (DERA) : une ligne par gérant, actif et trimestre.

    Source : https://www.sec.gov/data-research/sec-markets-data/form-13f-data-sets
    (un répertoire décompressé par période ; les fichiers SUBMISSION.tsv et INFOTABLE.tsv
    sont utilisés). Règles point-in-time : voir filter_13f_filings. On compte le NOMBRE
    d'actions (SSHPRNAMT), insensible au changement d'unité du champ VALUE.

    cusip_to_asset : correspondance CUSIP -> code actif (None = on garde le CUSIP).
    reference_ciks : CIK des institutions « Smart Money » suivies (None = toutes).
    Retourne un DataFrame cik, asset, period_end, filing_date, shares.
    """
    subs, infos = [], []
    for directory in dataset_dirs:
        d = Path(directory)
        subs.append(pd.read_csv(d / "SUBMISSION.tsv", sep="\t", dtype=str))
        infos.append(pd.read_csv(d / "INFOTABLE.tsv", sep="\t", dtype=str))
    rows = filter_13f_filings(pd.concat(subs, ignore_index=True), pd.concat(infos, ignore_index=True),
                              statutory_lag_days, reference_ciks)
    if cusip_to_asset is None:
        rows = rows.assign(asset=rows["cusip"])
    else:
        mapping = {k.strip().upper(): v for k, v in cusip_to_asset.items()}
        rows = rows.assign(asset=rows["cusip"].map(mapping)).dropna(subset=["asset"])
    out = (
        rows.groupby(["cik", "asset", "period_end"])
        .agg(shares=("shares", "sum"), filing_date=("filing_date", "max"))
        .reset_index()
    )
    return out[out["shares"] > 0][["cik", "asset", "period_end", "filing_date", "shares"]]


def build_13f_holdings_from_sec(
    dataset_dirs: Iterable[str | Path],
    cusip_to_asset: Mapping[str, str],
    reference_ciks: Optional[Iterable[str | int]] = None,
    statutory_lag_days: int = 45,
) -> pd.DataFrame:
    """Détention agrégée par actif et trimestre (mêmes règles point-in-time que load_13f_positions).

    Pour une liste Smart Money qui évolue dans le temps, utiliser plutôt
    smart_money.smart_money_holdings_index(), qui mesure les variations à composition constante.
    Retourne un DataFrame asset, period_end, filing_date, value, n_filers.
    """
    pos = load_13f_positions(dataset_dirs, cusip_to_asset, reference_ciks, statutory_lag_days)
    out = (
        pos.groupby(["asset", "period_end"])
        .agg(value=("shares", "sum"), filing_date=("filing_date", "max"), n_filers=("cik", "nunique"))
        .reset_index()
    )
    return out[["asset", "period_end", "filing_date", "value", "n_filers"]].sort_values(["asset", "period_end"])


# =============================================================================
# 3. POINT-IN-TIME : APPLICATION STRICTE DES DÉLAIS DE PUBLICATION
# =============================================================================

def _staleness_limit_days(spec: SourceSpec, cfg: StrategyConfig) -> int:
    """Au-delà de cet âge (depuis la date d'arrêté), une donnée de détention est jugée périmée."""
    return spec.report_freq_days + spec.statutory_lag_days + cfg.processing_lag_days + max(7, spec.report_freq_days // 3)


def build_pit_institutional_change(
    holdings: pd.DataFrame,
    calendar: pd.DatetimeIndex,
    assets: pd.DataFrame,
    cfg: StrategyConfig,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Variation de la détention institutionnelle, telle qu'elle était CONNUE à chaque date.

    Pour chaque rapport k (date d'arrêté period_end_k) :
        variation_k = valeur_k / valeur du rapport arrêté ~io_lookback_days plus tôt - 1
        disponible_k = max(filing_date_k, period_end_k + délai légal) + délai de traitement
    La valeur est ensuite reportée sur le calendrier à partir de la première séance
    >= disponible_k. Un rapport tardif portant sur une période plus ancienne que la
    dernière connue est ignoré. Une donnée périmée (source muette) devient NaN.

    Retourne (io_change, io_period_end), deux DataFrame [date x actif].
    """
    cal = _as_ns(calendar)
    h_all = holdings.copy()
    h_all["period_end"] = _as_ns(h_all["period_end"])
    h_all["filing_date"] = _as_ns(h_all["filing_date"])
    change_cols: dict[str, pd.Series] = {}
    pe_cols: dict[str, pd.Series] = {}

    for asset in assets.index:
        spec = ASSET_CLASS_SPECS[assets.at[asset, "asset_class"]]
        h = h_all[(h_all["asset"] == asset) & h_all["value"].notna()]
        if h.empty:
            continue
        # PIT : en cas de doublon sur une même date d'arrêté, la première publication fait foi.
        h = h.sort_values(["period_end", "filing_date"]).drop_duplicates("period_end", keep="first")

        left = h[["period_end", "filing_date", "value"]].copy()
        left["ref_key"] = left["period_end"] - pd.Timedelta(days=cfg.io_lookback_days - LOOKBACK_TOLERANCE_DAYS)
        right = h[["period_end", "value"]].rename(columns={"period_end": "ref_period_end", "value": "ref_value"})
        m = pd.merge_asof(
            left.sort_values("ref_key"), right.sort_values("ref_period_end"),
            left_on="ref_key", right_on="ref_period_end", direction="backward",
        )
        m["change"] = m["value"] / m["ref_value"] - 1.0

        if cfg.ignore_publication_lags:  # BIAISÉ : on « connaît » le rapport à sa date d'arrêté
            m["available"] = m["period_end"]
        else:
            legal = m["period_end"] + pd.Timedelta(days=spec.statutory_lag_days)
            filed = m["filing_date"].fillna(legal)
            m["available"] = filed.where(filed > legal, legal) + pd.Timedelta(days=cfg.processing_lag_days)

        m = m.dropna(subset=["change"]).sort_values(["available", "period_end"])
        m = m[m["period_end"] >= m["period_end"].cummax()]  # ignore les rapports tardifs plus anciens
        m = m.drop_duplicates("available", keep="last")

        pos = cal.searchsorted(_as_ns(m["available"]), side="left")
        ok = pos < len(cal)
        chg = pd.Series(np.nan, index=cal)
        pe = pd.Series(pd.NaT, index=cal, dtype="datetime64[ns]")
        chg.iloc[pos[ok]] = m["change"].to_numpy()[ok]
        pe.iloc[pos[ok]] = m["period_end"].to_numpy()[ok]
        chg, pe = chg.ffill(), pe.ffill()

        age = (cal - pd.DatetimeIndex(pe)).days
        stale = np.asarray(age > _staleness_limit_days(spec, cfg)) | pe.isna().to_numpy()
        chg[stale] = np.nan
        pe[stale] = pd.NaT
        change_cols[asset] = chg
        pe_cols[asset] = pe

    io_change = pd.DataFrame(change_cols, index=cal).reindex(columns=assets.index)
    io_pe = pd.DataFrame(pe_cols, index=cal).reindex(columns=assets.index)
    return io_change, io_pe


# =============================================================================
# 4. SIGNAUX (vectorisés, sans état)
# =============================================================================

@dataclass
class Signals:
    calendar: pd.DatetimeIndex
    prices: pd.DataFrame  # cours reportés (valorisation)
    price_valid: pd.DataFrame  # cotation effective ce jour (exécution possible)
    returns: pd.DataFrame
    vol: pd.DataFrame  # volatilité annualisée réalisée
    io_change: pd.DataFrame  # jambe lente, point-in-time
    io_period_end: pd.DataFrame  # date d'arrêté du rapport utilisé
    flow_ratio: pd.DataFrame  # jambe rapide : flux net 30 j / encours
    flow_ratio_short: pd.DataFrame  # flux net 7 j / encours (vélocité court terme)
    entry: pd.DataFrame  # convergence accumulation (achat)
    dist_slow: pd.DataFrame  # institutions en allègement
    dist_fast: pd.DataFrame  # flux sortant massif
    dist_hard: pd.DataFrame  # liquidation institutionnelle
    dist_foot: pd.DataFrame  # empreinte de marché vendeuse (prix / volume)
    score: pd.DataFrame  # intensité du signal (classement des candidats)
    footprint: Optional[Footprint] = None
    radar: Optional[Radar] = None


def _window_min_periods(window: str) -> int:
    return max(3, int(pd.Timedelta(window).days * 0.5))


def compute_signals(data: MarketData, cfg: StrategyConfig) -> Signals:
    """Calcule toutes les variables de décision, chacune avec l'information disponible à la date."""
    data.validate()
    names = list(data.assets.index)
    raw = data.prices.reindex(columns=names).sort_index()
    cal = _as_ns(raw.index)
    raw.index = cal
    price_valid = raw.notna()
    prices = raw.ffill()
    returns = (prices / prices.shift(1) - 1.0).where(price_valid)
    vol = returns.rolling(cfg.vol_lookback, min_periods=cfg.vol_lookback // 2).std() * math.sqrt(252)

    io_change, io_pe = build_pit_institutional_change(data.holdings, cal, data.assets, cfg)

    # Flux : la donnée du jour J n'est publiée qu'en J+1 (parts en circulation, rapports EPFR...).
    lag = 0 if cfg.ignore_publication_lags else cfg.flow_publication_lag
    flows = data.flows.copy()
    flows.index = _as_ns(flows.index)
    aum = data.aum.copy()
    aum.index = _as_ns(aum.index)
    flows = flows.reindex(cal).shift(lag)
    aum = aum.reindex(cal).ffill().shift(lag)

    def ratio(window: str) -> pd.DataFrame:
        summed = flows.rolling(window, min_periods=_window_min_periods(window)).sum()
        return summed / aum

    vehicles = data.assets["flow_vehicle"].to_numpy()

    def to_assets(frame: pd.DataFrame) -> pd.DataFrame:
        out = frame.reindex(columns=vehicles)
        out.columns = names
        return out

    flow_ratio = to_assets(ratio(cfg.flow_window))
    flow_ratio_short = to_assets(ratio(cfg.flow_window_short))

    acc_slow = io_change >= cfg.entry_io_change
    if cfg.flow_rule == "cumulative":
        acc_fast = flow_ratio > cfg.entry_flow_threshold
    else:  # lecture littérale : chaque flux quotidien de la fenêtre est positif
        daily_min = flows.rolling(cfg.flow_window, min_periods=_window_min_periods(cfg.flow_window)).min()
        acc_fast = (to_assets(daily_min) > 0) & (flow_ratio > cfg.entry_flow_threshold)
    tradable = price_valid & vol.notna() if cfg.sizing == "inverse_vol" else price_valid
    entry = acc_slow & acc_fast & tradable

    dist_slow = io_change <= cfg.exit_io_change
    dist_fast = flow_ratio <= cfg.exit_flow_threshold
    dist_hard = io_change <= cfg.exit_io_change_hard
    score = io_change + flow_ratio

    # Troisième jambe : empreinte prix / volume des grands acteurs. Elle interdit d'acheter
    # pendant une distribution visible, renforce le classement des candidats en accumulation
    # et compte comme une jambe à part entière dans l'échelle de sortie.
    def aligned(frame: pd.DataFrame) -> pd.DataFrame:
        out = frame.reindex(columns=names).copy()
        out.index = _as_ns(out.index)
        return out.reindex(cal)

    footprint = None
    dist_foot = pd.DataFrame(False, index=cal, columns=names)
    if cfg.use_footprint and data.has_ohlcv:
        footprint = compute_footprint(aligned(data.high), aligned(data.low), raw, aligned(data.volume))
        dist_foot = footprint.distribution
        entry = entry & ~dist_foot
        score = score + 0.02 * footprint.accumulation

    # Radar des grands acteurs : gros volumes anormaux recoupés sur plusieurs unités de temps.
    radar = None
    if cfg.use_radar and data.has_ohlcv:
        offx = aligned(data.offexchange) if data.offexchange is not None else None
        offx_short = aligned(data.offexchange_short) if data.offexchange_short is not None else None
        extras = replace(data.extras) if data.extras is not None else RadarExtras()
        vehicle = data.assets["flow_vehicle"]
        sizes = vehicle.map(vehicle.value_counts())
        extras.groups = vehicle.where(sizes >= 3, data.assets["asset_class"]).to_dict()  # secteur, sinon classe
        hourly = None
        if data.hourly is not None and len(data.hourly):
            hourly = {a: g.set_index("time")[["high", "low", "close", "volume"]].sort_index()
                      for a, g in data.hourly.assign(time=_as_ns(data.hourly["time"])).groupby("asset") if a in names}
        radar = compute_radar(aligned(data.high), aligned(data.low), raw, aligned(data.volume), offx, offx_short,
                              hourly=hourly, extras=extras)
        entry = entry & ~(radar.score <= -ALERT_THRESHOLD)
        score = score + 0.01 * radar.score

    return Signals(
        calendar=cal, prices=prices, price_valid=price_valid, returns=returns, vol=vol,
        io_change=io_change, io_period_end=io_pe, flow_ratio=flow_ratio,
        flow_ratio_short=flow_ratio_short, entry=entry, dist_slow=dist_slow,
        dist_fast=dist_fast, dist_hard=dist_hard, dist_foot=dist_foot, score=score, footprint=footprint,
        radar=radar,
    )


# =============================================================================
# 5. MOTEUR DE BACKTEST (boucle quotidienne, avec état)
# =============================================================================

@dataclass
class BacktestResult:
    config: StrategyConfig
    assets: pd.DataFrame
    signals: Signals
    equity: pd.Series
    benchmark: pd.Series
    weights: pd.DataFrame
    scales: pd.DataFrame
    gross: pd.Series
    n_positions: pd.Series
    costs: pd.Series
    traded: pd.Series
    derisk: pd.Series
    trades: pd.DataFrame
    journal: pd.DataFrame
    metrics: dict
    benchmark_metrics: dict
    index_exposure: Optional[pd.Series] = None  # part de la NAV placée dans l'indice (trésorerie)


def _pct(x: float, digits: int = 1, signed: bool = True) -> str:
    if x is None or not np.isfinite(x):
        return "n.d."
    return f"{x:+.{digits}%}" if signed else f"{x:.{digits}%}"


def _date(x) -> str:
    return "n.d." if x is None or pd.isna(x) else pd.Timestamp(x).strftime("%d/%m/%Y")


def run_backtest(data: MarketData, cfg: Optional[StrategyConfig] = None, signals: Optional[Signals] = None) -> BacktestResult:
    """Simule la gestion quotidienne du fonds.

    Chronologie d'une séance t (aucune information postérieure à la clôture de t n'est utilisée) :
      1. intérêts sur la trésorerie ;
      2. exécution à la clôture de t (proxy VWAP) d'une fraction des ordres décidés
         au plus tard en t-1 : ordre fractionné en parts égales sur execution_days séances ;
      3. valorisation (NAV) ;
      4. contrôles du Risk Manager : perte critique par ligne, coupe-circuit drawdown ;
      5. revue de 100 % des lignes (Exit Management) puis sélection des nouvelles entrées ;
         les ordres correspondants commencent à être exécutés en t+1.
    """
    cfg = cfg or StrategyConfig()
    sig = signals if signals is not None else compute_signals(data, cfg)
    cal = sig.calendar
    names = list(sig.prices.columns)
    meta = data.assets.loc[names]
    classes = meta["asset_class"].to_numpy()
    vehicles = meta["flow_vehicle"].to_numpy()
    n, n_t = len(names), len(cal)

    px = sig.prices.to_numpy(float)
    valid = sig.price_valid.to_numpy(bool)
    io = sig.io_change.to_numpy(float)
    io_pe = sig.io_period_end
    fr = sig.flow_ratio.to_numpy(float)
    entry_sig = sig.entry.to_numpy(bool)
    d_slow = sig.dist_slow.to_numpy(bool)
    d_fast = sig.dist_fast.to_numpy(bool)
    d_hard = sig.dist_hard.to_numpy(bool)
    d_foot = sig.dist_foot.to_numpy(bool)
    score = sig.score.to_numpy(float)
    vol = sig.vol.to_numpy(float)
    cost_rate = np.array([ASSET_CLASS_SPECS[c].cost_bps for c in classes]) * 1e-4 * cfg.cost_multiplier

    units = np.zeros(n)
    cost_basis = np.zeros(n)
    base_units = np.zeros(n)
    entry_vol = np.full(n, np.nan)
    scale = np.zeros(n)
    target = np.zeros(n)
    days_left = np.zeros(n, dtype=int)
    pending_action = [""] * n
    entry_date: list[Optional[pd.Timestamp]] = [None] * n
    last_exit: list[Optional[pd.Timestamp]] = [None] * n
    last_reduce: list[Optional[pd.Timestamp]] = [None] * n
    open_trade: list[Optional[dict]] = [None] * n

    cash = cfg.initial_capital
    peak = cash
    use_market = cfg.idle_cash_in_market
    if use_market and data.market is None:
        raise ValueError("idle_cash_in_market exige le cours de l'indice (MarketData.market, fichier market.csv)")
    mkt_ret = (data.market.reindex(data.market.index.union(cal)).sort_index().ffill().reindex(cal).pct_change()
               .to_numpy() if use_market else np.zeros(len(cal)))
    index_hist = np.zeros(len(cal))
    was_in_market = False
    derisk_until: Optional[pd.Timestamp] = None
    nav_hist = np.empty(n_t)
    w_hist = np.zeros((n_t, n))
    scale_hist = np.zeros((n_t, n))
    cost_hist = np.zeros(n_t)
    traded_hist = np.zeros(n_t)
    derisk_hist = np.zeros(n_t, dtype=bool)
    journal: list[dict] = []
    trades: list[dict] = []

    def close_trade(a: int, t: pd.Timestamp, reason: str, mark_price: float = 0.0) -> None:
        tr = open_trade[a]
        residual = units[a] * mark_price
        exit_value = tr["sell_notional"] + residual
        tr.update(
            exit_date=t if reason != "EN_COURS" else pd.NaT,
            exit_reason=reason,
            holding_days=(t - tr["first_fill"]).days,
            avg_entry_price=tr["buy_notional"] / tr["buy_units"],
            avg_exit_price=exit_value / (tr["sell_units"] + units[a]) if tr["sell_units"] + units[a] > 0 else np.nan,
            pnl=exit_value - tr["buy_notional"] - tr["fees"],
        )
        tr["return"] = tr["pnl"] / tr["buy_notional"]
        trades.append(tr)
        open_trade[a] = None

    def decide(i: int, t: pd.Timestamp, a: int, new_scale: float, action: str, urgent: bool, text: str, nav: float) -> None:
        scale_before = scale[a]
        scale[a] = new_scale
        target[a] = base_units[a] * new_scale
        days_left[a] = cfg.urgent_execution_days if urgent else cfg.execution_days
        pending_action[a] = action
        decided[a] = True
        if new_scale == 0:
            last_exit[a] = t
            entry_date[a] = None
        elif action != "ENTRY" and new_scale < scale_before:
            last_reduce[a] = t
        journal.append({
            "date": t, "asset": names[a], "asset_class": classes[a], "action": action,
            "label": ACTION_LABELS[action], "target_scale": new_scale, "price": px[i, a],
            "io_change": io[i, a], "flow_ratio_30d": fr[i, a], "nav": nav, "justification": text,
        })

    def flow_text(i: int, a: int) -> str:
        spec = ASSET_CLASS_SPECS[classes[a]]
        pe = io_pe.iat[i, a]
        return (f"détention institutionnelle {_pct(io[i, a])} ({spec.name}, arrêté au {_date(pe)}) ; "
                f"flux net 30 j {_pct(fr[i, a], 2)} de l'encours ({vehicles[a]})")

    prev_t = None
    for i, t in enumerate(cal):
        decided = np.zeros(n, dtype=bool)
        p = px[i]

        # 1) Intérêts sur la trésorerie (solde de la veille)
        #    ou, si la trésorerie est placée dans l'indice, performance de l'indice depuis la veille
        in_market = use_market and derisk_until is None and cash > 0
        if prev_t is not None:
            if in_market and np.isfinite(mkt_ret[i]):
                cash *= 1.0 + mkt_ret[i]
            else:
                cash *= (1.0 + cfg.cash_rate) ** ((t - prev_t).days / 365.0)
        prev_t = t
        if use_market and in_market != was_in_market and cash > 0:
            cash -= cash * cfg.market_cost  # achat ou vente de l'indice à l'entrée / sortie du coupe-circuit
        was_in_market = in_market

        # 2) Exécution fractionnée des ordres décidés au plus tard la veille
        day_cost = day_traded = 0.0
        for a in np.flatnonzero(days_left > 0):
            if not valid[i, a]:
                continue  # pas de cotation : l'ordre attend
            qty = (target[a] - units[a]) / days_left[a]
            days_left[a] -= 1
            if qty < 0:
                qty = max(qty, -units[a])  # pas de vente à découvert
            if abs(qty) * p[a] < 1e-6:
                continue
            notional = qty * p[a]
            fee = abs(notional) * (cost_rate[a] + (cfg.market_cost if in_market else 0.0))
            cash -= notional + fee
            day_cost += fee
            day_traded += abs(notional)
            tr = open_trade[a]
            if qty > 0:
                if tr is None:
                    tr = open_trade[a] = {
                        "asset": names[a], "asset_class": classes[a],
                        "entry_signal_date": entry_date[a] if entry_date[a] is not None else t,
                        "first_fill": t, "buy_units": 0.0, "buy_notional": 0.0,
                        "sell_units": 0.0, "sell_notional": 0.0, "fees": 0.0,
                    }
                cost_basis[a] = (units[a] * cost_basis[a] + notional) / (units[a] + qty)
                tr["buy_units"] += qty
                tr["buy_notional"] += notional
            else:
                tr["sell_units"] -= qty
                tr["sell_notional"] -= notional
            tr["fees"] += fee
            units[a] += qty
            if days_left[a] == 0:
                units[a] = target[a]  # élimine les résidus d'arrondi
            if units[a] <= 0:
                units[a] = 0.0
                cost_basis[a] = 0.0
                close_trade(a, t, pending_action[a])

        # 3) Valorisation
        nav = cash + float(np.nansum(units * p))
        index_hist[i] = cash / nav if in_market and nav > 0 else 0.0

        # 4) Risk Manager : garde-fous indépendants des signaux de flux
        if cfg.position_stop_loss is not None:
            for a in np.flatnonzero((scale > 0) & (units > 0) & (cost_basis > 0) & valid[i]):
                loss = p[a] / cost_basis[a] - 1.0
                limit = max(cfg.position_stop_loss, cfg.stop_vol_multiple * np.nan_to_num(entry_vol[a]))
                if loss <= -limit:
                    decide(i, t, a, 0.0, "RISK_STOP", True,
                           f"Perte de {_pct(loss)} vs prix de revient (seuil {_pct(-limit, 0)}, ajusté à la volatilité) ; "
                           f"sortie imposée par le Risk Manager indépendamment des flux ({flow_text(i, a)}).", nav)
        if cfg.max_weight_drift:
            for a in np.flatnonzero((scale > 0) & (units > 0) & (days_left == 0) & valid[i] & ~decided):
                weight = units[a] * p[a] / nav
                if weight > cfg.max_weight * cfg.max_weight_drift:
                    base_units[a] = cfg.max_weight * nav / p[a] / scale[a]
                    decide(i, t, a, scale[a], "RISK_TRIM", False,
                           f"Poids de la ligne {_pct(weight, 1, False)} > limite de concentration "
                           f"{_pct(cfg.max_weight * cfg.max_weight_drift, 1, False)} : écrêtage à "
                           f"{_pct(cfg.max_weight, 0, False)} de la NAV, thèse de flux inchangée.", nav)
        if cfg.portfolio_dd_limit is not None:
            if derisk_until is not None and t >= derisk_until:
                derisk_until = None
                peak = nav  # réarmement du coupe-circuit
            peak = max(peak, nav)
            dd = nav / peak - 1.0
            if derisk_until is None and dd <= -cfg.portfolio_dd_limit:
                for a in np.flatnonzero(scale > 0):
                    decide(i, t, a, scale[a] * cfg.derisk_fraction, "RISK_DRAWDOWN", True,
                           f"Drawdown du portefeuille {_pct(dd)} (seuil {_pct(-cfg.portfolio_dd_limit, 0)}) : "
                           f"exposition réduite de {_pct(1 - cfg.derisk_fraction, 0, False)} et entrées gelées "
                           f"{cfg.derisk_cooldown_days} jours.", nav)
                derisk_until = t + pd.Timedelta(days=cfg.derisk_cooldown_days)

        # 5a) Exit Management : revue de 100 % des lignes actives
        for a in np.flatnonzero((scale > 0) & ~decided):
            if (t - entry_date[a]).days < cfg.min_holding_days:
                continue
            legs = [name for name, flag in (
                (f"institutions en allègement (<= {_pct(cfg.exit_io_change, 0)})", d_slow[i, a]),
                (f"flux sortant massif (<= {_pct(cfg.exit_flow_threshold, 0)} de l'encours)", d_fast[i, a]),
                ("empreinte prix / volume vendeuse (grands acteurs en distribution)", d_foot[i, a]),
            ) if flag]
            if d_hard[i, a]:
                decide(i, t, a, 0.0, "EXIT_INSTITUTIONAL_LIQUIDATION", False,
                       f"Les institutions de référence liquident ({flow_text(i, a)}) : "
                       f"baisse au-delà du seuil de {_pct(cfg.exit_io_change_hard, 0)}, thèse de flux invalidée.", nav)
            elif len(legs) >= 2:
                decide(i, t, a, 0.0, "EXIT_DISTRIBUTION_CONFIRMED", False,
                       f"Distribution confirmée par {len(legs)} jambes sur 3 : {' ; '.join(legs)}. {flow_text(i, a)}.", nav)
            elif legs and scale[a] > cfg.partial_exit_fraction + 1e-12:
                decide(i, t, a, cfg.partial_exit_fraction, "REDUCE_DISTRIBUTION_ALERT", False,
                       f"Premier signal de distribution — {legs[0]} : {flow_text(i, a)}. "
                       f"Ligne ramenée à {_pct(cfg.partial_exit_fraction, 0, False)} en attendant confirmation.", nav)
            elif (scale[a] < 1.0 and entry_sig[i, a] and derisk_until is None and valid[i, a]
                  and (last_reduce[a] is None or (t - last_reduce[a]).days >= cfg.reentry_cooldown_days)):
                # Renforcement plafonné par la limite par ligne et par l'exposition brute disponible
                others = float(np.nansum(np.maximum(target, units) * p)) - max(target[a], units[a]) * p[a]
                cap = min(cfg.max_weight * nav, cfg.max_gross * nav - others) / p[a]
                new_units = min(base_units[a], cap)
                if new_units > target[a] * 1.05:
                    base_units[a] = new_units
                    decide(i, t, a, 1.0, "ADD_REACCUMULATION", False,
                           f"Alerte levée, accumulation de nouveau confirmée : {flow_text(i, a)}.", nav)

        # 5b) Nouvelles entrées : convergence des deux jambes, classées par intensité
        if derisk_until is None:
            free = cfg.max_positions - int((scale > 0).sum())
            candidates = [
                a for a in np.flatnonzero(entry_sig[i] & (scale == 0) & ~decided)
                if last_exit[a] is None or (t - last_exit[a]).days >= cfg.reentry_cooldown_days
            ]
            candidates.sort(key=lambda a: -score[i, a])
            for a in candidates:
                if free <= 0:
                    break
                if cfg.sizing == "equal":
                    w = min(cfg.max_weight, 1.0 / cfg.max_positions)
                else:
                    w = cfg.max_weight if vol[i, a] <= 0 else min(cfg.max_weight, cfg.risk_budget_per_position / vol[i, a])
                # Les lignes en cours de vente comptent jusqu'à exécution complète (pas de levier transitoire)
                exposure = float(np.nansum(np.maximum(target, units) * p)) / nav
                w = min(w, cfg.max_gross - exposure)
                if not np.isfinite(w) or w < cfg.min_weight:
                    continue
                base_units[a] = w * nav / p[a]
                entry_vol[a] = vol[i, a]
                entry_date[a] = t
                decide(i, t, a, 1.0, "ENTRY", False,
                       f"Convergence des flux : {flow_text(i, a)}. Accumulation (>= {_pct(cfg.entry_io_change, 0)}) "
                       f"et flux net positif sur 30 j. Poids cible {_pct(w, 1, False)} "
                       f"({'budget de risque' if cfg.sizing == 'inverse_vol' else 'équipondéré'}).", nav)
                free -= 1

        # 6) Enregistrement
        nav_hist[i] = nav
        w_hist[i] = np.nan_to_num(units * p) / nav
        scale_hist[i] = scale
        cost_hist[i] = day_cost
        traded_hist[i] = day_traded
        derisk_hist[i] = derisk_until is not None

    for a in range(n):  # lignes encore ouvertes : valorisées au dernier cours
        if open_trade[a] is not None:
            close_trade(a, cal[-1], "EN_COURS", px[-1, a])

    equity = pd.Series(nav_hist, index=cal, name="Stratégie")
    bench_ret = sig.returns.mean(axis=1).fillna(0.0)
    benchmark = (cfg.initial_capital * (1.0 + bench_ret).cumprod()).rename("Univers équipondéré")
    weights = pd.DataFrame(w_hist, index=cal, columns=names)
    trade_cols = ["asset", "asset_class", "entry_signal_date", "first_fill", "exit_date", "exit_reason",
                  "holding_days", "avg_entry_price", "avg_exit_price", "buy_notional", "fees", "pnl", "return"]
    trades_df = pd.DataFrame(trades, columns=trade_cols).sort_values("first_fill").reset_index(drop=True)
    journal_df = pd.DataFrame(journal, columns=["date", "asset", "asset_class", "action", "label", "target_scale",
                                                "price", "io_change", "flow_ratio_30d", "nav", "justification"])
    traded = pd.Series(traded_hist, index=cal, name="traded")

    metrics = compute_metrics(equity, cfg.risk_free_rate)
    metrics["turnover_annual"] = float(traded.sum() / equity.mean() / metrics["years"])
    metrics["total_costs_pct_initial"] = float(cost_hist.sum() / cfg.initial_capital)
    metrics["avg_gross_exposure"] = float(weights.sum(axis=1).mean())
    metrics["avg_index_exposure"] = float(index_hist.mean())
    metrics.update(trade_statistics(trades_df))

    return BacktestResult(
        config=cfg, assets=meta, signals=sig, equity=equity, benchmark=benchmark, weights=weights,
        scales=pd.DataFrame(scale_hist, index=cal, columns=names), gross=weights.sum(axis=1).rename("gross"),
        n_positions=pd.Series((scale_hist > 0).sum(axis=1), index=cal, name="n_positions"),
        costs=pd.Series(cost_hist, index=cal, name="costs"), traded=traded,
        derisk=pd.Series(derisk_hist, index=cal, name="derisk"), trades=trades_df, journal=journal_df,
        metrics=metrics, benchmark_metrics=compute_metrics(benchmark, cfg.risk_free_rate),
        index_exposure=pd.Series(index_hist, index=cal, name="index"),
    )


# =============================================================================
# 6. MÉTRIQUES DE RISQUE ET DE PERFORMANCE
# =============================================================================

def _drawdown_stats(equity: pd.Series) -> dict:
    values = equity.to_numpy(float)
    dates = equity.index
    peak_val, peak_i = values[0], 0
    uw_start: Optional[int] = None
    longest = 0
    mdd, mdd_peak_i, mdd_trough_i = 0.0, 0, 0
    for i, v in enumerate(values):
        if v >= peak_val:
            if uw_start is not None:
                longest = max(longest, (dates[i] - dates[uw_start]).days)
                uw_start = None
            peak_val, peak_i = v, i
        else:
            if uw_start is None:
                uw_start = peak_i
            dd = v / peak_val - 1.0
            if dd < mdd:
                mdd, mdd_peak_i, mdd_trough_i = dd, peak_i, i
    current_uw = (dates[-1] - dates[uw_start]).days if uw_start is not None else 0
    longest = max(longest, current_uw)

    recovery_date = None
    if mdd < 0:
        after = np.flatnonzero(values[mdd_trough_i:] >= values[mdd_peak_i])
        if after.size:
            recovery_date = dates[mdd_trough_i + after[0]]
    return {
        "max_drawdown": float(mdd),
        "mdd_peak_date": dates[mdd_peak_i],
        "mdd_trough_date": dates[mdd_trough_i],
        "mdd_recovery_date": recovery_date,
        # Recovery Time : du creux au retour au plus-haut précédent (jours calendaires)
        "recovery_time_days": (recovery_date - dates[mdd_trough_i]).days if recovery_date is not None else None,
        # Durée totale de l'épisode : du pic au retour au plus-haut
        "mdd_duration_days": (recovery_date - dates[mdd_peak_i]).days if recovery_date is not None else None,
        "longest_underwater_days": int(longest),
        "current_underwater_days": int(current_uw),
    }


def compute_metrics(equity: pd.Series, risk_free_rate: float = 0.0) -> dict:
    """Métriques annualisées. La fréquence est déduite du calendrier (jours ouvrés ou 7j/7)."""
    eq = equity.dropna()
    r = (eq / eq.shift(1) - 1.0).dropna()
    years = (eq.index[-1] - eq.index[0]).days / 365.25
    ppy = len(r) / years
    gaps = np.diff(eq.index.to_numpy()).astype("timedelta64[s]").astype(float) / 86400.0
    excess = r.to_numpy() - ((1.0 + risk_free_rate) ** (gaps / 365.25) - 1.0)
    std = excess.std(ddof=1)
    downside = math.sqrt(np.mean(np.minimum(excess, 0.0) ** 2))
    var95 = float(-np.quantile(r, 0.05))
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1.0 / years) - 1.0
    out = {
        "start": eq.index[0], "end": eq.index[-1], "years": years,
        "total_return": float(eq.iloc[-1] / eq.iloc[0] - 1.0),
        "cagr": float(cagr),
        "volatility": float(r.std(ddof=1) * math.sqrt(ppy)),
        "sharpe": float(excess.mean() / std * math.sqrt(ppy)) if std > 0 else float("nan"),
        "sortino": float(excess.mean() / downside * math.sqrt(ppy)) if downside > 0 else float("nan"),
        "var_95_1d": var95,
        "cvar_95_1d": float(-r[r <= -var95].mean()),
        "best_day": float(r.max()),
        "worst_day": float(r.min()),
    }
    out.update(_drawdown_stats(eq))
    out["calmar"] = float(cagr / abs(out["max_drawdown"])) if out["max_drawdown"] < 0 else float("nan")
    return out


def trade_statistics(trades: pd.DataFrame) -> dict:
    closed = trades[trades["exit_reason"] != "EN_COURS"]
    if closed.empty:
        return {"n_trades": 0, "n_open": int(len(trades))}
    gains = closed.loc[closed["pnl"] > 0, "pnl"].sum()
    losses = -closed.loc[closed["pnl"] < 0, "pnl"].sum()
    return {
        "n_trades": int(len(closed)),
        "n_open": int(len(trades) - len(closed)),
        "win_rate": float((closed["pnl"] > 0).mean()),
        "avg_trade_return": float(closed["return"].mean()),
        "median_holding_days": float(closed["holding_days"].median()),
        "profit_factor": float(gains / losses) if losses > 0 else float("inf"),
        "exit_reasons": closed["exit_reason"].value_counts().to_dict(),
    }


# =============================================================================
# 7. REVUE CONTINUE DES POSITIONS : MATRICE PRÉDICTIVE 3 HORIZONS + EXIT SIGNAL
# =============================================================================

# (libellé, horizon en observations, variable conditionnante, nombre de classes)
REVIEW_HORIZONS = (
    ("J+5", 5, "flow_ratio_short", 5),  # jours   : vélocité des flux à 7 j
    ("M+3", 63, "flow_ratio", 5),  # mois    : flux nets à 30 j
    ("A+1", 252, "io_change", 3),  # années  : positionnement institutionnel
)


def _exit_signal(sig: Signals, i: int, a: int, scale: float) -> str:
    legs = int(sig.dist_slow.iat[i, a]) + int(sig.dist_fast.iat[i, a]) + int(sig.dist_foot.iat[i, a])
    if sig.dist_hard.iat[i, a] or legs >= 2:
        return "VENTE IMMÉDIATE (niveau 2)"
    if legs == 1:
        return "ALLÈGEMENT PROGRESSIF (niveau 1)"
    if 0 < scale < 1 and sig.entry.iat[i, a]:
        return "RENFORCER"
    return "CONSERVER (niveau 0)"


def review_positions(result: BacktestResult, date=None, include_all: bool = False) -> pd.DataFrame:
    """Produit, pour chaque ligne active, la Matrice Prédictive, l'Explication et l'Exit Signal.

    Modèle de référence (volontairement simple, à remplacer par les modèles du Quant) :
    pour chaque horizon h, la probabilité empirique de hausse P(r[t, t+h] > 0) est estimée
    dans la classe d'actifs, conditionnellement au quantile de la variable de flux
    correspondante. Seules les observations dont le rendement futur était CONNU à la date
    de revue (t + h <= date) sont utilisées : pas de biais d'anticipation.
    """
    sig = result.signals
    cal = sig.calendar
    i_t = len(cal) - 1 if date is None else int(cal.searchsorted(pd.Timestamp(date), side="right")) - 1
    t = cal[i_t]
    names = list(sig.prices.columns)
    scales = result.scales.iloc[i_t]
    selected = names if include_all else [a for a in names if scales[a] > 0]
    classes = result.assets["asset_class"]

    stats: dict[tuple[str, str], tuple[float, float, float, int]] = {}
    for label, h, feature_name, n_buckets in REVIEW_HORIZONS:
        feature = getattr(sig, feature_name)
        fwd = sig.prices.shift(-h) / sig.prices - 1.0
        last_train = i_t - h
        if last_train < 50:
            continue
        for cls in classes.unique():
            cols = [a for a in names if classes[a] == cls]
            x = feature.iloc[: last_train + 1][cols].to_numpy().ravel()
            y = fwd.iloc[: last_train + 1][cols].to_numpy().ravel()
            ok = np.isfinite(x) & np.isfinite(y)
            x, y = x[ok], y[ok]
            if len(x) < 100:
                continue
            edges = np.unique(np.quantile(x, np.linspace(0, 1, n_buckets + 1))[1:-1])
            buckets = np.searchsorted(edges, x, side="right")
            base = float((y > 0).mean())
            for a in selected:
                if classes[a] != cls:
                    continue
                x_now = feature.iat[i_t, names.index(a)]
                if not np.isfinite(x_now):
                    continue
                sel = y[buckets == np.searchsorted(edges, x_now, side="right")]
                if len(sel):
                    stats[(a, label)] = (float((sel > 0).mean()), float(np.median(sel)), base, len(sel))

    rows = []
    i_30 = max(0, int(cal.searchsorted(t - pd.Timedelta(days=30))))
    for a in selected:
        j = names.index(a)
        cls = classes[a]
        spec = ASSET_CLASS_SPECS[cls]
        vehicle = result.assets.at[a, "flow_vehicle"]
        fr30, fr7 = sig.flow_ratio.iat[i_t, j], sig.flow_ratio_short.iat[i_t, j]
        io, pe = sig.io_change.iat[i_t, j], sig.io_period_end.iat[i_t, j]
        price_ret = sig.prices.iat[i_t, j] / sig.prices.iat[i_30, j] - 1.0
        signal = _exit_signal(sig, i_t, j, scales[a])

        parts = []
        if np.isfinite(fr30) and np.isfinite(fr7):
            pace = "accélération" if fr7 / 7 > fr30 / 30 else "décélération"
            direction = "acheteuse" if fr30 > 0 else "vendeuse"
            parts.append(f"Vélocité des flux ({vehicle}) : {_pct(fr30, 2)} de l'encours sur 30 j, "
                         f"{_pct(fr7, 2)} sur 7 j — pression {direction} en {pace}.")
        if np.isfinite(io):
            trend = "accumulent" if io > 0 else "allègent"
            parts.append(f"Les institutions de référence {trend} : {_pct(io)} sur ~1 trimestre "
                         f"({spec.name}, arrêté au {_date(pe)}, donnée publiée).")
        else:
            parts.append(f"Positionnement institutionnel non disponible ou périmé ({spec.name}).")
        if np.isfinite(fr30) and np.isfinite(price_ret):
            if abs(price_ret) < 0.01:
                parts.append(f"Prix stable ({_pct(price_ret)} sur 30 j) : "
                             + ("absorption des entrées de capitaux." if fr30 > 0 else "absorption des sorties de capitaux."))
            elif price_ret > 0 > fr30:
                parts.append(f"Divergence baissière : prix {_pct(price_ret)} sur 30 j malgré des sorties de capitaux "
                             "(distribution dans la force).")
            elif price_ret < 0 < fr30:
                parts.append(f"Divergence haussière : prix {_pct(price_ret)} sur 30 j malgré des entrées de capitaux "
                             "(accumulation dans la faiblesse).")
            else:
                parts.append(f"Prix ({_pct(price_ret)} sur 30 j) et flux alignés.")
        if sig.footprint is not None:
            parts.append(describe_footprint(sig.footprint, sig.prices.iat[i_t, j], i_t, j))
        if sig.radar is not None:
            radar_text = sig.radar.explain(a, i_t)
            if radar_text != "Aucune trace anormale.":
                parts.append("Radar des grands acteurs — " + radar_text)
        probas = [f"{lbl} {stats[(a, lbl)][0]:.0%} (base {stats[(a, lbl)][2]:.0%})"
                  for lbl, *_ in REVIEW_HORIZONS if (a, lbl) in stats]
        if probas:
            parts.append("Probabilité empirique de hausse : " + ", ".join(probas) + ".")

        row = {"asset": a, "asset_class": cls, "weight": result.weights.iat[i_t, j], "target_scale": scales[a]}
        for lbl, *_ in REVIEW_HORIZONS:
            p_up, med, base, n_obs = stats.get((a, lbl), (np.nan, np.nan, np.nan, 0))
            row[f"p_up_{lbl}"] = p_up
            row[f"median_ret_{lbl}"] = med
            row[f"edge_{lbl}"] = p_up - base
            row[f"n_obs_{lbl}"] = n_obs
        row.update(exit_signal=signal, justification=" ".join(parts), review_date=t)
        rows.append(row)
    return pd.DataFrame(rows)


# =============================================================================
# 8. RAPPORT, SORTIES ET GRAPHIQUE
# =============================================================================

def _metric_rows(m: dict) -> list[tuple[str, str]]:
    def days(x) -> str:
        return "non récupéré" if x is None else f"{int(x)} j"

    return [
        ("Rendement total", _pct(m["total_return"])),
        ("CAGR", _pct(m["cagr"])),
        ("Volatilité annualisée", _pct(m["volatility"], signed=False)),
        ("Sharpe Ratio", f"{m['sharpe']:.2f}"),
        ("Sortino Ratio", f"{m['sortino']:.2f}"),
        ("Calmar Ratio", f"{m['calmar']:.2f}"),
        ("Maximum Drawdown", _pct(m["max_drawdown"])),
        ("  pic → creux", f"{_date(m['mdd_peak_date'])} → {_date(m['mdd_trough_date'])}"),
        ("Recovery Time (creux → plus-haut)", days(m["recovery_time_days"])),
        ("Durée du Max DD (pic → plus-haut)", days(m["mdd_duration_days"])),
        ("Plus longue période sous l'eau", f"{m['longest_underwater_days']} j"),
        ("VaR 95 % 1 jour (historique)", _pct(m["var_95_1d"], 2, False)),
        ("CVaR 95 % 1 jour", _pct(m["cvar_95_1d"], 2, False)),
    ]


def print_report(result: BacktestResult, review: pd.DataFrame, biased: Optional[BacktestResult] = None,
                 data_label: str = "") -> None:
    m, b, cfg = result.metrics, result.benchmark_metrics, result.config
    line = "=" * 88
    print(line)
    print("BACKTEST — MOYEN/LONG-TERM FLOW TRADING (Smart Money & Exit Management)")
    print(line)
    if data_label:
        print(f"Données : {data_label}")
    print(f"Période : {_date(m['start'])} → {_date(m['end'])} ({m['years']:.1f} ans) | "
          f"{len(result.assets)} actifs | capital initial {cfg.initial_capital:,.0f}")
    print(f"Entrée : détention {_pct(cfg.entry_io_change, 0)} & flux {cfg.flow_window} > "
          f"{_pct(cfg.entry_flow_threshold, 1)} ({cfg.flow_rule}) | Sortie : détention "
          f"{_pct(cfg.exit_io_change, 0)} & flux {_pct(cfg.exit_flow_threshold, 0)} "
          f"(liquidation {_pct(cfg.exit_io_change_hard, 0)})")
    print(f"Délais PIT : 13F +45 j, COT +3 j, on-chain +1 j, flux J+1, traitement +{cfg.processing_lag_days} j | "
          f"exécution sur {cfg.execution_days} séances")

    print(f"\n{'MÉTRIQUE':<40}{'STRATÉGIE':>22}{'UNIVERS ÉQUIPONDÉRÉ':>24}")
    for (label, v_s), (_, v_b) in zip(_metric_rows(m), _metric_rows(b)):
        print(f"{label:<40}{v_s:>22}{v_b:>24}")
    print(f"{'Exposition brute moyenne':<40}{_pct(m['avg_gross_exposure'], 0, False):>22}{'100%':>24}")
    print(f"{'Turnover annuel':<40}{m['turnover_annual']:>21.1f}x")
    print(f"{'Coûts de transaction cumulés':<40}{_pct(m['total_costs_pct_initial'], 2, False):>22}")

    print("\nTRANSACTIONS (allers-retours clôturés)")
    if m.get("n_trades", 0):
        print(f"  Nombre : {m['n_trades']} (+{m['n_open']} en cours) | taux de réussite {_pct(m['win_rate'], 0, False)} | "
              f"rendement moyen {_pct(m['avg_trade_return'])} | détention médiane {m['median_holding_days']:.0f} j | "
              f"profit factor {m['profit_factor']:.2f}")
        print("  Motifs de sortie :")
        for reason, count in m["exit_reasons"].items():
            print(f"    - {ACTION_LABELS.get(reason, reason):<62} {count:>4}")
    else:
        print("  Aucune transaction clôturée.")

    if biased is not None:
        mb = biased.metrics
        print("\nCONTRÔLE DU BIAIS D'ANTICIPATION (même stratégie, délais de publication ignorés)")
        print(f"{'':<28}{'CAGR':>10}{'Sharpe':>10}{'Max DD':>10}{'Trades':>10}")
        print(f"{'PIT (délais légaux)':<28}{_pct(m['cagr']):>10}{m['sharpe']:>10.2f}{_pct(m['max_drawdown']):>10}{m.get('n_trades', 0):>10}")
        print(f"{'Biaisé (délais ignorés)':<28}{_pct(mb['cagr']):>10}{mb['sharpe']:>10.2f}{_pct(mb['max_drawdown']):>10}{mb.get('n_trades', 0):>10}")
        print(f"  => Écart biaisé - PIT : {_pct(mb['cagr'] - m['cagr'])} de CAGR, {mb['sharpe'] - m['sharpe']:+.2f} "
              "de Sharpe (un écart positif est une performance fictive, inatteignable en réel).")

    print(f"\nREVUE DES LIGNES ACTIVES AU {_date(review['review_date'].iloc[0]) if len(review) else 'n.d.'} "
          f"— Matrice prédictive (P(hausse) par horizon)")
    if review.empty:
        print("  Aucune ligne active.")
    for _, r in review.iterrows():
        probs = " | ".join(f"{lbl} {r[f'p_up_{lbl}']:.0%}" if np.isfinite(r[f"p_up_{lbl}"]) else f"{lbl} n.d."
                           for lbl, *_ in REVIEW_HORIZONS)
        print(f"  {r['asset']:<14} poids {_pct(r['weight'], 1, False):>6} | {probs} | {r['exit_signal']}")
        print(f"      {r['justification']}")
    print(line)


def save_outputs(result: BacktestResult, review: pd.DataFrame, out_dir: str | Path,
                 biased: Optional[BacktestResult] = None) -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    pd.concat([result.equity, result.benchmark, result.gross, result.n_positions], axis=1).to_csv(out / "equity.csv")
    result.trades.to_csv(out / "trades.csv", index=False)
    result.journal.to_csv(out / "journal_arbitrages.csv", index=False)
    review.to_csv(out / "revue_positions.csv", index=False)
    payload = {"config": asdict(result.config), "strategy": result.metrics, "benchmark": result.benchmark_metrics}
    if biased is not None:
        payload["biased_no_publication_lag"] = biased.metrics
    (out / "metrics.json").write_text(json.dumps(payload, indent=2, default=str, ensure_ascii=False), encoding="utf-8")
    return out


def plot_results(result: BacktestResult, path: str | Path) -> Optional[Path]:
    """Graphique en trois panneaux : valeur liquidative, drawdown, exposition. Optionnel."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.ticker as mticker
    except ImportError:
        print("matplotlib absent : graphique non produit.")
        return None

    # Jetons de couleur : une seule entité (la stratégie) en bleu sur les trois panneaux,
    # l'univers de comparaison en gris (mise en retrait), textes en encre neutre.
    surface, ink, secondary, muted, grid = "#fcfcfb", "#0b0b0b", "#52514e", "#898781", "#e1e0d9"
    strat_c = "#2a78d6"
    plt.rcParams.update({"font.size": 9, "axes.edgecolor": grid, "axes.labelcolor": secondary,
                         "xtick.color": secondary, "ytick.color": secondary, "axes.titlecolor": ink,
                         "figure.facecolor": surface, "axes.facecolor": surface})

    fig, axes = plt.subplots(3, 1, figsize=(11, 8), sharex=True, gridspec_kw={"height_ratios": [3, 1.3, 1.1]})
    eq = result.equity / result.equity.iloc[0] * 100
    bench = result.benchmark / result.benchmark.iloc[0] * 100

    ax = axes[0]
    ax.plot(bench.index, bench, color=muted, lw=1.2, label="Univers équipondéré (sans coûts)")
    ax.plot(eq.index, eq, color=strat_c, lw=2.0, label="Stratégie (nette de coûts)")
    ax.set_yscale("log")
    ax.yaxis.set_major_locator(mticker.LogLocator(base=10, subs=(1.0, 1.5, 2.0, 3.0, 5.0, 7.0)))
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v:,.0f}"))
    ax.yaxis.set_minor_formatter(mticker.NullFormatter())
    ax.text(eq.index[-1], eq.iloc[-1], f"  {eq.iloc[-1]:,.0f}", color=ink, va="center", fontweight="bold")
    ax.text(bench.index[-1], bench.iloc[-1], f"  {bench.iloc[-1]:,.0f}", color=secondary, va="center")
    ax.legend(loc="upper left", frameon=False, labelcolor=ink)
    m = result.metrics
    ax.set_title(f"Valeur liquidative (base 100, échelle log) — Sharpe {m['sharpe']:.2f}, "
                 f"CAGR {_pct(m['cagr'])}, Max DD {_pct(m['max_drawdown'])}", loc="left", fontsize=10)

    ax = axes[1]
    dd = result.equity / result.equity.cummax() - 1
    ax.fill_between(dd.index, dd, 0, color=strat_c, alpha=0.18, lw=0)
    ax.plot(dd.index, dd, color=strat_c, lw=1.2)
    ax.yaxis.set_major_formatter(mticker.PercentFormatter(1.0, decimals=0))
    ax.set_title("Drawdown de la stratégie", loc="left", fontsize=10)

    ax = axes[2]
    ax.fill_between(result.gross.index, result.gross, 0, color=strat_c, alpha=0.18, lw=0)
    ax.plot(result.gross.index, result.gross, color=strat_c, lw=1.2)
    ax.yaxis.set_major_formatter(mticker.PercentFormatter(1.0, decimals=0))
    ax.set_ylim(0, max(1.0, float(result.gross.max()) * 1.05))
    ax.set_title("Exposition brute (part de la NAV investie)", loc="left", fontsize=10)

    for ax in axes:
        ax.grid(True, color=grid, lw=0.6)
        ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)
    return Path(path)


def run_multi_seed(cfg: StrategyConfig, n_seeds: int) -> pd.DataFrame:
    """Robustesse sur N mondes synthétiques : la conclusion ne doit pas dépendre d'une graine."""
    rows = []
    for seed in range(1, n_seeds + 1):
        data = generate_synthetic_market(seed=seed)
        pit = run_backtest(data, cfg)
        biased = run_backtest(data, replace(cfg, ignore_publication_lags=True))
        rows.append({
            "graine": seed, "CAGR": pit.metrics["cagr"], "Sharpe": pit.metrics["sharpe"],
            "Sortino": pit.metrics["sortino"], "MaxDD": pit.metrics["max_drawdown"],
            "Sharpe univers": pit.benchmark_metrics["sharpe"], "Sharpe biaisé": biased.metrics["sharpe"],
            "Trades": pit.metrics.get("n_trades", 0),
        })
    table = pd.DataFrame(rows).set_index("graine")
    summary = table.agg(["mean", "min", "max"])
    print("ROBUSTESSE — mondes synthétiques indépendants (données SYNTHÉTIQUES)")
    print(pd.concat([table, summary]).round(3).to_string())
    return table


def _build_parser() -> argparse.ArgumentParser:
    d = StrategyConfig()
    p = argparse.ArgumentParser(description="Backtest Moyen/Long-Term Flow Trading (Smart Money & Exit Management)")
    p.add_argument("--data-dir", help="répertoire de CSV réels (voir load_market_from_csv) ; défaut : synthétique")
    p.add_argument("--export-synthetic", metavar="DIR", help="exporte le jeu synthétique en CSV (gabarit) puis quitte")
    p.add_argument("--output-dir", default="outputs", help="répertoire des résultats (défaut : outputs)")
    p.add_argument("--seed", type=int, default=7, help="graine du jeu synthétique")
    p.add_argument("--entry-io", type=float, default=d.entry_io_change, help="hausse de détention à l'achat (0.03 = +3 %%)")
    p.add_argument("--exit-io", type=float, default=d.exit_io_change, help="baisse de détention (alerte) (-0.03)")
    p.add_argument("--exit-io-hard", type=float, default=d.exit_io_change_hard, help="baisse de détention (sortie seule)")
    p.add_argument("--entry-flow", type=float, default=d.entry_flow_threshold, help="flux 30 j / encours minimal à l'achat")
    p.add_argument("--exit-flow", type=float, default=d.exit_flow_threshold, help="flux sortant massif (-0.02 = -2 %%)")
    p.add_argument("--flow-rule", choices=["cumulative", "consecutive"], default=d.flow_rule)
    p.add_argument("--sizing", choices=["inverse_vol", "equal"], default=d.sizing)
    p.add_argument("--max-positions", type=int, default=d.max_positions)
    p.add_argument("--execution-days", type=int, default=d.execution_days)
    p.add_argument("--stop-loss", type=float, default=d.position_stop_loss, help="perte critique par ligne ; <= 0 désactive")
    p.add_argument("--dd-limit", type=float, default=d.portfolio_dd_limit, help="coupe-circuit drawdown ; <= 0 désactive")
    p.add_argument("--idle-cash-in-market", action="store_true",
                   help="trésorerie non investie placée dans l'indice (market.csv : SPY)")
    p.add_argument("--no-bias-study", action="store_true", help="ne pas relancer le backtest sans délais de publication")
    p.add_argument("--no-plot", action="store_true")
    p.add_argument("--tradingview", action="store_true",
                   help="exporte watchlist + indicateurs Pine des lignes actives dans <output-dir>/tradingview")
    p.add_argument("--multi-seed", type=int, metavar="N",
                   help="robustesse : relance la démo synthétique sur les graines 1..N et affiche le tableau")
    return p


def main(argv: Optional[list[str]] = None) -> Optional[BacktestResult]:
    args = _build_parser().parse_args(argv)
    cfg = StrategyConfig(
        entry_io_change=args.entry_io, exit_io_change=args.exit_io, exit_io_change_hard=args.exit_io_hard,
        entry_flow_threshold=args.entry_flow, exit_flow_threshold=args.exit_flow, flow_rule=args.flow_rule,
        sizing=args.sizing, max_positions=args.max_positions, execution_days=args.execution_days,
        position_stop_loss=args.stop_loss if args.stop_loss and args.stop_loss > 0 else None,
        portfolio_dd_limit=args.dd_limit if args.dd_limit and args.dd_limit > 0 else None,
        idle_cash_in_market=args.idle_cash_in_market,
    )
    if args.multi_seed:
        run_multi_seed(cfg, args.multi_seed)
        return None
    if args.data_dir:
        data = load_market_from_csv(args.data_dir)
        label = f"réelles ({args.data_dir})"
    else:
        data = generate_synthetic_market(seed=args.seed)
        label = f"SYNTHÉTIQUES (graine {args.seed}) — validation de la mécanique uniquement"
    if args.export_synthetic:
        export_market_to_csv(data, args.export_synthetic)
        print(f"Gabarits CSV écrits dans {args.export_synthetic}")
        return None

    result = run_backtest(data, cfg)
    biased = None if args.no_bias_study else run_backtest(data, replace(cfg, ignore_publication_lags=True))
    review = review_positions(result)
    print_report(result, review, biased, label)
    out = save_outputs(result, review, args.output_dir, biased)
    if not args.no_plot:
        plot_results(result, out / "backtest.png")
    if args.tradingview:
        from tradingview_bridge import export_tradingview
        tv_dir = export_tradingview(result, review, out / "tradingview", synthetic=not args.data_dir)
        print(f"Fichiers TradingView (watchlist + Pine) : {tv_dir.resolve()}")
    print(f"Résultats enregistrés dans {out.resolve()}")
    return result


if __name__ == "__main__":
    main()
