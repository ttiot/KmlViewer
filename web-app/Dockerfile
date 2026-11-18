# Stage 1: Build stage
FROM python:3.15-slim AS builder

RUN apt-get update && apt-get install -y gcc && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
# --require-hashes : chaque paquet, transitif compris, doit correspondre au verrou
RUN pip install --no-cache-dir --user --require-hashes -r requirements.txt

# Stage 2: Production stage
FROM python:3.15-slim

# Utilisateur non-root à UID fixe (pratique pour les droits des volumes montés).
# pip est retiré : inutile à l'exécution, et ses bibliothèques embarquées traînent des CVE.
# apt-get upgrade : applique les correctifs de sécurité Debian parus depuis la dernière
# publication de l'image de base (la CI reconstruit chaque semaine pour les récupérer).
RUN apt-get update && apt-get upgrade -y --no-install-recommends && \
    rm -rf /var/lib/apt/lists/* && \
    groupadd -r -g 10001 appuser && useradd -r -u 10001 -g appuser appuser && \
    python -m pip uninstall -y --root-user-action=ignore pip && \
    rm -rf /usr/local/lib/python3.15/ensurepip

COPY --from=builder --chown=appuser:appuser /root/.local /home/appuser/.local

WORKDIR /app
COPY . .

# Le code reste en lecture seule pour l'appli ; seul /data est inscriptible
# /dem : cible du téléchargeur de dalles (service dem-tiles), le volume hérite de ces droits
RUN mkdir -p /data /dem /app/uploads && chown -R appuser:appuser /data /dem /app/uploads && chmod 700 /data

USER appuser

# Version publiée par /api/health ; la CI y met le tag de release
ARG APP_VERSION=dev

ENV PATH=/home/appuser/.local/bin:$PATH \
    PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    FLASK_APP=kml_viewer.py \
    FLASK_ENV=production \
    DATA_DIR=/data \
    APP_VERSION=${APP_VERSION}

VOLUME ["/data"]
EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:5000/api/health', timeout=5)" || exit 1

# Un seul worker : l'éditeur et le cache gardent de l'état en mémoire, et le dossier
# surveillé tourne dans ce processus. Les threads absorbent la concurrence.
# Pas de socket de contrôle : inutile ici, et le système de fichiers est en lecture seule.
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "1", "--threads", "8", \
     "--timeout", "120", "--access-logfile", "-", "--no-control-socket", "kml_viewer:app"]
