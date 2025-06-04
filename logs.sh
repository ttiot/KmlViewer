#!/bin/bash

# Script pour afficher les logs de l'application Visualiseur KML

echo "📋 Logs de l'application Visualiseur KML"
echo "========================================"
echo ""

# Fonction pour utiliser docker-compose ou docker compose
run_compose() {
    if command -v docker-compose &> /dev/null; then
        docker-compose "$@"
    else
        docker compose "$@"
    fi
}

# Afficher les logs en temps réel
run_compose logs -f kml-viewer