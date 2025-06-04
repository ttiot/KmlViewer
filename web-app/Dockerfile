# Stage 1: Build stage
FROM python:3.11-slim AS builder

# Installer les outils de build nécessaires
RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Définir le répertoire de travail
WORKDIR /app

# Copier et installer les dépendances
COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

# Stage 2: Production stage
FROM python:3.11-slim

# Créer un utilisateur non-root pour la sécurité
RUN groupadd -r appuser && useradd -r -g appuser appuser

# Copier les dépendances installées depuis le stage de build
COPY --from=builder /root/.local /home/appuser/.local

# Définir le répertoire de travail
WORKDIR /app

# Copier le code de l'application
COPY . .

# Créer les dossiers nécessaires et ajuster les permissions
RUN mkdir -p uploads && \
    chown -R appuser:appuser /app

# Passer à l'utilisateur non-root
USER appuser

# Ajouter le répertoire local des packages Python au PATH
ENV PATH=/home/appuser/.local/bin:$PATH

# Définir les variables d'environnement
ENV FLASK_APP=kml_viewer.py
ENV FLASK_ENV=production
ENV PYTHONPATH=/app

# Exposer le port sur lequel l'application s'exécute
EXPOSE 5000

# Vérification de santé
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import requests; requests.get('http://localhost:5000/api/health')" || exit 1

# Commande pour démarrer l'application
CMD ["python", "kml_viewer.py"]