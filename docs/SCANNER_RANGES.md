# Scanner des ranges : modèles d'accumulation et de distribution

Chaque nuit, le scanner passe en revue le **S&P 500 et les 20 premières cryptos** (hors monnaies
stables). Il cherche les ranges en **W, D et 4h**. Il t'envoie une notification sur le téléphone (ntfy)
dès qu'un range a fait **2 touches du même côté, suivies d'un retour au milieu**. Sans retour au
milieu, il n'envoie rien.

| Fichier | Rôle |
|---|---|
| [`backtest/zigzag.py`](../backtest/zigzag.py) | Ton Zig Zag « 2 bougies », recodé à l'identique ([`tradingview/zigzag_2_bougies.pine`](../tradingview/zigzag_2_bougies.pine)) |
| [`backtest/modeles_range.py`](../backtest/modeles_range.py) | Ranges, décalages, mort, zones d'offre / de demande, liquidité, touches, alertes |
| [`backtest/scanner_ranges.py`](../backtest/scanner_ranges.py) | Univers, cours Alpaca, rapport, notification |
| [`.github/workflows/scanner_ranges.yml`](../.github/workflows/scanner_ranges.yml) | Passage de nuit sur GitHub, rapport déposé sur la branche `ranges-rapport` |

```bash
python backtest/scanner_ranges.py --symboles JNJ,AAPL,BTC/USD   # quelques actifs (≈ 1 min)
python backtest/scanner_ranges.py                               # tout l'univers
```

Résultats dans `outputs/ranges/` :
- `rapport_ranges.md` : alertes, 2e touches en attente du retour au milieu, modèles en cours ;
- `ranges.csv` : tous les ranges vivants, avec leurs niveaux, zones, liquidités et touches ;
- `etat_ranges.json` : les alertes déjà envoyées, pour ne jamais envoyer deux fois la même.

> Repérage automatique de tes règles, à vérifier sur le graphique. Ce n'est pas un conseil
> d'investissement.

## Tes règles, telles que le scanner les applique

### 1. Les hauts et les bas
Le scanner utilise ton Zig Zag « 2 bougies » avec ses réglages par défaut :
- 2 bougies consécutives pour valider ;
- pivot au plus haut ou au plus bas de la phase, sur les mèches ;
- bougies intérieures ou englobées de même couleur ignorées.

Il a été vérifié sur J&J en journalier : il trouve les mêmes sommets et creux que sur ta capture
TradingView (244,6 / 231,4 / 241,6 / 233,0 / … / 281,1 / 260,7 / 273,7 / 251,2 / 282,0).

Un pivot n'est utilisé qu'à partir de la bougie qui l'a validé, comme sur le graphique en direct.

### 2. Le range
- **Formation** : un haut et un bas consécutifs du Zig Zag, après une hausse ou une baisse.
  Le retracement de Fibonacci donne 0 (haut), 0,5 (milieu), 1 (bas), -0,3 et 1,3.
- **Validation** : le prix doit revenir au 0,5 (une mèche suffit) après le dernier point du
  mouvement. Sinon le range n'existe pas.
- **Pendant le repli**, un petit creux (ou sommet) intermédiaire ne remplace pas le range en attente.
  Si le prix repart au-delà de l'extrême, la dernière jambe du Zig Zag devient le nouveau mouvement.
- **Un seul range à la fois par unité de temps** : une fois validé, on le garde jusqu'à sa mort. Les
  petits mouvements à l'intérieur ne créent pas de nouveau range.
- **Décalage** :
  - une mèche ou une clôture entre 0 et -0,3 déplace le haut sur cette mèche, et le 0,5 est recalculé ;
  - une mèche au-delà de -0,3 avec une clôture revenue en dessous : le range reste valide et le haut
    se décale aussi ;
  - même chose en bas entre 1 et 1,3.
- **Mort** : une clôture au-delà de -0,3 ou de 1,3.

### 3. Les touches
- Le haut et le bas qui forment le range sont la **1re touche** de chaque côté. Les deux côtés sont
  suivis en parallèle : en haut pour une **distribution**, en bas pour une **accumulation**.
- Après un **retour au milieu**, il y a une nouvelle touche quand le prix atteint, avec une mèche ou
  une clôture, l'un de ces niveaux (le plus proche du milieu compte) :
  - la **zone d'offre** (ou de demande) ;
  - le **niveau de liquidité** ;
  - le haut (ou le bas) du range, ou au-delà, ce qui décale le range.
- **ALERTE** : 2e touche, puis retour au milieu, sans que le range soit mort entre-temps.

### 4. La zone d'offre / de demande
On cherche la zone de la 1re touche en partant de l'unité de temps du range, puis en descendant
(W → D → 4h → 1h → 30 min → 15 min → 5 min). On ne descend que si on ne trouve pas.

- **Distribution** : on prend la bougie verte de l'extrême, ou à défaut la bougie verte juste avant.
  Elle doit avoir une mèche haute d'au moins **40 %** de sa hauteur. La zone va du haut de la mèche
  à la clôture.
- **Accumulation** : on prend la bougie rouge de l'extrême, ou à défaut la bougie rouge juste avant.
  Elle doit avoir une mèche basse d'au moins 40 %. La zone va du bas de la mèche à la clôture.

### 5. Le niveau de liquidité
On prend la jambe du Zig Zag entre la 1re touche et le retour au milieu, avec la même cascade
d'unités de temps.
- **Distribution** : premier sommet intermédiaire après la touche. Il doit être entre 0 et 0,25.
- **Accumulation** : premier creux intermédiaire après la touche. Il doit être entre 0,75 et 1.

Si la jambe est droite (aucun point intermédiaire) ou si le point est trop près du milieu, on passe
à l'unité de temps inférieure. Plus l'unité de temps est haute, plus le niveau est solide : le
rapport indique où il a été trouvé.

## Tes choix (questions du 08/11/2026)

| Question | Réponse |
|---|---|
| Actifs | S&P 500 + top 20 crypto |
| Unités de temps du range | W, D et 4h, chacune à part |
| Petits ranges à l'intérieur | Garder le grand range jusqu'à sa mort |
| 1re touche | Le haut et le bas du range comptent ; les deux côtés sont suivis |
| Décalage | Décaler sur la mèche et recalculer tous les niveaux |
| Mèche « importante » | Au moins 40 % de la hauteur de la bougie |
| Alertes | ntfy + rapport, chaque nuit ; à la 2e touche suivie d'un retour au milieu |

## Précisions ajoutées (à valider)

- **La limite -0,3 ne suit pas le haut pendant une sortie.** Quand le prix sort par le haut, le
  haut se décale sur chaque nouvelle mèche. La limite -0,3 reste celle du range d'avant la sortie,
  et elle est recalculée au retour au milieu. Sans cette précaution, une tendance qui monte bougie
  après bougie élargirait le range sans fin et ne le tuerait jamais.
- **Données comme ton TradingView** : actions ajustées des dividendes (ton graphique J&J l'est),
  séance de New York 9 h 30 - 16 h, barres 1h et 4h ancrées sur 9 h 30. Pour la crypto, les journées
  et les semaines commencent à 0 h UTC.
- **Seules les bougies terminées comptent.** Une alerte en W peut donc attendre la clôture de la semaine.
- **Alertes envoyées** : celles des 5 derniers jours, une seule fois chacune, et seulement si le range
  est encore vivant.
- Les cours crypto viennent d'Alpaca (paires en dollars). Les mèches peuvent différer un peu de
  celles d'une autre plateforme (Binance, Coinbase).

## À revoir ensemble

- **Le retour au milieu entre chaque touche** : tu voulais qu'on en reparle. Pour l'instant, il faut
  un retour au 0,5 après chaque touche, et la 2e touche suivie d'un retour au milieu déclenche l'alerte.
- Les 3e touches sont comptées dans le rapport, mais elles n'envoient pas d'alerte.
