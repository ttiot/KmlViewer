#!/bin/bash

# Script pour nettoyer complètement l'application Visualiseur KML

set -e

echo "🗑️  Nettoyage de l'application Visualiseur KML..."
echo ""

# Fonction pour utiliser docker-compose ou docker compose
run_compose() {
    if command -v docker-compose &> /dev/null; then
        docker-compose "$@"
    else
        docker compose "$@"
    fi
}

# Arrêter et supprimer les conteneurs, réseaux, volumes et images
echo "🛑 Arrêt des conteneurs..."
run_compose down

echo "🧹 Suppression des volumes..."
run_compose down -v

echo "🗑️  Suppression des images..."
run_compose down --rmi all

echo "🧽 Nettoyage des ressources Docker inutilisées..."
docker system prune -f

echo ""
echo "✅ Nettoyage terminé!"
echo ""
echo "Pour relancer l'application, utilisez: ./start.sh"