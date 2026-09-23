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
python backtest/phase1_data.py status    # SEC_CONTACT_EMAIL doit être « défini »
```

Domaines autorisés nécessaires : `www.sec.gov`, `data.sec.gov`, `api.openfigi.com`,
`publicreporting.cftc.gov`, `api.tiingo.com`.

## 1. Étapes, dans l'ordre

| Étape | Commande | Durée indicative | Contrôle |
|---|---|---|---|
| 13F de la SEC (2013 → aujourd'hui) | `python backtest/phase1_data.py sec` | 30 à 90 min (≈ 50 archives) | nombre d'archives traitées, lignes et gérants par archive |
| Univers | `python backtest/phase1_data.py universe --max-symbols 250` | < 5 min | « N premiers titres par trimestre, M titres distincts » |
| Symboles boursiers | `python backtest/phase1_data.py figi` | ≈ 1 min par 250 titres | part des CUSIP reconnus (> 85 % attendu) |
| Secteurs | `python backtest/phase1_data.py sectors` | < 5 min | titres sans code SIC (rattachés à SPY) |
| Cours Tiingo | `nohup python backtest/phase1_data.py prices > data/phase1/prices.log 2>&1 &` | **≈ 75 s par symbole** (50/heure) : ≈ 5 h pour 250 titres | `tail data/phase1/prices.log` ; reprise automatique si relancé |
| Banques (CFTC) | `python backtest/phase1_data.py cot` | < 1 min | position des banques vs leur habitude |
| Fichiers du moteur | `python backtest/phase1_data.py build` | quelques minutes | `data/phase1/engine/resume.json` |

Premier passage conseillé : **250 titres** (`--max-symbols 250`), pour obtenir un résultat en une
demi-journée. L'univers pourra être élargi à 488 le mois suivant (limite Tiingo gratuite : 500
symboles différents par mois).

Les données téléchargées restent dans `data/` (exclu du dépôt : licences des fournisseurs). Si la
session est interrompue, relancer la même commande : chaque étape reprend là où elle s'est
arrêtée ; un symbole déjà téléchargé ne compte pas deux fois dans le quota Tiingo du mois.

## 2. Lancer le backtest réel

```bash
python backtest/flow_backtest.py --data-dir data/phase1/engine --entry-flow 0 --exit-flow -0.05 \
    --output-dir outputs/phase1 --tradingview
```

Les seuils de flux `0` / `-0.05` sont ceux du proxy gratuit (Chaikin Money Flow des ETF
sectoriels, §9.2 de la synthèse). Le serveur MCP du fonds peut ensuite répondre sur ces données :
`FLOWFUND_DATA_DIR=data/phase1/engine`.

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
5. La position actuelle des banques sur les contrats S&P 500 et Nasdaq-100 (étape `cot`), comparée
   à leur habitude.
