#!/usr/bin/env bash
# Lance la mise à jour du matin DÉTACHÉE du terminal : elle dure 2 à 4 heures, plus que ce qu'une
# commande en arrière-plan de Claude Code peut durer (2 heures au plus, puis elle est arrêtée).
#   bash backtest/lancer_mise_a_jour.sh     # rend la main tout de suite
#   bash backtest/attendre_mise_a_jour.sh   # attend la fin (à relancer s'il répond « toujours en cours »)
# Journal : outputs/mise_a_jour.log ; à la fin, outputs/mise_a_jour.fin contient le code de sortie
# (0 complète, 2 incomplète, 1 échec).
set -u
cd "$(dirname "$0")/.."
mkdir -p outputs
if [ -f outputs/mise_a_jour.pid ] && kill -0 "$(cat outputs/mise_a_jour.pid)" 2>/dev/null; then
  echo "Une mise à jour est déjà en cours (processus $(cat outputs/mise_a_jour.pid)) : rien de relancé."
  exit 0
fi
rm -f outputs/mise_a_jour.fin outputs/mise_a_jour.pid
ARGS=()
[ -f outputs/page_veille.html ] && ARGS=(--etat outputs/page_veille.html)
# Le processus détaché écrit lui-même son numéro (setsid peut créer un processus intermédiaire).
nohup setsid bash -c 'echo $$ > outputs/mise_a_jour.pid
python backtest/mise_a_jour.py "$@" > outputs/mise_a_jour.log 2>&1; echo $? > outputs/mise_a_jour.fin' \
  _ ${ARGS[@]+"${ARGS[@]}"} > /dev/null 2>&1 &
sleep 1
echo "Mise à jour lancée (journal : outputs/mise_a_jour.log)."
