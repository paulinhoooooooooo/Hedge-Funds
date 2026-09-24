# Pistes d'amélioration — résultats sur données réelles (24/09/2026)

Script : `python backtest/pistes_phase1.py` (≈ 30 min ; résultats dans `outputs/pistes/`).
Période commune : 16/08/2018 (premier achat du fonds) → 23/09/2026, coupée en deux moitiés
(avant / après le 01/07/2022) pour repérer les réglages qui ne marchent que par chance.
Sharpe calculé avec un taux sans risque de 2 %.

| Piste | Rendement / an | Sharpe | Pire perte | 1re moitié | 2e moitié | Part investie |
|---|---|---|---|---|---|---|
| Référence (réglages actuels) | +13,3 % | 0,95 | -19 % | +13,1 % | +13,4 % | 48 % |
| S&P 500 (SPY) | +14,8 % | 0,72 | -34 % | +9,5 % | +19,6 % | 100 % |
| 1a. Budget de risque 3 % par ligne | +17,1 % | 0,99 | -21 % | +15,4 % | +18,3 % | 57 % |
| 1b. Budget de risque 4 % par ligne | +18,9 % | 1,03 | -22 % | +14,9 % | +22,4 % | 60 % |
| 1c. 20 lignes, budget 3 % | +17,9 % | 0,96 | -22 % | +16,4 % | +18,9 % | 64 % |
| **1d. 12 lignes de même poids** | **+24,8 %** | **1,26** | -21 % | +16,5 % | +32,5 % | 57 % |
| 1e. 20 lignes de même poids | +20,3 % | 1,12 | -21 % | +18,8 % | +21,3 % | 58 % |
| 2a. Réglages actuels + cash dans le SPY | +20,7 % | 1,01 | -33 % | +20,6 % | +20,5 % | 100 % |
| 2b. 12 lignes de même poids + cash dans le SPY | +30,2 % | 1,25 | -32 % | +22,1 % | +37,6 % | 100 % |
| 3. Gérants : 4 trimestres / 50 | +8,4 % | 0,59 | -22 % | +6,4 % | +10,0 % | 55 % |
| 3. Gérants : 8 trimestres / 20 | +7,9 % | 0,53 | -19 % | +8,7 % | +6,9 % | 50 % |
| 3. Gérants : 8 trimestres / 100 | +9,5 % | 0,67 | -23 % | +9,0 % | +9,7 % | 48 % |
| 4. Cash dans le SPY si banques moins vendeuses | +17,2 % | 1,02 | -21 % | +20,3 % | +14,2 % | — |
| 4. Cash dans le SPY si banques plus vendeuses | +16,6 % | 0,91 | -33 % | +13,3 % | +19,5 % | — |
| 4. SPY seul si banques moins vendeuses | +6,7 % | 0,45 | -23 % | +11,6 % | +2,2 % | 33 % du temps |

Robustesse de la piste 1d (même poids) selon le nombre de lignes : 8 lignes +15,3 % ; 10 lignes
+23,6 % ; 12 lignes +24,8 % ; 15 lignes +24,6 %. Les 5 meilleurs titres (MSTR, LITE, BE, AMD, CIEN)
apportent 48 % du gain : le résultat dépend de quelques gagnants très volatils.

## Conclusions

1. **Investir davantage marche**, surtout avec des lignes de même poids (10 à 15 lignes) : le
   réglage actuel (« budget de risque ») donne trop peu de poids aux titres volatils, qui sont
   justement ceux que les grands gérants font monter.
2. **Placer le cash inutilisé dans le SPY bat le S&P 500 sur les deux moitiés**, mais on retrouve
   alors sa pire perte (-33 %) : c'est un choix de risque, pas un gain gratuit.
3. **La liste actuelle des gérants (8 trimestres, 50 gérants) est la meilleure testée** : ne pas la
   changer. Une fenêtre de 4 trimestres fait démarrer le fonds en 2017, mais rapporte moins.
4. **La position des banques (CFTC) n'apporte pas de signal fiable** : son effet change de sens
   d'une moitié à l'autre. Ne pas l'utiliser pour décider ; la garder comme information.

## Décision (24/09/2026) : pistes 1 et 2 adoptées

Réglages du fonds sur données réelles : 12 lignes de même poids et trésorerie non investie placée
dans le S&P 500. Dans le moteur, le coupe-circuit (perte de 20 %) repasse cette trésorerie en cash
pendant 63 jours ; c'est pourquoi le résultat réel est plus prudent que le test 2b ci-dessus :

| Période | Fonds | S&P 500 |
|---|---|---|
| 2016 → 2026 | +20,4 % / an, Sharpe 1,03, pire perte -27 % | +15,1 % / an, Sharpe 0,78, pire perte -34 % |
| Depuis août 2018 | +21,9 % / an | +14,8 % / an |
| Août 2018 → juin 2022 | +18,8 % / an | +9,5 % / an |
| Juillet 2022 → 2026 | +24,3 % / an | +19,6 % / an |

## Précautions avant d'adopter un réglage

- Réglages choisis en regardant les résultats : le vrai niveau sera plus bas.
- Titres radiés en partie absents (20 % des codes CUSIP non reconnus) : cela flatte surtout les
  réglages concentrés sur les titres volatils.
- Proposition : suivre 1d (+ éventuellement 2) en simulation au jour le jour pendant 3 à 6 mois
  avant d'y mettre de l'argent réel.
