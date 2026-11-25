#!/usr/bin/env bash
# Banc navigateur local (OPTIONNEL, jamais en CI) : application réelle (python:3.15-slim, dépendances hachées) sur une base jetable,
# moteur de routage local 127.0.0.1:8989, Chromium réel (image Playwright épinglée par version ET digest), scénarios + captures.
#
#   web-app/tests_browser/run.sh            # lance tout ; captures dans ~/.cache/kmlviewer-browser/<horodatage>/
#   BENCH_OUT=/chemin BENCH_PORT=5077 BENCH_ENGINE_URL=http://127.0.0.1:8989 web-app/tests_browser/run.sh
#
# Prérequis : docker ; un moteur GraphHopper joignable sur BENCH_ENGINE_URL (le diagnostic et le constructeur en ont besoin).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WEB_APP="$(dirname "$HERE")"
PLAYWRIGHT_IMAGE="kmlviewer-bench-playwright:1.65.2"      # Dockerfile.playwright : image officielle épinglée (version + digest) + client 1.65.2
PORT="${BENCH_PORT:-5077}"
ENGINE="${BENCH_ENGINE_URL:-http://127.0.0.1:8989}"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="${BENCH_OUT:-$HOME/.cache/kmlviewer-browser}/$STAMP"
NAME="kmlviewer-bench-$$"
REQ_HASH="$(sha256sum "$WEB_APP/requirements.txt" | cut -c1-12)"
APP_IMAGE="kmlviewer-bench-app:$REQ_HASH"

case "$OUT" in "$WEB_APP"/*|"$(dirname "$WEB_APP")"/*) echo "Les captures doivent être HORS dépôt : $OUT" >&2; exit 2;; esac
mkdir -p "$OUT"

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
OIDC_PORT="${BENCH_OIDC_PORT:-5079}"
NAME_OIDC="$NAME-oidc"
cleanup() { docker rm -f "$NAME" "$NAME_OIDC" >/dev/null 2>&1 || true; }
trap cleanup EXIT

# Instance 1 : mode local (administrateur implicite), moteur réel, e-mail volontairement NON configuré au départ (bandeau « SMTP absent »)
docker run -d --name "$NAME" --network host --tmpfs /data --tmpfs /tmp \
    -v "$WEB_APP:/app:ro" -w /app \
    -e PYTHONPATH=/app -e PYTHONDONTWRITEBYTECODE=1 -e FLASK_ENV=production -e AUTH_MODE=local -e DATA_DIR=/data -e INBOX_ENABLED=0 \
    -e "SECRETS_KEY=$BENCH_SECRETS_KEY" -e "OUTBOUND_ALLOWED_HOSTS=routing=$HOST_PORT_ENGINE,smtp=127.0.0.1:2525,smtp=127.0.0.1:2526" -e "BENCH_ENGINE_URL=$ENGINE" \
    "$APP_IMAGE" sh -c "python tests_browser/seed.py && exec gunicorn --bind 127.0.0.1:$PORT --workers 1 --threads 8 --no-control-socket kml_viewer:app" >/dev/null

# Instance 2 : mode oidc de test (IdP factice jamais joint) avec comptes à mot de passe : page de connexion unique (SSO + formulaire)
docker run -d --name "$NAME_OIDC" --network host --tmpfs /data --tmpfs /tmp \
    -v "$WEB_APP:/app:ro" -w /app \
    -e PYTHONPATH=/app -e PYTHONDONTWRITEBYTECODE=1 -e FLASK_ENV=production -e AUTH_MODE=oidc -e DATA_DIR=/data -e INBOX_ENABLED=0 \
    -e OIDC_ISSUER=https://idp.invalid -e OIDC_CLIENT_ID=bench -e OIDC_CLIENT_SECRET=bench -e "SECRET_KEY=$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')" \
    -e "SECRETS_KEY=$BENCH_SECRETS_KEY" -e "PUBLIC_URL=http://127.0.0.1:$OIDC_PORT" -e "TRUSTED_HOSTS=127.0.0.1" -e SESSION_COOKIE_SECURE=0 \
    "$APP_IMAGE" gunicorn --bind "127.0.0.1:$OIDC_PORT" --workers 1 --threads 8 --no-control-socket kml_viewer:app >/dev/null

for _ in $(seq 1 60); do
    curl -fsS -m 2 "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1 && curl -fsS -m 2 "http://127.0.0.1:$OIDC_PORT/api/health" >/dev/null 2>&1 && break
    sleep 1
done
for url in "http://127.0.0.1:$PORT" "http://127.0.0.1:$OIDC_PORT"; do
    curl -fsS -m 2 "$url/api/health" >/dev/null 2>&1 || { echo "L'application ne démarre pas ($url) :" >&2; docker logs "$NAME" 2>&1 | tail -10 >&2; docker logs "$NAME_OIDC" 2>&1 | tail -10 >&2; exit 3; }
done
sleep 6   # laisse passer le premier sondage de santé du moteur (diagnostic « disponible »)

set +e
docker run --rm --network host --ipc=host \
    -v "$HERE:/work:ro" -v "$OUT:/out" -e "BENCH_URL=http://127.0.0.1:$PORT" -e "BENCH_OIDC_URL=http://127.0.0.1:$OIDC_PORT" -e BENCH_OUT=/out -e PYTHONDONTWRITEBYTECODE=1 -e "BENCH_UID=$(id -u)" -e "BENCH_GID=$(id -g)" \
    "$PLAYWRIGHT_IMAGE" python /work/scenarios.py
STATUS=$?
set -e
docker logs "$NAME" > "$OUT/application.log" 2>&1 || true
docker logs "$NAME_OIDC" > "$OUT/application-oidc.log" 2>&1 || true
echo "Captures et résultats : $OUT"
exit $STATUS
