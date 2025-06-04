#!/bin/bash

# Script pour redémarrer l'application Visualiseur KML

set -e

echo "🔄 Redémarrage de l'application Visualiseur KML..."
echo ""

# Fonction pour utiliser docker-compose ou docker compose
run_compose() {
    if command -v docker-compose &> /dev/null; then
        docker-compose "$@"
    else
        docker compose "$@"
    fi
}

# Redémarrer les conteneurs
if run_compose restart; then
    echo "✅ Application redémarrée avec succès!"
    echo ""
    echo "📍 Accès à l'application:"
    echo "   🌐 URL: http://localhost:8080"
else
    echo "❌ Erreur lors du redémarrage de l'application."
    exit 1
fi