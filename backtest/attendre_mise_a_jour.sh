#!/usr/bin/env bash
# Attend la fin de la mise à jour lancée par lancer_mise_a_jour.sh, 110 minutes au plus par appel.
# Code de sortie : celui de la mise à jour (0, 1 ou 2) si elle est finie ;
#                  3 si elle tourne encore (relancer ce script, jamais la mise à jour) ;
#                  4 si elle s'est arrêtée sans rien écrire (processus disparu).
set -u
cd "$(dirname "$0")/.."
limit=$(( $(date +%s) + ${ATTENTE_MAX_SECONDES:-6600} ))
while [ ! -f outputs/mise_a_jour.fin ]; do
  if [ -f outputs/mise_a_jour.pid ] && ! kill -0 "$(cat outputs/mise_a_jour.pid)" 2>/dev/null; then
    sleep 5  # laisse au processus le temps d'écrire son code de sortie
    [ -f outputs/mise_a_jour.fin ] && break
    echo "La mise à jour s'est arrêtée sans finir. Fin du journal :"
    tail -5 outputs/mise_a_jour.log 2>/dev/null
    exit 4
  fi
  if [ "$(date +%s)" -ge "$limit" ]; then
    echo "Toujours en cours. Dernière étape : $(grep '^===' outputs/mise_a_jour.log 2>/dev/null | tail -1)"
    exit 3
  fi
  sleep 30
done
code=$(cat outputs/mise_a_jour.fin)
echo "Mise à jour terminée (code $code). Fin du journal :"
tail -5 outputs/mise_a_jour.log 2>/dev/null
exit "$code"
