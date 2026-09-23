# Hedge Fund — Moyen/Long-Term Flow Trading (Smart Money & Exit Management)

Organisation, protocole de revue et backtest d'un fonds qui suit l'accumulation et la
distribution des capitaux institutionnels, sur les actions, les devises, les matières premières
et la crypto, avec des positions détenues de 15 jours à plusieurs trimestres.

| Livrable | Contenu |
|---|---|
| [`docs/SYNTHESE_MANAGERIALE.md`](docs/SYNTHESE_MANAGERIALE.md) | Synthèse managériale : validation de l'organisation, protocole d'évaluation continue et de sortie, architecture, TradingView, résultats, questions ouvertes |
| [`backtest/flow_backtest.py`](backtest/flow_backtest.py) | Moteur de backtest : délais de publication appliqués (13F, COT, flux), sortie à trois niveaux, exécution fractionnée, métriques de risque, Matrice prédictive |
| [`backtest/smart_money.py`](backtest/smart_money.py) | Liste Smart Money point-in-time (gérants sélectionnés sur leurs 13F passés), indice de détention à composition constante, proxy de flux gratuit à partir des volumes |
| [`backtest/tradingview_bridge.py`](backtest/tradingview_bridge.py) | Export vers TradingView : watchlist et indicateur Pine « journal du fonds » par ligne active |
| [`tradingview/`](tradingview/) | Indicateur Pine *Smart Money Flow Monitor* et récepteur des alertes webhook |
| [`tests/`](tests/) | 29 tests, dont le test d'absence de biais d'anticipation (perturbation du futur) |

**Décisions du 23/09/2026** : acheteur uniquement, liste Smart Money, actions US d'abord, données
gratuites, risque équilibré, TradingView gratuit (détail au §9 de la synthèse).

## Démarrage rapide

```bash
pip install -r requirements.txt
python backtest/flow_backtest.py                  # démo sur données SYNTHÉTIQUES
python backtest/flow_backtest.py --tradingview    # + fichiers TradingView dans outputs/tradingview
python backtest/flow_backtest.py --multi-seed 8   # robustesse sur 8 mondes synthétiques
python -m pytest -q                               # tests
```

Résultats écrits dans `outputs/` :

| Fichier | Contenu |
|---|---|
| `equity.csv` | Valeur liquidative et exposition |
| `trades.csv` | Allers-retours |
| `journal_arbitrages.csv` | Chaque décision et sa justification |
| `revue_positions.csv` | Matrice prédictive à 3 horizons et Exit Signal |
| `metrics.json` | Métriques de risque et de performance |
| `backtest.png` | Graphique du backtest |

> Les données synthétiques valident la **mécanique** (points-in-time, règles de sortie,
> métriques), pas la performance réelle de la stratégie.

## Utiliser vos données réelles

```bash
python backtest/flow_backtest.py --export-synthetic data_demo/   # gabarits CSV au bon format
python backtest/flow_backtest.py --data-dir mes_donnees/
```

| Fichier | Colonnes |
|---|---|
| `assets.csv` | `asset, asset_class (EQUITY / FX / COMMODITY / CRYPTO), flow_vehicle[, tv_symbol]` |
| `prices.csv` | `date, asset, close` (inclure les titres radiés, avec leur dernier prix de radiation) |
| `holdings.csv` | `asset, period_end, filing_date, value` : détention des institutions de référence (13F, COT, on-chain) |
| `flows.csv` | `date, vehicle, net_flow, aum` : flux nets et encours des véhicules (ETF, EPFR, ETP) |

Pour les actions, `build_13f_holdings_from_sec()` agrège les « Form 13F Data Sets » de la SEC en
`holdings.csv`, en point-in-time :
- déclarations initiales uniquement, déposées avant l'échéance légale ;
- options exclues ;
- filtre optionnel sur une liste de CIK « Smart Money ».

## Paramètres principaux (`StrategyConfig`)

| Paramètre | Défaut | Rôle |
|---|---|---|
| `entry_io_change` | +3 % | Hausse de détention institutionnelle sur ~1 trimestre pour acheter |
| `flow_rule` / `entry_flow_threshold` | `cumulative` / 0 % | Flux net 30 j / encours positif (`consecutive` : chaque jour positif) |
| `exit_io_change` / `exit_flow_threshold` | −3 % / −2 % | Une jambe = allègement à 50 % ; les deux = sortie totale |
| `exit_io_change_hard` | −10 % | Liquidation institutionnelle : sortie totale |
| `min_holding_days` / `reentry_cooldown_days` | 15 / 20 | Horizon minimal ; délai avant ré-achat ou renforcement |
| `execution_days` | 5 | Exécution fractionnée (proxy VWAP) |
| `position_stop_loss` / `stop_vol_multiple` | 25 % / 0.75 | Perte critique (Risk Manager) : max(25 %, 0.75 × volatilité annuelle) |
| `portfolio_dd_limit` | 20 % | Coupe-circuit : −50 % d'exposition et entrées gelées 63 jours |

## TradingView

TradingView sert de couche visuelle et d'alerte ; il ne sert pas à générer les signaux (voir §4.3 de la synthèse).
1. **Watchlist** : importer `outputs/tradingview/watchlist_fonds.txt` dans TradingView.
2. **Journal sur le graphique** : coller `outputs/tradingview/pine/<actif>.pine` dans l'éditeur Pine, puis l'ajouter au graphique du symbole indiqué en en-tête.
3. **Smart Money Flow Monitor** : coller `tradingview/smart_money_flow_monitor.pine` dans l'éditeur Pine et renseigner le secret du webhook.
4. **Alertes** : lancer le récepteur, puis le placer derrière un reverse proxy HTTPS (port 443) :
   ```bash
   TV_WEBHOOK_SECRET="un-secret-long" python tradingview/webhook_receiver.py --port 8080
   ```
   Côté TradingView, créer une alerte sur l'indicateur (« N'importe quel appel de fonction alert() ») avec l'URL du webhook.

Les scripts Pine n'ont pas pu être compilés dans l'environnement de développement (pas d'accès
à TradingView) : les vérifier dans l'éditeur Pine lors du premier import.
