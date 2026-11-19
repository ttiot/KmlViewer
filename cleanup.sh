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

# Les volumes contiennent la base et les fichiers d'origine de tous les utilisateurs
echo "⚠️  Cette opération supprime les conteneurs, les images ET les volumes du projet :"
echo "   toutes les activités enregistrées et les dalles d'altitude seront perdues."
read -r -p "   Tape « supprimer » pour confirmer : " answer
if [ "$answer" != "supprimer" ]; then
    echo "Abandon : rien n'a été supprimé."
    exit 1
fi

# Uniquement les ressources de ce projet : rien d'autre sur la machine n'est touché
echo "🛑 Arrêt des conteneurs, suppression des volumes et des images..."
run_compose --profile dem down -v --rmi all

echo ""
echo "✅ Nettoyage terminé!"
echo ""
echo "Pour relancer l'application, utilisez: ./start.sh"