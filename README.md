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
   Ouvrez votre navigateur à l'adresse : **http://localhost:8080**

3. **Arrêter l'application** :
   ```bash
   ./stop.sh
   ```

## ✨ Fonctionnalités principales

### 📁 Gestion des fichiers KML
- **Upload par glisser-déposer** ou sélection de fichiers
- **Fichiers d'exemple** intégrés pour test rapide
- **Support complet** des éléments KML (traces GPS, points, annotations)
- **Parsing intelligent** des métadonnées (nom, description, altitude)

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

### 📱 Interface utilisateur
- **Design responsive** compatible mobile/desktop
- **Interface moderne** avec Bootstrap 5
- **Icônes Font Awesome** pour une meilleure UX
- **Messages d'état** informatifs et colorés
- **Panneau d'informations** détaillé sur les éléments

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
- **Framework** : Flask 2.3.3
- **Parsing KML** : xml.etree.ElementTree
- **Sécurité** : Validation des fichiers, noms sécurisés
- **API REST** : Endpoints pour upload et gestion des fichiers

### Frontend (JavaScript/HTML/CSS)
- **Cartes** : Leaflet.js (bibliothèque légère et performante)
- **UI** : Bootstrap 5 + Font Awesome
- **JavaScript ES6** : Navigation, gestion des événements
- **Responsive design** : Compatible tous écrans

### Déploiement (Docker)
- **Conteneurisation** : Docker + Docker Compose
- **Port** : 8080 (configurable)
- **Volumes** : Montage des fichiers d'exemple
- **Health checks** : Surveillance automatique

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

- Utilisez les boutons radio dans le panneau de droite
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

### 4. Consulter les informations

- **Cliquez sur un marqueur** pour voir ses détails
- **Panneau d'informations** en bas avec résumé complet
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

## 🎯 Cas d'usage typiques

- **Aviation** : Visualisation de plans de vol et waypoints
- **Randonnée** : Traces GPS et points d'intérêt
- **Géologie** : Points de mesure et parcours terrain
- **Tourisme** : Itinéraires et lieux remarquables
- **Recherche** : Données géospatiales et analyses

**Profitez de votre exploration cartographique ! 🗺️✈️**