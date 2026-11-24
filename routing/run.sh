#!/bin/sh
# Point d'entrée : « server » (défaut) sert le graphe courant, « import » construit un graphe neuf.
# Variables : JAVA_OPTS (mémoire), GH_GRAPH (dossier du graphe), GH_PBF (extrait à importer).
set -eu
action="${1:-server}"
case "$action" in server|import) ;; *) echo "action inconnue : $action" >&2; exit 2 ;; esac
if [ "$action" = "import" ] && [ -z "${GH_PBF:-}" ]; then echo "GH_PBF requis pour import" >&2; exit 2; fi
exec java ${JAVA_OPTS:--Xmx1536m} \
  ${GH_PBF:+-Ddw.graphhopper.datareader.file="$GH_PBF"} \
  -Ddw.graphhopper.graph.location="${GH_GRAPH:-/data/graphs/current}" \
  -jar /opt/graphhopper/graphhopper-web.jar "$action" /opt/graphhopper/config.yml
