# Copier Nancy Pelosi : backtest et suivi réel

`python backtest/pelosi.py` (≈ 1 min) télécharge ses déclarations de transactions officielles (greffe de la
Chambre des représentants, STOCK Act), les copie et les compare à notre fonds. Résultats dans
`outputs/pelosi/` ; la page Desk affiche le tableau « Face à Nancy Pelosi ».

Règles de la copie : achat ou vente le lendemain de la PUBLICATION (en moyenne 26 jours après sa
transaction, jusqu'à 45 jours) ; montant = milieu de la fourchette déclarée ; vente partielle = moitié
de la ligne ; ses options sont copiées comme des actions (approximation : ni levier, ni options expirées
sans valeur) ; frais 0,10 %. Les déclarations scannées avant septembre 2018 sont illisibles.

## Résultats (07/09/2018 → 24/09/2026)

| Portefeuille | Par an | Pire perte | Sharpe | 2018-2022 | 2022-2026 |
|---|---|---|---|---|---|
| Pelosi elle-même (date de transaction, non copiable) | +19,1 % | -49 % | 0,69 | +7,6 % | +30,4 % |
| Copie Pelosi (date de publication) | +17,6 % | -48 % | 0,65 | +4,7 % | +30,3 % |
| Copie Pelosi, actions seulement | +14,2 % | -49 % | 0,55 | +4,2 % | +23,7 % |
| **Notre fonds (Smart Money)** | **+21,9 %** | **-27 %** | **1,03** | +18,9 % | +24,3 % |
| S&P 500 (SPY) | +14,7 % | -34 % | 0,71 | +9,3 % | +19,5 % |

## Suivi réel (« front test »)

Depuis le 24/09/2026, la copie Pelosi et le fonds démarrent vides. Chaque soir, `backtest/mise_a_jour.py`
relit les nouvelles déclarations : la notification signale toute nouvelle transaction publiée.
