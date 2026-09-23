# Améliorations de l'analyse : résultats sur données réelles

Données : 73 grandes actions américaines (plus grosses lignes des déclarations 13F), cours Alpaca depuis 2016, déclarations SEC depuis 2013. Mesure du 04/01/2019 au 23/09/2026 (les trois premières années servent à calibrer). Programme : `python backtest/ameliorations.py`.

## Tout l'univers : 73 actions

Période mesurée : 04/01/2019 → 23/09/2026

| Variante | Rendement annuel | Sharpe | Pire perte | Achats | Gagnants | Exposition |
|---|---|---|---|---|---|---|
| Référence : acheter et garder les mêmes actions | +21.2% | 0.98 | -31.9% | 73 | — | 100% |
| Référence : S&P 500 (SPY) | +17.2% | 0.83 | -33.8% | 1 | — | 100% |
| Programme actuel | +9.7% | 0.72 | -17.7% | 309 | 50% | 62% |
| 1 · Calibrage des indices | +7.2% | 0.52 | -20.4% | 312 | 49% | 62% |
| 2 · Vrais flux des ETF | +8.0% | 0.57 | -15.0% | 290 | 51% | 62% |
| 3 · Options | non testable : pas d'historique gratuit | | | | | |
| 4 · Météo du marché | +8.6% | 0.65 | -17.3% | 300 | 48% | 60% |
| 5 · Comptes des entreprises | +7.9% | 0.63 | -14.2% | 303 | 53% | 62% |
| 6 · Contrôle des risques | +4.2% | 0.30 | -13.5% | 286 | 47% | 44% |
| 7 · Actualités | non testable : clés Alpaca absentes de cette session | | | | | |
| 8 · Coût d'emprunt | non testable : pas d'historique gratuit | | | | | |
| 1 + 4 + 6 + 2 ensemble | +5.6% | 0.48 | -15.5% | 257 | 53% | 45% |


## Moyenne de 30 tirages de 3 actions

| Variante | Rendement annuel | Sharpe | Pire perte | Bat le programme actuel |
|---|---|---|---|---|
| Référence : acheter et garder les mêmes actions | +20.9% | 0.77 | -39.5% | 90% des tirages |
| Référence : S&P 500 (SPY) | +17.2% | 0.83 | -33.8% | 87% des tirages |
| Programme actuel | +6.7% | 0.46 | -16.7% | 0% des tirages |
| 1 · Calibrage des indices | +6.2% | 0.44 | -15.7% | 37% des tirages |
| 2 · Vrais flux des ETF | +6.0% | 0.41 | -16.7% | 37% des tirages |
| 4 · Météo du marché | +5.7% | 0.40 | -16.6% | 23% des tirages |
| 5 · Comptes des entreprises | +4.7% | 0.38 | -14.9% | 30% des tirages |
| 6 · Contrôle des risques | +4.2% | 0.34 | -12.4% | 50% des tirages |
| 1 + 4 + 6 + 2 ensemble | +3.4% | 0.25 | -12.4% | 20% des tirages |


## 3 actions tirées au hasard (graine 2026) : ABBV, BIDU, SPCX

Période mesurée : 04/01/2019 → 23/09/2026

| Variante | Rendement annuel | Sharpe | Pire perte | Achats | Gagnants | Exposition |
|---|---|---|---|---|---|---|
| Référence : acheter et garder les mêmes actions | +9.6% | 0.39 | -36.6% | 3 | — | 100% |
| Référence : S&P 500 (SPY) | +17.2% | 0.83 | -33.8% | 1 | — | 100% |
| Programme actuel | +1.1% | -0.05 | -26.7% | 22 | 41% | 16% |
| 1 · Calibrage des indices | +2.5% | 0.10 | -19.0% | 11 | 45% | 8% |
| 2 · Vrais flux des ETF | +2.4% | 0.09 | -23.6% | 20 | 50% | 17% |
| 3 · Options | non testable : pas d'historique gratuit | | | | | |
| 4 · Météo du marché | -0.4% | -0.27 | -24.1% | 20 | 40% | 15% |
| 5 · Comptes des entreprises | +1.3% | -0.05 | -26.1% | 18 | 44% | 13% |
| 6 · Contrôle des risques | +2.7% | 0.13 | -12.3% | 21 | 43% | 13% |
| 7 · Actualités | non testable : clés Alpaca absentes de cette session | | | | | |
| 8 · Coût d'emprunt | non testable : pas d'historique gratuit | | | | | |
| 1 + 4 + 6 + 2 ensemble | +3.7% | 0.36 | -8.1% | 9 | 78% | 7% |


## Ce que chaque indice a prédit (calibrage 2026, rendement des 3 mois suivants)

| Indice | Corrélation de rang |
|---|---|
| argent des fonds | +0.019 |
| empreinte prix / volume | +0.018 |
| radar cinq pourcent | +0.017 |
| détention des gérants | +0.017 |
| radar force | +0.009 |
| radar ventes a decouvert | +0.005 |
| radar jours | +0.004 |
| radar jour | +0.004 |
| radar bourses privees | +0.002 |
| radar divergence | -0.000 |
| radar mois | -0.001 |
| radar semaine | -0.002 |
| radar dirigeants | -0.006 |

## Lecture

- **Aucune amélioration ne bat le programme actuel** sur l'ensemble des 73 actions, ni en moyenne
  sur 30 tirages de 3 actions (chacune ne gagne que dans 20 à 50 % des tirages : du hasard).
- Le **contrôle des risques** et les **comptes des entreprises** réduisent la pire perte
  (−12 à −14 % au lieu de −17 %), au prix d'un rendement plus faible.
- Sur ces grandes actions, **aucun indice pris seul n'annonce le rendement des 3 mois suivants**
  (corrélations inférieures à 0,02) : le calibrage ne trouve rien à renforcer.
- Le programme actuel gagne **moins que le S&P 500** sur la période (+9,7 % par an contre +17,2 %),
  avec une pire perte plus faible (−17,7 % contre −33,8 %). Il n'est investi qu'à 62 % en moyenne ;
  le reste dort en trésorerie à 2 %.
- Limites : une seule période, très haussière ; univers des plus grosses lignes 13F (qui flatte
  l'achat-conservation : les gagnants y entrent) ; historique des cours depuis 2016 seulement.
