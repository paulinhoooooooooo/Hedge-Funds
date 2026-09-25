# Suivre l'argent qui entre dans les ETF bitcoin (BlackRock IBIT…)

`python backtest/etf_btc.py` (quelques secondes). Données : TFTC (licence CC BY 4.0, sources SoSoValue et
Farside), flux nets quotidiens de chaque ETF bitcoin américain depuis leur lancement le 11/01/2024
(693 séances). Le flux d'une séance n'est connu qu'après la clôture : la copie agit le lendemain ; frais
0,20 % par achat ou vente ; trésorerie rémunérée 2 %.

## Résultats (11/01/2024 → 23/09/2026)

| Règle | Par an | Pire perte | 1re moitié | 2e moitié |
|---|---|---|---|---|
| Bitcoin acheté et gardé | +25 % | -53 % | +122 % | -18 % |
| Bitcoin seulement si BlackRock a fait entrer de l'argent sur 5 séances | +49 % | -30 % | +162 % | +12 % |
| Bitcoin seulement si tous les ETF ont fait entrer de l'argent sur 20 séances | +50 % | -33 % | +123 % | +33 % |
| Contrôle : bitcoin seulement s'il a monté sur 5 séances (prix seul) | +5 % | -38 % | +12 % | +1 % |
| Contrôle : bitcoin seulement s'il a monté sur 20 séances (prix seul) | +10 % | -39 % | +42 % | -10 % |

Robustesse (16 variantes : BlackRock ou tous les ETF, 3 à 20 séances, avec ou sans un jour de retard
supplémentaire) : toutes font entre +16 % et +50 % par an avec une pire perte de -26 % à -40 %, contre
+25 % et -53 % pour le bitcoin gardé ; investi 45 à 67 % du temps. Les règles « prix seul » font bien
moins : les flux des ETF apportent une information que le prix ne contient pas.

## Conclusion

Signal prometteur, contrairement aux flux des plateformes (docs/ONCHAIN.md) : il a battu le bitcoin
gardé sur les deux moitiés, avec des pertes bien plus faibles. Mais l'historique ne couvre que 2 ans et
8 mois (un seul cycle), et le résultat varie beaucoup selon le réglage (+16 % à +50 %). À suivre en
simulation avant tout argent réel. Le fonds actuel (actions américaines) n'est pas modifié.
