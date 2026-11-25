# Image de l'application pour le banc navigateur : MÊMES dépendances (hachées) que la CI, python:3.15-slim épinglée par digest.
# Contexte de build : web-app/ (passé en --build-context req=...), seul requirements.txt est copié ; le code est monté en lecture seule.
FROM python@sha256:63bb298d2b7b88f176f51b5189f54ce6535c997682ea70885d2132f5d473a8a5
ENV PIP_ROOT_USER_ACTION=ignore PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY --from=req requirements.txt /tmp/requirements.txt
RUN pip install --require-hashes -r /tmp/requirements.txt
