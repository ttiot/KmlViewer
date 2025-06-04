#!/bin/bash

# Script de démarrage pour l'application Visualiseur KML
# Ce script facilite le lancement de l'application avec Docker Compose

set -e

echo "🗺️  Visualiseur KML - GPS Clean"
echo "================================"
echo ""

# Vérifier si Docker est installé
if ! command -v docker &> /dev/null; then
    echo "❌ Docker n'est pas installé. Veuillez installer Docker pour continuer."
    echo "   Visitez: https://docs.docker.com/get-docker/"
    exit 1
fi

# Vérifier si Docker Compose est installé
if ! command -v docker-compose &> /dev/null && ! docker compose version &> /dev/null; then
    echo "❌ Docker Compose n'est pas installé. Veuillez installer Docker Compose pour continuer."
    echo "   Visitez: https://docs.docker.com/compose/install/"
    exit 1
fi

# Fonction pour utiliser docker-compose ou docker compose
run_compose() {
    if command -v docker-compose &> /dev/null; then
        docker-compose "$@"
    else
        docker compose "$@"
    fi
}

# Vérifier si le fichier docker-compose.yml existe
if [ ! -f "docker-compose.yml" ]; then
    echo "❌ Fichier docker-compose.yml non trouvé."
    echo "   Assurez-vous d'être dans le répertoire racine du projet."
    exit 1
fi

echo "🔧 Construction et démarrage de l'application..."
echo ""

# Construire et démarrer les conteneurs
if run_compose up -d --build; then
    echo ""
    echo "✅ Application démarrée avec succès!"
    echo ""
    echo "📍 Accès à l'application:"
    echo "   🌐 URL: http://localhost:8080"
    echo ""
    echo "📋 Commandes utiles:"
    echo "   📊 Voir les logs:        ./logs.sh"
    echo "   🛑 Arrêter l'app:        ./stop.sh"
    echo "   🔄 Redémarrer l'app:     ./restart.sh"
    echo "   🗑️  Supprimer l'app:      ./cleanup.sh"
    echo ""
    echo "📁 Fichiers KML d'exemple disponibles dans l'interface web"
    echo ""
    
    # Attendre que l'application soit prête
    echo "⏳ Vérification de la disponibilité de l'application..."
    for i in {1..30}; do
        if curl -s http://localhost:8080 > /dev/null 2>&1; then
            echo "✅ Application prête!"
            break
        fi
        if [ "$i" -eq 30 ]; then
            echo "⚠️  L'application met du temps à démarrer. Vérifiez les logs avec: ./logs.sh"
        fi
        sleep 2
    done
    
else
    echo ""
    echo "❌ Erreur lors du démarrage de l'application."
    echo "   Vérifiez les logs avec: docker-compose logs"
    exit 1
fi