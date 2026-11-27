#!/usr/bin/env bash
# Banc navigateur local (OPTIONNEL, jamais en CI) : application réelle (python:3.15-slim, dépendances hachées) sur une base jetable,
# moteur de routage local 127.0.0.1:8989, Chromium réel (image Playwright épinglée par version ET digest), scénarios + captures.
#
#   web-app/tests_browser/run.sh            # lance tout ; captures dans ~/.cache/kmlviewer-browser/<horodatage>/
#   BENCH_OUT=/chemin BENCH_PORT=5277 BENCH_ENGINE_URL=http://127.0.0.1:8989 web-app/tests_browser/run.sh   # (variables facultatives)
#
# Prérequis : docker ; un moteur GraphHopper joignable sur BENCH_ENGINE_URL (le diagnostic et le constructeur en ont besoin).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WEB_APP="$(dirname "$HERE")"
PLAYWRIGHT_IMAGE="kmlviewer-bench-playwright:1.64.1"      # Dockerfile.playwright : image officielle épinglée (version + digest) + client 1.64.1 (requirements.txt, hachages)
PORT="${BENCH_PORT:-5277}"
ENGINE="${BENCH_ENGINE_URL:-http://127.0.0.1:8989}"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="${BENCH_OUT:-$HOME/.cache/kmlviewer-browser}/$STAMP"
NAME="kmlviewer-bench-$$"
REQ_HASH="$(sha256sum "$WEB_APP/requirements.txt" | cut -c1-12)"
APP_IMAGE="kmlviewer-bench-app:$REQ_HASH"

case "$OUT" in "$WEB_APP"/*|"$(dirname "$WEB_APP")"/*) echo "Les captures doivent être HORS dépôt : $OUT" >&2; exit 2;; esac
mkdir -p "$OUT"

# Les quatre ports doivent être libres (un serveur de développement oublié sur le même port ferait jouer les scénarios sur la mauvaise application)
for port in "${BENCH_PORT:-5277}" "${BENCH_OIDC_PORT:-5279}" "${BENCH_SETTINGS_PORT:-5281}" "${BENCH_REGISTRY_PORT:-5283}" \
        "${BENCH_DEM_APP_PORT:-5285}" "${BENCH_DEM_SVC_PORT:-5290}" "${BENCH_NOPW_PORT:-5287}" "${BENCH_COVERAGE_PORT:-5293}"; do
    if (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; then
        echo "Le port $port est déjà utilisé : libère-le ou choisis-en un autre (BENCH_PORT, BENCH_OIDC_PORT, BENCH_SETTINGS_PORT, BENCH_REGISTRY_PORT)" >&2
        exit 2
    fi
done

if ! curl -fsS -m 5 "$ENGINE/info" >/dev/null 2>&1; then
    echo "Moteur de routage injoignable sur $ENGINE : démarre-le (ou BENCH_ENGINE_URL=...)" >&2
    exit 2
fi

if ! docker image inspect "$APP_IMAGE" >/dev/null 2>&1; then
    echo "Construction de $APP_IMAGE (dépendances hachées)..."
    docker build -q -f "$HERE/Dockerfile.app" --build-context "req=$WEB_APP" -t "$APP_IMAGE" "$HERE" >/dev/null
fi

if ! docker image inspect "$PLAYWRIGHT_IMAGE" >/dev/null 2>&1; then
    echo "Construction de $PLAYWRIGHT_IMAGE..."
    docker build -q -f "$HERE/Dockerfile.playwright" -t "$PLAYWRIGHT_IMAGE" "$HERE" >/dev/null
fi

HOST_PORT_ENGINE="$(echo "$ENGINE" | sed -E 's#^https?://##; s#/.*$##')"
BENCH_SECRETS_KEY="${BENCH_SECRETS_KEY:-a21sdmlld2VyLWJlbmNoLWtleS0wMTIzNDU2Nzg5YWI=}"   # clé Fernet FIXE du banc (base jetable, jamais un secret)
OIDC_PORT="${BENCH_OIDC_PORT:-5279}"
SETTINGS_PORT="${BENCH_SETTINGS_PORT:-5281}"
REGISTRY_PORT="${BENCH_REGISTRY_PORT:-5283}"
DEM_APP_PORT="${BENCH_DEM_APP_PORT:-5285}"
NOPW_PORT="${BENCH_NOPW_PORT:-5287}"
DEM_SVC_PORT="${BENCH_DEM_SVC_PORT:-5290}"
NAME_OIDC="$NAME-oidc"
NAME_SET="$NAME-settings"
NAME_REG="$NAME-registry"
NAME_DEM="$NAME-dem"
NAME_NOPW="$NAME-nopw"
NAME_DEMSVC="$NAME-demsvc"
COVERAGE_PORT="${BENCH_COVERAGE_PORT:-5293}"
NAME_COV="$NAME-coverage"
cleanup() { docker rm -f "$NAME" "$NAME_OIDC" "$NAME_SET" "$NAME_REG" "$NAME_DEM" "$NAME_DEMSVC" "$NAME_NOPW" "$NAME_COV" >/dev/null 2>&1 || true; rm -rf "${SESS:-/nonexistent}" 2>/dev/null || true; }
trap cleanup EXIT

# Instance 1 : mode local (administrateur implicite), moteur réel, e-mail volontairement NON configuré au départ (bandeau « SMTP absent »)
docker run -d --name "$NAME" --network host --tmpfs /data --tmpfs /tmp \
    -v "$WEB_APP:/app:ro" -w /app \
    -e PYTHONPATH=/app -e PYTHONDONTWRITEBYTECODE=1 -e FLASK_ENV=production -e AUTH_MODE=local -e DATA_DIR=/data -e INBOX_ENABLED=0 \
    -e "SECRETS_KEY=$BENCH_SECRETS_KEY" -e "OUTBOUND_ALLOWED_HOSTS=routing=$HOST_PORT_ENGINE,smtp=127.0.0.1:2525,smtp=127.0.0.1:2526" -e "BENCH_ENGINE_URL=$ENGINE" \
    "$APP_IMAGE" sh -c "python tests_browser/seed.py && exec gunicorn --bind 127.0.0.1:$PORT --workers 1 --threads 8 --no-control-socket kml_viewer:app" >/dev/null

# Instance 2 : mode oidc de test (IdP factice jamais joint) avec comptes à mot de passe : page de connexion unique (SSO + formulaire)
# et comptes SSO de test (seed_sso.py : cookies de session signés par l'application, SECRET_KEY fixe du banc) : profil, invitations
BENCH_SECRET_KEY="${BENCH_SECRET_KEY:-bench-secret-key-0123456789abcdef-0123456789abcdef}"     # SECRET_KEY FIXE du banc (base jetable, jamais un secret)
SESS="$(mktemp -d)"
docker run -d --name "$NAME_OIDC" --network host --tmpfs /data --tmpfs /tmp \
    -v "$WEB_APP:/app:ro" -v "$SESS:/sess" -w /app \
    -e PYTHONPATH=/app -e PYTHONDONTWRITEBYTECODE=1 -e FLASK_ENV=production -e AUTH_MODE=oidc -e DATA_DIR=/data -e INBOX_ENABLED=0 \
    -e OIDC_ISSUER=https://idp.invalid -e OIDC_CLIENT_ID=bench -e OIDC_CLIENT_SECRET=bench -e "SECRET_KEY=$BENCH_SECRET_KEY" \
    -e "SECRETS_KEY=$BENCH_SECRETS_KEY" -e "PUBLIC_URL=http://127.0.0.1:$OIDC_PORT" -e "TRUSTED_HOSTS=127.0.0.1" -e SESSION_COOKIE_SECURE=0 \
    -e SSO_OUT=/sess -e SSO_NAME=oidc \
    "$APP_IMAGE" sh -c "python tests_browser/seed_sso.py && exec gunicorn --bind 127.0.0.1:$OIDC_PORT --workers 1 --threads 8 --no-control-socket kml_viewer:app" >/dev/null

# Instance 2 bis : mode oidc SANS SECRETS_KEY : les invitations sont indisponibles (EF-0.4) ; même comptes SSO de test
docker run -d --name "$NAME_NOPW" --network host --tmpfs /data --tmpfs /tmp \
    -v "$WEB_APP:/app:ro" -v "$SESS:/sess" -w /app \
    -e PYTHONPATH=/app -e PYTHONDONTWRITEBYTECODE=1 -e FLASK_ENV=production -e AUTH_MODE=oidc -e DATA_DIR=/data -e INBOX_ENABLED=0 \
    -e OIDC_ISSUER=https://idp.invalid -e OIDC_CLIENT_ID=bench -e OIDC_CLIENT_SECRET=bench -e "SECRET_KEY=$BENCH_SECRET_KEY" \
    -e "PUBLIC_URL=http://127.0.0.1:$NOPW_PORT" -e "TRUSTED_HOSTS=127.0.0.1" -e SESSION_COOKIE_SECURE=0 \
    -e SSO_OUT=/sess -e SSO_NAME=nopw \
    "$APP_IMAGE" sh -c "python tests_browser/seed_sso.py && exec gunicorn --bind 127.0.0.1:$NOPW_PORT --workers 1 --threads 8 --no-control-socket kml_viewer:app" >/dev/null

# Instance 3 : page Réglages (CA-72 à CA-77) : registre v1, en base SEULEMENT map.zoom, dans l'env SEULEMENT DEM_TIMEOUT=30 (ni INBOX_ENABLED, ni
# aucune autre variable de réglage), tuiles autorisées : a.tiles.exemple.fr seulement ; SMTP : deux destinations permises (confirmation du secret lié)
docker run -d --name "$NAME_SET" --network host --tmpfs /data --tmpfs /tmp \
    -v "$WEB_APP:/app:ro" -w /app \
    -e PYTHONPATH=/app -e PYTHONDONTWRITEBYTECODE=1 -e FLASK_ENV=production -e AUTH_MODE=local -e DATA_DIR=/data -e DEM_TIMEOUT=30 \
    -e "SECRETS_KEY=$BENCH_SECRETS_KEY" -e "OUTBOUND_ALLOWED_HOSTS=tiles=a.tiles.exemple.fr,smtp=127.0.0.1:2525,smtp=127.0.0.1:2526" -e BENCH_PROFILE=settings \
    "$APP_IMAGE" sh -c "python tests_browser/seed.py && exec gunicorn --bind 127.0.0.1:$SETTINGS_PORT --workers 1 --threads 8 --no-control-socket kml_viewer:app" >/dev/null

# Instance 4 : registre de TEST (réglages list, requires_restart, secret : CA-78, CA-79) et variable d'env invalide DEM_TIMEOUT=abc (CA-72)
docker run -d --name "$NAME_REG" --network host --tmpfs /data --tmpfs /tmp \
    -v "$WEB_APP:/app:ro" -w /app \
    -e PYTHONPATH=/app:/app/tests_browser -e PYTHONDONTWRITEBYTECODE=1 -e FLASK_ENV=production -e AUTH_MODE=local -e DATA_DIR=/data -e DEM_TIMEOUT=abc \
    -e "SECRETS_KEY=$BENCH_SECRETS_KEY" -e "OUTBOUND_ALLOWED_HOSTS=tiles=a.tiles.exemple.fr" -e BENCH_PROFILE=registry \
    "$APP_IMAGE" sh -c "python tests_browser/seed.py && exec gunicorn --bind 127.0.0.1:$REGISTRY_PORT --workers 1 --threads 8 --no-control-socket bench_app:app" >/dev/null

# Service d'altitude FACTICE (porte pilotée par le scénario) et instance 5 : DEM_URL posé, tâche de fond toutes les 30 s, moteur de routage réel
docker run -d --name "$NAME_DEMSVC" --network host -v "$HERE:/work:ro" -e PYTHONDONTWRITEBYTECODE=1 \
    "$APP_IMAGE" python /work/fake_dem.py "$DEM_SVC_PORT" >/dev/null
docker run -d --name "$NAME_DEM" --network host --tmpfs /data --tmpfs /tmp \
    -v "$WEB_APP:/app:ro" -w /app \
    -e PYTHONPATH=/app -e PYTHONDONTWRITEBYTECODE=1 -e FLASK_ENV=production -e AUTH_MODE=local -e DATA_DIR=/data -e INBOX_ENABLED=0 \
    -e "SECRETS_KEY=$BENCH_SECRETS_KEY" -e "OUTBOUND_ALLOWED_HOSTS=routing=$HOST_PORT_ENGINE,dem=127.0.0.1:$DEM_SVC_PORT" \
    -e "BENCH_ENGINE_URL=$ENGINE" -e "DEM_URL=http://127.0.0.1:$DEM_SVC_PORT/v1/test" -e DEM_BACKFILL_INTERVAL=30 \
    "$APP_IMAGE" sh -c "python tests_browser/seed.py && exec gunicorn --bind 127.0.0.1:$DEM_APP_PORT --workers 1 --threads 8 --no-control-socket kml_viewer:app" >/dev/null

# Instance 6 : mini-carte « Couverture en service » (CA-86, lot Z3) : ROUTING_ZONE_BUILD=1, tuiles autorisées a, b et c.tiles.exemple.fr, état amorcé
# par seed.py (profil coverage : trois fonds de carte, zone active ≠ zone demandée)
docker run -d --name "$NAME_COV" --network host --tmpfs /data --tmpfs /tmp \
    -v "$WEB_APP:/app:ro" -w /app \
    -e PYTHONPATH=/app -e PYTHONDONTWRITEBYTECODE=1 -e FLASK_ENV=production -e AUTH_MODE=local -e DATA_DIR=/data -e INBOX_ENABLED=0 -e ROUTING_ZONE_BUILD=1 \
    -e "SECRETS_KEY=$BENCH_SECRETS_KEY" -e "OUTBOUND_ALLOWED_HOSTS=tiles=a.tiles.exemple.fr,tiles=b.tiles.exemple.fr,tiles=c.tiles.exemple.fr" -e BENCH_PROFILE=coverage \
    "$APP_IMAGE" sh -c "python tests_browser/seed.py && exec gunicorn --bind 127.0.0.1:$COVERAGE_PORT --workers 1 --threads 8 --no-control-socket kml_viewer:app" >/dev/null

URLS="http://127.0.0.1:$PORT http://127.0.0.1:$OIDC_PORT http://127.0.0.1:$SETTINGS_PORT http://127.0.0.1:$REGISTRY_PORT http://127.0.0.1:$DEM_APP_PORT http://127.0.0.1:$NOPW_PORT http://127.0.0.1:$COVERAGE_PORT"
for _ in $(seq 1 90); do
    ready=1
    for url in $URLS; do curl -fsS -m 2 "$url/api/health" >/dev/null 2>&1 || ready=0; done
    [ "$ready" = 1 ] && break
    sleep 1
done
for url in $URLS; do
    curl -fsS -m 2 "$url/api/health" >/dev/null 2>&1 || { echo "L'application ne démarre pas ($url) :" >&2
        for name in "$NAME" "$NAME_OIDC" "$NAME_SET" "$NAME_REG" "$NAME_DEM" "$NAME_NOPW" "$NAME_COV"; do docker logs "$name" 2>&1 | tail -10 >&2; done; exit 3; }
done
sleep 6   # laisse passer le premier sondage de santé du moteur (diagnostic « disponible »)

set +e
docker run --rm --network host --ipc=host \
    -v "$HERE:/work:ro" -v "$OUT:/out" -v "$SESS:/sess:ro" -e "BENCH_URL=http://127.0.0.1:$PORT" -e "BENCH_OIDC_URL=http://127.0.0.1:$OIDC_PORT" -e "BENCH_SETTINGS_URL=http://127.0.0.1:$SETTINGS_PORT" -e "BENCH_REGISTRY_URL=http://127.0.0.1:$REGISTRY_PORT" -e "BENCH_DEM_URL=http://127.0.0.1:$DEM_APP_PORT" -e "BENCH_DEM_SVC=http://127.0.0.1:$DEM_SVC_PORT" -e "BENCH_NOPW_URL=http://127.0.0.1:$NOPW_PORT" -e "BENCH_COVERAGE_URL=http://127.0.0.1:$COVERAGE_PORT" -e BENCH_OUT=/out -e "BENCH_ONLY=${BENCH_ONLY:-}" -e PYTHONDONTWRITEBYTECODE=1 -e "BENCH_UID=$(id -u)" -e "BENCH_GID=$(id -g)" \
    "$PLAYWRIGHT_IMAGE" python /work/scenarios.py
STATUS=$?
set -e
docker logs "$NAME" > "$OUT/application.log" 2>&1 || true
docker logs "$NAME_OIDC" > "$OUT/application-oidc.log" 2>&1 || true
docker logs "$NAME_SET" > "$OUT/application-settings.log" 2>&1 || true
docker logs "$NAME_REG" > "$OUT/application-registry.log" 2>&1 || true
docker logs "$NAME_DEM" > "$OUT/application-dem.log" 2>&1 || true
docker logs "$NAME_COV" > "$OUT/application-coverage.log" 2>&1 || true
echo "Captures et résultats : $OUT"
exit $STATUS
