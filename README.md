# Hedge Fund — Moyen/Long-Term Flow Trading (Smart Money & Exit Management)

Organisation, protocole de revue et backtest d'un fonds qui suit l'accumulation et la
distribution des capitaux institutionnels, sur les actions, les devises, les matières premières
et la crypto, avec des positions détenues de 15 jours à plusieurs trimestres.

| Livrable | Contenu |
|---|---|
| [`docs/SYNTHESE_MANAGERIALE.md`](docs/SYNTHESE_MANAGERIALE.md) | Synthèse managériale : validation de l'organisation, protocole d'évaluation continue et de sortie, architecture, TradingView, résultats, décisions |
| [`backtest/flow_backtest.py`](backtest/flow_backtest.py) | Moteur de backtest : délais de publication appliqués (13F, COT, flux), sortie à trois niveaux, exécution fractionnée, métriques de risque, Matrice prédictive |
| [`backtest/smart_money.py`](backtest/smart_money.py) | Liste Smart Money point-in-time (gérants sélectionnés sur leurs 13F passés), indice de détention à composition constante, proxy de flux gratuit à partir des volumes |
| [`backtest/market_footprint.py`](backtest/market_footprint.py) | Troisième jambe : empreinte des grands acteurs (banques, institutions) dans le prix et le volume — zone de valeur, VWAP, ratio hausses / baisses, jours de distribution |
| [`backtest/institutional_radar.py`](backtest/institutional_radar.py) | Radar des grands acteurs : volumes anormaux, pression acheteuse ou vendeuse, accumulation discrète et échanges hors bourse, sur l'heure, le jour, la semaine et le mois ; plus les indices gratuits complémentaires (divergence volume / prix, jours d'accumulation, force relative, bourses privées et plateformes des banques, positions vendeuses, achats des dirigeants, franchissements de 5 %, gros blocs d'au moins 1 M$ lus dans le détail des transactions, filtre des jours de résultats) |
| [`backtest/trade_cards.py`](backtest/trade_cards.py) · [`backtest/dashboard.py`](backtest/dashboard.py) | Fiches de trade (quoi, pourquoi, historique du signal, aujourd'hui) et page « Desk Smart Money » |
| [`backtest/phase1_data.py`](backtest/phase1_data.py) | Circuit de données réelles gratuites (SEC 13F, OpenFIGI, secteurs SIC, cours Tiingo, positions des banques CFTC) — marche à suivre : [`docs/PHASE1_DONNEES_REELLES.md`](docs/PHASE1_DONNEES_REELLES.md) |
| [`backtest/tradingview_bridge.py`](backtest/tradingview_bridge.py) | Export vers TradingView : watchlist et indicateur Pine « journal du fonds » par ligne active |
| [`tradingview/`](tradingview/) | Indicateurs Pine *Smart Money Flow Monitor* et *Empreinte des grands acteurs*, récepteur des alertes webhook |
| [`mcp_server/`](mcp_server/) | Serveur MCP du fonds (lecture seule) : interroger le fonds en français depuis un assistant IA |
| [`backtest/ameliorations.py`](backtest/ameliorations.py) | Banc d'essai des améliorations (calibrage des indices, vrais flux des ETF, météo du marché, comptes des entreprises, contrôle des risques) ; résultats dans [`docs/RESULTATS_AMELIORATIONS.md`](docs/RESULTATS_AMELIORATIONS.md) |
| [`tests/`](tests/) | 84 tests, dont le test d'absence de biais d'anticipation (perturbation du futur) |

**Décisions du 23/09/2026** : acheteur uniquement, liste Smart Money, actions US d'abord, données
gratuites, risque équilibré, TradingView gratuit, suivi des banques par le prix et le volume
(détail au §9 de la synthèse).

## Démarrage rapide

```bash
pip install -r requirements.txt
python backtest/flow_backtest.py                  # démo sur données SYNTHÉTIQUES
python backtest/flow_backtest.py --tradingview    # + fichiers TradingView dans outputs/tradingview
python backtest/flow_backtest.py --multi-seed 8   # robustesse sur 8 mondes synthétiques
python backtest/dashboard.py                      # page « Desk Smart Money » (outputs/desk_smart_money.html)
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
| `prices.csv` | `date, asset, close[, high, low, volume]` (inclure les titres radiés ; plus haut, plus bas et volume activent l'empreinte et le radar des grands acteurs) |
| `offexchange.csv` (optionnel) | `date, asset, total_volume, short_volume` : volumes hors bourse (FINRA) |
| `ats.csv` (optionnel) | `asset, week_start, published, ats_volume, block_volume, bank_volume, banks` : bourses privées (FINRA, hebdomadaire) |
| `short_interest.csv` (optionnel) | `asset, settlement, available, short_qty` : positions vendeuses déclarées (FINRA) |
| `insiders.csv` (optionnel) | `asset, filing_date, owner, role, value` : achats des dirigeants sur le marché (Form 4) |
| `filings_5pct.csv` (optionnel) | `asset, filing_date, form, filer` : franchissements de 5 % (13D / 13G initiaux) |
| `earnings.csv` (optionnel) | `asset, date` : publications de résultats (jours exclus du radar) |
| `blocks.csv` (optionnel) | `asset, date, time, price, size, notional, venue, side` : gros blocs (Alpaca) |
| `hourly.csv` (optionnel) | `asset, time, high, low, close, volume` : barres horaires, heure de New York (Alpaca) |
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

TradingView sert de couche visuelle ; il ne sert pas à générer les signaux (voir §4.3 de la synthèse).
Avec le plan gratuit retenu, les étapes 1 à 3 suffisent : les alertes du fonds viennent du moteur
(`revue_positions.csv`, section « ALERTES DISTRIBUTION » de la watchlist).
1. **Watchlist** : importer `outputs/tradingview/watchlist_fonds.txt` dans TradingView.
2. **Journal sur le graphique** : coller `outputs/tradingview/pine/<actif>.pine` dans l'éditeur Pine, puis l'ajouter au graphique du symbole indiqué en en-tête.
3. **Indicateurs du fonds** : coller `tradingview/smart_money_flow_monitor.pine` et `tradingview/empreinte_grands_acteurs.pine` dans l'éditeur Pine (le second trace la zone où les grands acteurs ont échangé l'essentiel du volume).
4. **Alertes webhook (plan payant uniquement)** : renseigner le secret dans l'indicateur, lancer le récepteur, puis le placer derrière un reverse proxy HTTPS (port 443) :
   ```bash
   TV_WEBHOOK_SECRET="un-secret-long" python tradingview/webhook_receiver.py --port 8080
   ```
   Côté TradingView, créer une alerte sur l'indicateur (« N'importe quel appel de fonction alert() ») avec l'URL du webhook.

Les scripts Pine n'ont pas pu être compilés dans l'environnement de développement (pas d'accès
à TradingView) : les vérifier dans l'éditeur Pine lors du premier import.

## Serveur MCP du fonds

Pour poser des questions au fonds en français depuis un assistant IA : « où sont les grands
acteurs sur le cuivre ? », « pourquoi le fonds a-t-il vendu ? ». Le serveur est en lecture seule,
ne passe aucun ordre et n'accède à aucun compte TradingView (voir §4.4 de la synthèse).

- **Claude Code** : le fichier `.mcp.json` du dépôt déclare le serveur `fonds-flux` ; l'accepter à
  l'ouverture du projet.
- **Claude Desktop** : ajouter dans la configuration des serveurs MCP :
  ```json
  {"mcpServers": {"fonds-flux": {"command": "python3", "args": ["/chemin/vers/Hedge-Funds/mcp_server/serveur_fonds.py"]}}}
  ```
- **Données réelles** : définir `FLOWFUND_DATA_DIR` (répertoire de CSV au format ci-dessus) dans
  l'environnement du serveur. Sans cette variable, le serveur répond sur les données synthétiques et
  le signale dans chaque réponse.
