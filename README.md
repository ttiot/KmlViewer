[![Build and Push Docker Image](https://github.com/ttiot/KmlViewer/actions/workflows/docker.yml/badge.svg)](https://github.com/ttiot/KmlViewer/actions/workflows/docker.yml)

# 🗺️ Visualiseur KML - Application Web Complète

Application web interactive pour visualiser des fichiers KML sur une carte avec navigation entre les points et différents fonds de carte.

## 🚀 Démarrage rapide

### Avec Docker (Recommandé)

1. **Cloner et lancer l'application** :

   ```bash
   git clone https://github.com/ttiot/KmlViewer
   cd kmlViewer
   ./start.sh
   ```

2. **Accéder à l'application** :
   Ouvrez votre navigateur à l'adresse : **<http://localhost:8080>**

3. **Arrêter l'application** :

   ```bash
   ./stop.sh
   ```

### Tests

Installez les dépendances et lancez `pytest` :

```bash
cd web-app
pip install -r requirements.txt
pytest
```

## ✨ Fonctionnalités principales

### 📁 Gestion des fichiers KML

- **Upload par glisser-déposer** ou sélection de fichiers
- **Fichiers d'exemple** intégrés pour test rapide
- **Support complet** des éléments KML (traces GPS, points, annotations)
- **Parsing intelligent** des métadonnées (nom, description, altitude)
- **Modes d'affichage** : Double unité (km/h + kts, ft + m) ou unité simple

### 🗺️ Visualisation cartographique

- **5 fonds de carte** différents :
  - OpenStreetMap (par défaut)
  - Satellite (Esri)
  - Terrain topographique (OpenTopoMap)
  - CartoDB Positron (minimaliste)
  - Stamen Toner (noir et blanc)
- **Affichage optimisé** des traces GPS et points d'intérêt
- **Marqueurs colorés** selon le type (points principaux vs annotations)
- **Popups informatifs** avec détails complets

### 🧭 Navigation entre les points

- **Navigation au clavier** : Flèches ← → pour passer d'un point à l'autre
- **Contrôles visuels** : Boutons Précédent/Suivant
- **Centrage automatique** sur le point sélectionné
- **Informations contextuelles** : Position actuelle (Point X/Y)
- **Clic sur marqueur** pour sélection directe
- **Liste interactive** des points avec navigation

### 📊 Analyse de trajectoire avancée

- **Statistiques complètes** : Distance, durée, vitesses, dénivelés
- **Graphiques interactifs** : Profils d'élévation et de vitesse
- **Détection d'arrêts** automatique avec durée et position
- **Segmentation intelligente** de la trajectoire par type de mouvement
- **Zones de vitesse** colorées avec pourcentages de répartition
- **Analyse du terrain** : Montées, descentes, sections plates
- **Points d'intérêt** détectés automatiquement

### ✏️ Édition de trajectoires

- **Ajout de points** : Clic sur la carte pour créer de nouveaux points
- **Modification de points** : Édition des noms, descriptions et métadonnées
- **Suppression de points** : Retrait de points indésirables
- **Simplification de traces** : Réduction automatique du nombre de points
- **Interface intuitive** avec modes d'édition dédiés

### 📤 Export multi-formats

- **Format GPX** : Compatible avec les appareils GPS et applications de randonnée
- **Format CSV** : Pour analyse dans Excel ou autres outils
- **Format GeoJSON** : Standard web pour applications cartographiques
- **Format KML** : Compatible Google Earth et autres visualiseurs
- **Options personnalisables** : Inclusion/exclusion des traces, nom de fichier

### 📱 Interface utilisateur

- **Design responsive** compatible mobile/desktop
- **Interface moderne** avec Bootstrap 5
- **Icônes Font Awesome** pour une meilleure UX
- **Messages d'état** informatifs et colorés
- **Panneau d'analyse** avec onglets organisés
- **Sidebar rétractable** pour optimiser l'espace

## 🛠️ Scripts utilitaires

| Script | Description |
|--------|-------------|
| `./start.sh` | Démarre l'application avec Docker |
| `./stop.sh` | Arrête l'application |
| `./restart.sh` | Redémarre l'application |
| `./logs.sh` | Affiche les logs en temps réel |
| `./cleanup.sh` | Nettoyage complet (conteneurs, images, volumes) |

## 🏗️ Architecture technique

### Backend (Python Flask)

- **Framework** : Flask 2.3.3 avec architecture modulaire par blueprints
- **Parsing KML** : xml.etree.ElementTree avec validation avancée
- **Analyse GPS** : Algorithmes de calcul de distance (Haversine), vitesse, dénivelé
- **Services métier** :
  - `TrajectoryAnalyzer` : Analyses avancées et détection d'arrêts
  - `KMLEditor` : Édition et simplification de traces (Douglas-Peucker)
  - Export multi-formats (GPX, CSV, GeoJSON, KML)
- **API REST** : Endpoints organisés par fonctionnalité
  - `/api/upload` et `/api/load-sample` : Gestion des fichiers
  - `/api/analysis/*` : Analyses de trajectoires
  - `/api/editor/*` : Édition de points et traces
  - `/api/export/*` : Export multi-formats
- **Sécurité** : Validation des fichiers, noms sécurisés, gestion d'erreurs

### Frontend (JavaScript/HTML/CSS)

- **Cartes** : Leaflet.js avec gestion avancée des couches
- **Graphiques** : Chart.js pour profils d'élévation et vitesse
- **UI** : Bootstrap 5 + Font Awesome avec interface à onglets
- **JavaScript ES6** :
  - Navigation entre points avec clavier/souris
  - Modes d'édition interactifs
  - Gestion des analyses avancées
  - Export de données avec téléchargement automatique
- **Responsive design** : Sidebar rétractable, compatible tous écrans

### Déploiement (Docker)

- **Conteneurisation** : Docker + Docker Compose
- **Port** : 8080 (configurable)
- **Volumes** : Montage des fichiers d'exemple et données temporaires
- **Health checks** : Surveillance automatique de l'application
- **Optimisations** : Image légère, cache des dépendances

## 📋 Utilisation détaillée

### 1. Charger un fichier KML

**Option A : Upload de fichier**

- Glissez-déposez votre fichier .kml dans la zone prévue
- Ou cliquez pour sélectionner un fichier
- Le fichier est automatiquement traité et affiché

**Option B : Fichiers d'exemple**

- Cliquez sur un des boutons des fichiers d'exemple
- Parfait pour tester sans avoir ses propres fichiers

### 2. Changer le fond de carte

- Utilisez les boutons radio dans le panneau de gauche
- 5 options disponibles selon vos préférences
- Changement instantané sans rechargement

### 3. Naviguer entre les points

**Navigation au clavier :**

- **Flèche gauche (←)** : Point précédent
- **Flèche droite (→)** : Point suivant

**Navigation à la souris :**

- **Cliquez sur un marqueur** pour le sélectionner
- **Boutons Précédent/Suivant** dans le panneau de navigation
- **Bouton "Centrer"** dans les popups
- **Liste des points** : Cliquez sur un point dans la liste pour y naviguer

### 4. Analyser une trajectoire

**Onglet "Stats" :**

- Statistiques générales : distance, durée, vitesses, dénivelés
- Mise à jour automatique lors du chargement d'un fichier

**Onglet "Élévation" :**

- Graphique du profil d'élévation
- Statistiques détaillées (altitude min/max)

**Onglet "Vitesse" :**

- Graphique du profil de vitesse
- Statistiques détaillées (vitesse min/moy/max)

**Onglet "Avancé" :**

- **Cliquez sur "Lancer l'analyse avancée"** pour obtenir :
  - Détection automatique des arrêts
  - Segmentation de la trajectoire
  - Zones de vitesse colorées
  - Analyse du terrain (montées/descentes/plat)
- **Cliquez sur "Effacer"** pour nettoyer les résultats

### 5. Éditer une trajectoire

**Onglet "Édition" :** 🚧 Work in progress 🚧

- **Ajouter un point** : Cliquez sur le bouton puis sur la carte
- **Modifier un point** : Activez le mode puis cliquez sur un point existant
- **Supprimer un point** : Activez le mode puis cliquez sur le point à supprimer
- **Simplifier la trace** : Réduit automatiquement le nombre de points

**Formulaire d'édition :**

- Modifiez le nom, la description
- Marquez comme point d'annotation si nécessaire
- Sauvegardez ou annulez les modifications

### 6. Exporter les données

**Onglet "Export" :**

- **GPX** : Format standard pour GPS et applications de randonnée
- **CSV** : Tableau pour analyse dans Excel
- **GeoJSON** : Format web pour développeurs
- **KML** : Compatible Google Earth

**Options d'export :**

- Cochez "Inclure les points de trace" selon vos besoins
- Personnalisez le nom du fichier
- Le téléchargement démarre automatiquement

### 7. Consulter les informations

- **Cliquez sur un marqueur** pour voir ses détails
- **Panneau d'analyse** avec onglets organisés
- **Liste des points** interactive avec recherche visuelle
- **Distinction visuelle** entre points principaux et annotations

## 🎨 Types de points affichés

| Type | Couleur | Description |
|------|---------|-------------|
| **Points principaux** | 🔵 Bleu | Points d'intérêt principaux |
| **Annotations** | 🔴 Rouge | Points du dossier "Points" (détails) |
| **Traces GPS** | 🟣 Violet | Lignes de trajectoire |

## 🔧 Configuration avancée

### Variables d'environnement

```bash
FLASK_ENV=production          # Mode de l'application
FLASK_APP=app.py             # Point d'entrée
```

### Ports personnalisés

Modifiez le fichier `docker-compose.yml` :

```yaml
ports:
  - "VOTRE_PORT:5000"  # Remplacez VOTRE_PORT
```

### Ajout de fonds de carte

Modifiez le fichier `web-app/templates/index.html` dans la section `baseLayers`.

## 🐛 Dépannage

### Problèmes courants

**1. Port déjà utilisé**

```bash
# Changer le port dans docker-compose.yml
# Ou arrêter le service utilisant le port 8080
sudo lsof -i :8080
```

**2. Fichier KML non reconnu**

- Vérifiez que le fichier est un KML valide
- Assurez-vous de l'encodage UTF-8
- Consultez les logs : `./logs.sh`

**3. Carte ne s'affiche pas**

- Vérifiez votre connexion internet
- Ouvrez la console du navigateur (F12)
- Redémarrez l'application : `./restart.sh`

**4. Navigation clavier ne fonctionne pas**

- Cliquez sur la carte pour donner le focus
- Assurez-vous qu'un fichier KML avec des points est chargé

### Logs et diagnostic

```bash
./logs.sh                    # Logs en temps réel
docker ps                    # État des conteneurs
docker-compose ps            # État spécifique au projet
```

## 📊 Performances

- **Fichiers KML** : Jusqu'à 16MB supportés
- **Points** : Navigation optimisée pour milliers de points
- **Mémoire** : Conteneur léger (~100MB)
- **Démarrage** : ~10-30 secondes selon la machine

## 🔒 Sécurité

- **Validation des fichiers** : Extensions et taille limitées
- **Noms sécurisés** : Protection contre path traversal
- **Parsing sécurisé** : Protection contre XML bombs
- **Pas de persistance** : Fichiers uploadés temporaires

## 🤝 Contribution

1. Fork du projet
2. Créer une branche feature
3. Commit des modifications
4. Push vers la branche
5. Créer une Pull Request

## 📄 Licence

Ce projet est libre d'utilisation.

---

## 🆕 Nouveautés Phase 4 - Analyse et Édition Avancées

### 🔬 Analyses automatisées

- **Détection d'arrêts** : Identification automatique des pauses avec durée et localisation
- **Segmentation intelligente** : Division de la trajectoire en segments homogènes
- **Zones de vitesse** : Classification colorée des sections selon la vitesse
- **Analyse du terrain** : Répartition automatique montées/descentes/plat
- **Points d'intérêt** : Détection des lieux remarquables sur le parcours

### ✏️ Édition interactive

- **Ajout de points** : Création de nouveaux waypoints par clic sur carte
- **Modification en temps réel** : Édition des métadonnées (nom, description)
- **Suppression sélective** : Retrait de points indésirables
- **Simplification Douglas-Peucker** : Optimisation automatique des traces
- **Interface intuitive** : Modes d'édition avec feedback visuel

### 📊 Export professionnel

- **Multi-formats** : GPX, CSV, GeoJSON, KML avec options personnalisables
- **Compatibilité étendue** : Support des principaux logiciels GPS et SIG
- **Métadonnées préservées** : Conservation des informations d'origine
- **Téléchargement direct** : Export instantané sans rechargement

## 🎯 Cas d'usage typiques

### 🛩️ Aviation

- **Plans de vol** : Visualisation et édition de routes aériennes
- **Waypoints** : Gestion des points de navigation
- **Analyse de performance** : Vitesses, altitudes, temps de vol
- **Export vers GPS** : Formats compatibles équipements de bord

### 🥾 Randonnée et Outdoor

- **Traces GPS** : Visualisation de parcours avec profils d'élévation
- **Points d'intérêt** : Refuges, sommets, points de vue
- **Analyse d'effort** : Dénivelés, vitesses, temps d'arrêt
- **Partage** : Export vers applications mobiles (GPX)

### 🔬 Recherche et Géologie

- **Points de mesure** : Localisation précise d'échantillons
- **Parcours terrain** : Optimisation des itinéraires de collecte
- **Analyse spatiale** : Export vers SIG (GeoJSON, CSV)
- **Documentation** : Métadonnées détaillées par point

### 🏃‍♂️ Sport et Fitness

- **Entraînements** : Analyse de performances (vitesse, dénivelé)
- **Zones d'effort** : Identification des sections difficiles
- **Progression** : Comparaison de parcours similaires
- **Partage** : Export vers plateformes sportives

### 🚗 Transport et Logistique

- **Optimisation de routes** : Analyse de trajets commerciaux
- **Points d'arrêt** : Détection automatique des pauses
- **Rapports** : Export de données pour analyse (CSV)
- **Suivi** : Visualisation de flottes ou livraisons

**Profitez de votre exploration cartographique avancée ! 🗺️✈️📊**

## 🚀 Nouveautés Phase 5 - Optimisations

- **Calculs vectorisés** pour la distance totale (numpy)
- **Mise en cache** des parsings KML/GPX pour éviter les recalculs
- **Tests supplémentaires** couvrant le cache
- **Instructions de test** : `pytest`
