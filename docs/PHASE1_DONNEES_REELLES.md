# Phase 1 — Premier backtest sur données réelles (actions américaines, budget nul)

Marche à suivre pour l'équipe (et pour toute nouvelle session de Claude Code). Les fondateurs
ont fourni, dans les réglages de l'environnement :
- `SEC_CONTACT_EMAIL` : adresse dédiée au fonds, exigée par la SEC ;
- la clé Tiingo : dans les « API credentials » de l'environnement (hôte `api.tiingo.com`,
  en-tête `Authorization: Token …`) ou dans la variable `TIINGO_API_KEY`.

**Ne jamais demander, afficher ni écrire la clé Tiingo** (ni dans la conversation, ni dans un fichier).

## 0. Vérifier l'environnement

```bash
pip install -r requirements.txt pyarrow
python -m pytest -q                      # tous les tests doivent passer
python backtest/phase1_data.py status    # SEC_CONTACT_EMAIL et ALPACA doivent être « défini »
```

Clés Alpaca saisies à l'envers dans les réglages (l'identifiant commence par `PK`) : le script les remet
dans l'ordre tout seul.

Domaines autorisés nécessaires : `www.sec.gov`, `data.sec.gov`, `api.openfigi.com`,
`publicreporting.cftc.gov`, `api.tiingo.com`, `cdn.finra.org`, `api.finra.org`, `data.alpaca.markets` (l'environnement est en accès
réseau « Complet » : rien à ajouter).

## 1. Étapes, dans l'ordre

| Étape | Commande | Durée indicative | Contrôle |
|---|---|---|---|
| 13F de la SEC (2013 → aujourd'hui) | `python backtest/phase1_data.py sec` | 30 à 90 min (≈ 50 archives) | nombre d'archives traitées, lignes et gérants par archive |
| Univers | `python backtest/phase1_data.py universe --max-symbols 250` | < 5 min | « N premiers titres par trimestre, M titres distincts » |
| Symboles boursiers | `python backtest/phase1_data.py figi` | ≈ 1 min par 250 titres | part des CUSIP reconnus (> 85 % attendu) |
| Secteurs | `python backtest/phase1_data.py sectors` | < 5 min | titres sans code SIC (rattachés à SPY) |
| Cours | `python backtest/phase1_data.py prices` | **avec les clés Alpaca : quelques minutes** (historique depuis 2016, réécrit à chaque passage) ; sans elles, Tiingo : ≈ 75 s par symbole, ≈ 5 h pour 250 titres (lancer avec `nohup … &`) | nombre de symboles écrits dans `data/phase1/prices/` |
| Hors bourse (FINRA) | `python backtest/phase1_data.py finra` | 20 à 40 min (depuis août 2018) | un fichier par mois dans `data/phase1/finra/` |
| Banques (CFTC) | `python backtest/phase1_data.py cot` | < 1 min | position des banques vs leur habitude |
| Bourses privées (FINRA) | `python backtest/phase1_data.py ats` | 30 à 60 min (depuis 2022) | un fichier par semaine dans `data/phase1/ats/` |
| Positions vendeuses (FINRA) | `python backtest/phase1_data.py short` | < 5 min | nombre de rapports et de titres |
| Résultats et seuils de 5 % (SEC) | `python backtest/phase1_data.py events` | 5 à 20 min (certaines banques ont des milliers de dépôts) | publications de résultats, franchissements de 5 % |
| Heure par heure et gros blocs (Alpaca) | `python backtest/phase1_data.py alpaca` | 5 à 15 min (20 dernières séances) ; ensuite chaque soir, quelques minutes | `data/phase1/hourly.csv`, un fichier par séance dans `data/phase1/blocks/` |
| Achats des dirigeants (SEC) | `python backtest/phase1_data.py insiders` | 10 à 30 min (≈ 8 Mo par trimestre depuis 2019, puis Form 4 récents) | achats sur le marché, date du dernier jeu trimestriel |
| Fichiers du moteur | `python backtest/phase1_data.py build` | quelques minutes | `data/phase1/engine/resume.json` (`hors_bourse: true`, `indices_radar` : les 5 tables) |
| Noms des gérants | `python backtest/phase1_data.py names` | < 5 min | gérants identifiés |
| Acheteurs et vendeurs | `python backtest/phase1_data.py buyers` | < 1 min | `data/phase1/engine/smart_money_buyers.csv` |

Avec les clés Alpaca, tout l'univers (488 titres) passe d'une traite : `python backtest/phase1_data.py all`
(2 à 4 heures, surtout la SEC et la FINRA). Sans elles, premier passage conseillé à **250 titres**
(`--max-symbols 250`) à cause du rythme de Tiingo.

Les données téléchargées restent dans `data/` (exclu du dépôt : licences des fournisseurs). **Elles
ne suivent pas d'une conversation à l'autre** : chaque nouvelle conversation repart d'un conteneur
vide et relance les étapes. Si la session est interrompue, relancer la même commande : chaque
étape reprend là où elle s'est arrêtée ; un symbole déjà téléchargé ne compte pas deux fois dans
le quota Tiingo du mois.

## 2. Lancer le backtest réel

```bash
python backtest/flow_backtest.py --data-dir data/phase1/engine --entry-flow 0 --exit-flow -0.05 \
    --output-dir outputs/phase1 --tradingview
```

Les seuils de flux `0` / `-0.05` sont ceux du proxy gratuit (Chaikin Money Flow des ETF
sectoriels, §9.2 de la synthèse). Le serveur MCP du fonds peut ensuite répondre sur ces données :
`FLOWFUND_DATA_DIR=data/phase1/engine`.

## 2 bis. Page « Desk Smart Money » (ce que les fondateurs regardent)

```bash
python backtest/dashboard.py --data-dir data/phase1/engine --out outputs/desk_smart_money.html
```

Publier ce fichier comme page web privée (outil Artifact) en **remplaçant la démonstration au
même lien** : `https://claude.ai/artifact/6gUzpPqWQd6FEwMwn2pKz9` (paramètre `url`), puis donner
le lien aux fondateurs.
Chaque fiche doit montrer, sur données réelles, le nombre d'acheteurs et de vendeurs parmi les
50 meilleurs gérants, les noms des principaux acheteurs, le radar et la position des banques.

## 3. Ce qu'il faut rapporter aux fondateurs (en langage simple)

1. Performance et risque par rapport au S&P 500 (SPY) sur la même période : rendement annuel,
   pire perte, temps de récupération, Sharpe.
2. Ce qui a déclenché les ventes (distribution confirmée, alerte, stop) et la durée de détention.
3. Le contrôle du biais d'anticipation (délais ignorés vs réels).
4. Les limites de ce premier test, sans les minimiser :
   - titres radiés partiellement absents (codes CUSIP non reconnus par OpenFIGI) : biais du
     survivant résiduel, qui flatte les résultats ;
   - jambe rapide = proxy volume, pas de vrais flux de fonds ;
   - secteurs approximés par le code SIC ;
   - une seule période historique : aucun résultat n'est une promesse.
   - le fonds n'achète qu'à partir d'août 2018 (début des fichiers hors bourse de la FINRA) :
     comparer aussi le S&P 500 depuis cette date, pas seulement depuis 2016.
5. La position actuelle des banques sur les contrats S&P 500 et Nasdaq-100 (étape `cot`), comparée
   à leur habitude.
6. Les dernières alertes du radar des grands acteurs (heure, jour, semaine, mois), avec les
   indices complémentaires : bourses privées et banques les plus actives, positions vendeuses,
   achats des dirigeants, franchissements de 5 %, gros blocs (heure, montant, sens, hors bourse).
7. Le lien de la page « Desk Smart Money ».
