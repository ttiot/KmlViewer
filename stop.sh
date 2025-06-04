#!/bin/bash

# Script pour arrêter l'application Visualiseur KML

set -e

echo "🛑 Arrêt de l'application Visualiseur KML..."
echo ""

# Fonction pour utiliser docker-compose ou docker compose
run_compose() {
    if command -v docker-compose &> /dev/null; then
        docker-compose "$@"
    else
        docker compose "$@"
    fi
}

# Arrêter les conteneurs
if run_compose down; then
    echo "✅ Application arrêtée avec succès!"
else
    echo "❌ Erreur lors de l'arrêt de l'application."
    exit 1
fi