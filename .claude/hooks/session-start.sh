#!/bin/bash
# Installe les dépendances Python du fonds au démarrage d'une session Claude Code sur le web :
# le serveur MCP « fonds-flux », les tests et la mise à jour quotidienne en ont besoin.
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "$CLAUDE_PROJECT_DIR"
# PyJWT est installé par le système (Debian) sans fichier RECORD : pip ne peut pas le remplacer.
python3 -m pip install --quiet --disable-pip-version-check --ignore-installed PyJWT -r requirements.txt
