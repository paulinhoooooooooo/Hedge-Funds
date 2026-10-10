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
    --sizing equal --max-positions 12 --idle-cash-in-market --output-dir outputs/phase1 --tradingview
```

Réglages adoptés par les fondateurs le 24/09/2026 (`docs/PISTES_AMELIORATION.md`) : 12 lignes de même
poids et trésorerie non investie placée dans le S&P 500 (`market.csv`, écrit par l'étape `build`). La page
Desk et le serveur MCP les appliquent d'office (`smart_money.phase1_config`).

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

### Rendement de chaque action en direct (sur un ordinateur)

La page en ligne montre les cours de clôture de la veille (mise à jour du matin). Pour voir le
cours et le rendement de chaque action du portefeuille réel **en direct** (actualisés chaque
minute, source Alpaca IEX gratuite), lancer sur un ordinateur (Windows, Mac, Linux) :

```bash
python backtest/temps_reel.py               # ouvre http://127.0.0.1:8765 dans le navigateur
python backtest/temps_reel.py --mise-a-jour   # régénère d'abord la page avec les données du jour
```

Les clés Alpaca restent sur l'ordinateur : variables d'environnement, ou fichier `outputs/cles.env`
(jamais versionné) avec les lignes `ALPACA_API_KEY_ID=…` et `ALPACA_API_SECRET_KEY=…`. Sans clés,
ou si Alpaca ne répond pas, la page garde les valeurs du matin. Le serveur n'écoute que
l'ordinateur lui-même (127.0.0.1).

## 2 ter. Mise à jour automatique chaque soir (et garde-fous)

`python backtest/mise_a_jour.py --etat <page Desk de la veille>` enchaîne tout : téléchargement (chaque
étape retentée une fois), portefeuille réel, copie Pelosi, page Desk, `outputs/notification.md` et
`outputs/etat_mise_a_jour.json`.

Dans une tâche programmée, la lancer **détachée** : elle dure 2 à 4 heures (le conteneur est vide,
tout est retéléchargé), alors qu'une commande en arrière-plan de Claude Code est arrêtée au bout de
2 heures au plus.

```bash
bash backtest/lancer_mise_a_jour.sh     # rend la main tout de suite (journal : outputs/mise_a_jour.log)
bash backtest/attendre_mise_a_jour.sh   # 0 complète, 2 incomplète, 1 échec ; 3 = toujours en cours :
                                        # relancer ce script d'attente, jamais la mise à jour
```

- **Téléchargements coupés en route** : une réponse plus courte que sa taille annoncée est refaite ;
  une archive SEC abîmée laissée par un essai précédent est retéléchargée.
- **Portefeuille réel** : démarré vide le 24/09/2026 (`smart_money.LIVE_START`) ; seuls les achats et
  renforcements décidés depuis y entrent ; le coupe-circuit de -20 % repart de zéro ce jour-là.
- **Données manquantes ou cours périmés** : mise à jour déclarée incomplète, aucun mouvement annoncé,
  la page de la veille reste en place (le conteneur étant vide, une étape en échec = données absentes).
- **Aucun mouvement perdu ni annoncé deux fois** : la page Desk publiée garde un petit état
  (`<script id="etat-desk">` : dernière séance signalée, déclarations Pelosi déjà vues, heure de mise à
  jour). Une soirée manquée est rattrapée le lendemain ; un jour férié n'annonce rien.
- **Pelosi** : une déclaration pas encore en ligne ou illisible est signalée (lien) et retentée.

Notifications : application gratuite **ntfy** (`backtest/notifier.py`). Le nom du sujet est secret : il
n'est jamais écrit dans ce dépôt public, il est passé par la variable `NTFY_TOPIC` dans les tâches
programmées. Chaque mise à jour envoie son résumé (mouvements, ou « aucun mouvement », ou « ⚠️ »).

Tâches programmées (réglages Claude → Routines) :
- « Fonds — mise à jour du matin » : mardi → samedi, 6 h 30 UTC (séance de la veille à New York) ;
  republie la page Desk seulement si la mise à jour est complète, puis envoie la notification.
- « Fonds — contrôle de midi » : mardi → samedi, 11 h UTC ; envoie une alerte ntfy si la page n'a pas
  été mise à jour le matin (panne, crédits épuisés…).

Ce dont la chaîne dépend (à ne pas changer sans prévenir) : la branche `claude/sharp-pasteur-rnlt5h`,
les réglages de l'environnement (`SEC_CONTACT_EMAIL`, `ALPACA_API_KEY_ID`, `ALPACA_API_SECRET_KEY`),
le dépôt public (s'il devient privé, la tâche du soir doit recevoir l'accès au dépôt), et la page Desk
https://claude.ai/artifact/6gUzpPqWQd6FEwMwn2pKz9.

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
   - le fonds n'achète qu'à partir d'août 2018 : la liste des meilleurs gérants demande 8 trimestres
     de cours (Alpaca : depuis 2016), puis deux déclarations publiées. Comparer aussi le S&P 500
     depuis cette date, pas seulement depuis 2016.
5. La position actuelle des banques sur les contrats S&P 500 et Nasdaq-100 (étape `cot`), comparée
   à leur habitude.
6. Les dernières alertes du radar des grands acteurs (heure, jour, semaine, mois), avec les
   indices complémentaires : bourses privées et banques les plus actives, positions vendeuses,
   achats des dirigeants, franchissements de 5 %, gros blocs (heure, montant, sens, hors bourse).
7. Le lien de la page « Desk Smart Money ».
