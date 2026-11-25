#!/bin/bash

# Script pour redémarrer l'application Visualiseur KML

set -e

echo "🔄 Redémarrage (recréation des conteneurs, .env relu) de l'application Visualiseur KML..."
echo ""

# Fonction pour utiliser docker-compose ou docker compose
run_compose() {
    if command -v docker-compose &> /dev/null; then
        docker-compose "$@"
    else
        docker compose "$@"
    fi
}

# Recréer les conteneurs : `restart` ne relit PAS le fichier .env (variables figées à la création), `up -d --force-recreate` oui
if run_compose up -d --force-recreate; then
    echo "✅ Application redémarrée avec succès!"
    echo ""
    echo "📍 Accès à l'application:"
    echo "   🌐 URL: http://localhost:8080"
else
    echo "❌ Erreur lors du redémarrage de l'application."
    exit 1
fi