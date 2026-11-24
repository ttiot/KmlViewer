#!/usr/bin/env bash
# Mise à jour du graphe de routage (ADR-0007, TR7) : téléchargement -> fusion -> import -> bascule.
# À lancer depuis l'hôte, chaque mois (cron / timer systemd, par exemple posé par Ansible) :
#   /chemin/du/projet/routing/update.sh
# Relançable sans risque : verrou, graphe neuf dans un dossier horodaté, bascule atomique d'un lien
# symbolique, l'ancien graphe sert jusqu'au succès et revient en cas d'échec de démarrage.
#
# Configuration, par ordre de priorité : environnement du processus (ex. unité systemd posée par Ansible),
# puis .env du projet (lignes CLE=valeur, sans guillemets imbriqués ni commentaire en fin de ligne ; le
# fichier n'est JAMAIS exécuté), puis valeur par défaut. Toute valeur est validée avant usage.
#   ROUTING_EXTRACT_URLS         extraits .osm.pbf, séparés par des espaces (défaut : Nord-Pas-de-Calais,
#                                Picardie, Belgique de Geofabrik ; chacun doit avoir son .md5 à côté)
#   ROUTING_ALLOWED_HOSTS        hôtes autorisés pour ces URL, séparés par des espaces (défaut download.geofabrik.de)
#   ROUTING_KEEP_GRAPHS          graphes conservés, courant compris : 2 à 20 (défaut 2 ; 2 = retour arrière possible)
#   ROUTING_HEALTH_TIMEOUT       attente de santé du moteur après bascule, 5 à 3600 s (défaut 300)
#   ROUTING_MAX_DOWNLOAD_BYTES   taille maximale d'un extrait téléchargé (défaut 3 Gio)
#   ROUTING_LOCK                 fichier de verrou (défaut .routing-update.lock dans le dossier du projet)
# Les variables lues par docker-compose.yml (ROUTING_XMX, ROUTING_IMPORT_XMX, ROUTING_*_MEM_LIMIT) vont dans .env.
set -euo pipefail

cd "$(dirname "$0")/.."
[ -f docker-compose.yml ] || { echo "docker-compose.yml introuvable à $(pwd)" >&2; exit 1; }

die() { echo "routing-update: configuration invalide : $*" >&2; exit 2; }

# --- Lecture de la configuration (aucune évaluation du contenu) --------------------------------
dotenv() {  # dernière valeur de la clé $1 dans .env, sans guillemets autour ; vide si absente
  [ -f .env ] || return 0
  local line v
  line="$(grep -E "^$1=" .env | tail -n 1 || true)"
  v="${line#*=}"; v="${v%$'\r'}"
  case "$v" in \"*\") v="${v#\"}"; v="${v%\"}" ;; \'*\') v="${v#\'}"; v="${v%\'}" ;; esac
  printf '%s' "$v"
}
cfg() {  # cfg CLE DEFAUT : environnement > .env > défaut
  local v
  if [ -n "${!1+x}" ]; then v="${!1}"; else v="$(dotenv "$1")"; fi
  printf '%s' "${v:-$2}"
}

DEFAULT_URLS="https://download.geofabrik.de/europe/france/nord-pas-de-calais-latest.osm.pbf https://download.geofabrik.de/europe/france/picardie-latest.osm.pbf https://download.geofabrik.de/europe/belgium-latest.osm.pbf"
URLS="$(cfg ROUTING_EXTRACT_URLS "$DEFAULT_URLS")"
ALLOWED_HOSTS="$(cfg ROUTING_ALLOWED_HOSTS download.geofabrik.de)"
KEEP="$(cfg ROUTING_KEEP_GRAPHS 2)"
HEALTH_TIMEOUT="$(cfg ROUTING_HEALTH_TIMEOUT 300)"
MAX_BYTES="$(cfg ROUTING_MAX_DOWNLOAD_BYTES 3221225472)"
LOCK="$(cfg ROUTING_LOCK "$PWD/.routing-update.lock")"

# --- Validation : entiers bornés, hôtes et URL https au format strict, aucun caractère spécial ----
int_in() {  # int_in VALEUR MIN MAX
  [[ "$1" =~ ^[0-9]{1,10}$ ]] && [ "$((10#$1))" -ge "$2" ] && [ "$((10#$1))" -le "$3" ]
}
int_in "$KEEP" 2 20 || die "ROUTING_KEEP_GRAPHS doit être un entier de 2 à 20"
int_in "$HEALTH_TIMEOUT" 5 3600 || die "ROUTING_HEALTH_TIMEOUT doit être un entier de 5 à 3600"
int_in "$MAX_BYTES" 1048576 107374182400 || die "ROUTING_MAX_DOWNLOAD_BYTES doit être un entier de 1048576 à 107374182400"
[[ "$LOCK" =~ ^/[A-Za-z0-9._/-]+$ ]] || die "ROUTING_LOCK doit être un chemin absolu sans caractère spécial"
KEEP=$((10#$KEEP)); HEALTH_TIMEOUT=$((10#$HEALTH_TIMEOUT)); MAX_BYTES=$((10#$MAX_BYTES))

set -f                                   # pas d'expansion de jokers dans les boucles ci-dessous
hosts=(); for h in $ALLOWED_HOSTS; do
  [[ "$h" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ ]] || die "hôte autorisé invalide"
  hosts+=("$h")
done
[ "${#hosts[@]}" -gt 0 ] || die "ROUTING_ALLOWED_HOSTS est vide"

URL_RE='^https://([A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?)/([A-Za-z0-9._-]+/)*([A-Za-z0-9][A-Za-z0-9._-]*\.osm\.pbf)$'
urls=(); names=()
for url in $URLS; do
  [[ "$url" =~ $URL_RE ]] || die "URL refusée (https, chemin simple, extension .osm.pbf exigés)"
  host="${BASH_REMATCH[1]}"; name="${BASH_REMATCH[4]}"
  ok=0; for h in "${hosts[@]}"; do [ "$host" = "$h" ] && ok=1; done
  [ "$ok" = 1 ] || die "hôte $host absent de ROUTING_ALLOWED_HOSTS"
  for n in "${names[@]}"; do [ "$n" != "$name" ] || die "deux URL donnent le même nom de fichier ($name)"; done
  urls+=("$url"); names+=("$name")
done
set +f
[ "${#urls[@]}" -gt 0 ] || die "ROUTING_EXTRACT_URLS est vide"

STAMP="$(date -u +%Y%m%d%H%M%S)"
DC=(docker compose --profile routing --profile routing-tools)

log() { echo "$(date -u +%FT%TZ) routing-update: $*"; }
fail() { log "ECHEC : $*" >&2; exit 1; }
tool() { "${DC[@]}" run --rm -T "$@"; }       # tâche ponctuelle : tool [-e VAR=val ...] service [args]
# Opérations sur le volume, sans réseau. Dernier argument = script bash CONSTANT ; les valeurs variables
# passent par -e (jamais collées dans le texte du script).
vol() { local script="${!#}"; "${DC[@]}" run --rm -T "${@:1:$#-1}" routing-volume "$script"; }

exec 9>"$LOCK"
flock -n 9 || { log "une mise à jour est déjà en cours, rien à faire"; exit 0; }

log "début ($STAMP)"
"${DC[@]}" build graphhopper >/dev/null || fail "construction de l'image du moteur"
tool routing-init || fail "initialisation du volume"

# 1. Téléchargement ; somme de contrôle vérifiée avant de remplacer quoi que ce soit
for i in "${!urls[@]}"; do
  url="${urls[$i]}"; name="${names[$i]}"
  log "téléchargement $name"
  tool -e URL="$url" -e NAME="$name" -e MAXB="$MAX_BYTES" routing-fetch '
    cd /data/in
    dl() { curl -fsSL --proto =https --proto-redir =https --retry 3 --connect-timeout 20 "$@"; }
    dl --max-filesize "$MAXB" -o "$NAME.part" "$URL"
    expected="$(dl --max-filesize 1024 "$URL.md5" | cut -d" " -f1)"
    actual="$(md5sum "$NAME.part" | cut -d" " -f1)"
    if ! [[ "$expected" =~ ^[0-9a-f]{32}$ ]] || [ "$expected" != "$actual" ]; then
      echo "somme MD5 invalide pour $NAME" >&2; rm -f "$NAME.part"; exit 1
    fi
    mv -f "$NAME.part" "$NAME"
  ' || fail "téléchargement ou somme de contrôle de $name"
done

# 2. Fusion dans un fichier horodaté (le précédent reste intact) ; un fichier partiel est supprimé
merged="hdf-be-$STAMP.osm.pbf"
log "fusion -> $merged"
inputs=(); for n in "${names[@]}"; do inputs+=("/data/in/$n"); done
tool routing-merge merge "${inputs[@]}" -o "/data/in/$merged" --overwrite \
  || { vol -e MERGED="$merged" 'rm -f -- "/data/in/$MERGED"' || true; fail "fusion osmium"; }

# 3. Import dans un dossier neuf
new="graph-$STAMP"
vol -e NEW="$new" 'test ! -e "/data/graphs/$NEW"' || fail "le dossier graphs/$new existe déjà (relance dans une seconde)"
log "import -> graphs/$new"
ROUTING_IMPORT_PBF="/data/in/$merged" ROUTING_IMPORT_GRAPH="/data/graphs/$new" tool routing-import \
  || { vol -e NEW="$new" -e MERGED="$merged" 'rm -rf -- "/data/graphs/$NEW" "/data/in/$MERGED"' || true; fail "import GraphHopper"; }
vol -e NEW="$new" 'test -s "/data/graphs/$NEW/properties"' || fail "graphe produit incomplet ($new)"

# 4. Bascule atomique (lien relatif remplacé par rename) ; mémorise l'ancien pour le retour arrière
previous="$(vol 'readlink /data/graphs/current 2>/dev/null || true' | tr -d '\r\n')"
[[ "$previous" =~ ^graph-[0-9]{14}$ ]] || previous=""
vol -e NEW="$new" '
  cd /data/graphs
  # Un moteur demarre avant le premier graphe (redemarrage en boucle) cree un dossier current VIDE : le retirer,
  # sinon mv -T refuse de le remplacer par un lien. rmdir echoue si le dossier est non vide (jamais detruit).
  if [ -d current ] && [ ! -L current ]; then rmdir current; fi
  ln -sfn "$NEW" current.tmp && mv -T current.tmp current
' || fail "bascule impossible (graphs/current est un dossier non vide ?), graphe $new conservé"
log "bascule : current -> $new (précédent : ${previous:-aucun})"

# 5. Le moteur relit le graphe : recréation, puis attente de santé
"${DC[@]}" up -d --no-deps --force-recreate graphhopper >/dev/null || true
cid="$("${DC[@]}" ps -q graphhopper)"
healthy=0
for _ in $(seq 1 $((HEALTH_TIMEOUT / 5))); do
  state="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$cid" 2>/dev/null || echo gone)"
  [ "$state" = healthy ] && { healthy=1; break; }
  sleep 5
done
if [ "$healthy" != 1 ]; then
  log "le moteur n'est pas sain après ${HEALTH_TIMEOUT} s : retour arrière"
  if [ -n "$previous" ]; then
    vol -e PREV="$previous" 'cd /data/graphs && test -d "$PREV" && ln -sfn "$PREV" current.tmp && mv -T current.tmp current' \
      || fail "retour arrière impossible : $previous introuvable, graphe $new conservé pour analyse"
    "${DC[@]}" up -d --no-deps --force-recreate graphhopper >/dev/null || true
    [ "$new" != "$previous" ] && { vol -e NEW="$new" -e MERGED="$merged" 'rm -rf -- "/data/graphs/$NEW" "/data/in/$MERGED"' || true; }
  fi
  fail "nouveau graphe refusé (ancien rétabli : ${previous:-aucun})"
fi

# 6. Nettoyage : garde KEEP graphes dont le courant (jamais supprimé), et le dernier extrait fusionné
vol -e KEEP="$KEEP" '
  cd /data/graphs
  cur="$(readlink current || true)"
  { ls -1dt graph-* 2>/dev/null | grep -vxF -- "$cur" | tail -n +"$KEEP" || true; } | xargs -r rm -rf --
  cd /data/in
  { ls -1t hdf-be-*.osm.pbf 2>/dev/null | tail -n +2 || true; } | xargs -r rm -f --
' || log "avertissement : nettoyage incomplet"
log "terminé : graphe $new en service"
