# Suivre les mouvements des grandes plateformes crypto : ça améliore le fonds ?

Question des fondateurs (24/09/2026, captures Arkham) : les bitcoins qui entrent sur Binance, Coinbase,
Kraken… (souvent pour être vendus) et ceux qui en sortent (souvent pour être gardés) peuvent-ils améliorer
la stratégie ?

`python backtest/onchain.py` (≈ 2 min). Données : Coin Metrics Community, gratuites, quotidiennes depuis
2015, agrégées sur toutes les plateformes identifiées (Arkham détaille chaque transfert mais exige une clé
payante). BTC et ETH entrent dans le fonds comme deux lignes possibles parmi les 12, avec les mêmes règles :
- jambe lente : offre totale / réserve des plateformes (+3 % en un trimestre = les grands retirent) ;
- jambe rapide : sorties nettes des plateformes sur 30 jours, en % de leur réserve ;
- donnée du jour J utilisable en J+1, frais 0,20 %, flux du week-end reportés au lundi.

## Résultats (16/08/2018 → 23/09/2026)

| Portefeuille | Par an | Pire perte | Sharpe | 2018-2022 | 2022-2026 |
|---|---|---|---|---|---|
| Fonds actuel (actions) | +21,9 % | -27 % | 1,03 | +18,8 % | +24,3 % |
| Fonds + BTC/ETH pilotés par les flux des plateformes | +25,4 % | **-36 %** | 1,09 | +27,7 % | **+23,1 %** |
| BTC/ETH seuls, pilotés par les flux | +25,2 % | -66 % | 0,72 | +50,6 % | +5,8 % |
| BTC acheté et gardé | +37,8 % | -77 % | 0,80 | +33,6 % | +41,7 % |
| S&P 500 | +14,8 % | -34 % | 0,72 | +9,5 % | +19,6 % |

Le gain vient de **deux transactions** de la bulle 2020-2021 (BTC mai 2020 → mai 2021 : +70 M$ ;
ETH nov. 2020 → juin 2022 : +141 M$). Depuis 2022, le signal perd : ETH 2023 +5 %, BTC depuis janvier 2025
-24 % (ligne encore ouverte), et la stratégie crypto seule ne fait que +5,8 % par an contre +41,7 % pour
le bitcoin gardé.

## Conclusion : ne pas l'ajouter au fonds pour l'instant

1. Amélioration due à 2 paris chanceux d'une seule période ; la seconde moitié est moins bonne.
2. Pire perte nettement plus forte (-36 % au lieu de -27 %).
3. Depuis les ETF bitcoin (2024), une grande partie des jetons « sur les plateformes » est en réalité la
   conservation des ETF (Coinbase Custody…) : le signal entrées / sorties est brouillé.
4. Biais possible : Coin Metrics identifie les adresses des plateformes au fil du temps et peut réécrire
   l'historique des réserves.

Piste possible : afficher ces flux sur la page Desk comme information (radar), sans décider avec.
