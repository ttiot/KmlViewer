# Visualiseur KML - Application Web

Application web interactive pour visualiser des fichiers KML sur une carte avec différents fonds de carte.

## Fonctionnalités

- 📁 **Upload de fichiers KML** : Glissez-déposez ou sélectionnez vos fichiers KML
- 🗺️ **Multiples fonds de carte** :
  - OpenStreetMap (par défaut)
  - Satellite (Esri)
  - Terrain (OpenTopoMap)
  - CartoDB Positron
  - Stamen Toner
- 📍 **Affichage des traces GPS** : Visualisation des polylignes et points d'intérêt
- 📋 **Informations détaillées** : Affichage des métadonnées des éléments KML
- 📱 **Interface responsive** : Compatible mobile et desktop
- 🔄 **Fichiers d'exemple** : Chargement rapide des fichiers KML du projet

## Technologies utilisées

### Backend

- **Flask** : Framework web Python
- **Python 3.11** : Langage de programmation
- **XML ElementTree** : Parsing des fichiers KML

### Frontend

- **Leaflet** : Bibliothèque de cartes interactives
- **Bootstrap 5** : Framework CSS
- **Font Awesome** : Icônes
- **JavaScript ES6** : Logique côté client

### Déploiement

- **Docker** : Conteneurisation
- **Docker Compose** : Orchestration

## Installation et utilisation

### Avec Docker Compose (Recommandé)

1. **Cloner le projet** :

   ```bash
   git clone <votre-repo>
   cd gps-clean-kml
   ```

2. **Lancer l'application** :

   ```bash
   docker-compose up -d
   ```

3. **Accéder à l'application** :
   Ouvrez votre navigateur à l'adresse : <http://localhost:8080>

4. **Arrêter l'application** :

   ```bash
   docker-compose down
   ```

### Installation manuelle

1. **Prérequis** :
   - Python 3.11+
   - pip

2. **Installation des dépendances** :

   ```bash
   cd web-app
   pip install -r requirements.txt
   ```

3. **Lancer l'application** :

   ```bash
   python app.py
   ```

4. **Accéder à l'application** :
   Ouvrez votre navigateur à l'adresse : <http://localhost:5000>

## Utilisation

### Upload de fichiers KML

1. **Glissez-déposez** votre fichier KML dans la zone prévue
2. Ou **cliquez** sur la zone pour sélectionner un fichier
3. Le fichier sera automatiquement traité et affiché sur la carte

### Changement de fond de carte

1. Utilisez les **boutons radio** dans le panneau de droite
2. Sélectionnez le fond de carte souhaité :
   - **OpenStreetMap** : Carte standard collaborative
   - **Satellite** : Images satellite haute résolution
   - **Terrain** : Carte topographique avec relief
   - **CartoDB Positron** : Carte minimaliste claire
   - **Stamen Toner** : Carte en noir et blanc

### Fichiers d'exemple

- Cliquez sur les **boutons des fichiers d'exemple** pour charger rapidement les fichiers KML du projet
- Parfait pour tester l'application sans avoir ses propres fichiers

## Structure du projet

```bash
web-app/
├── app.py                 # Application Flask principale
├── templates/
│   └── index.html        # Interface utilisateur
├── requirements.txt      # Dépendances Python
├── Dockerfile           # Configuration Docker
├── .dockerignore        # Fichiers ignorés par Docker
└── README.md           # Cette documentation
```

## API Endpoints

- `GET /` : Page principale
- `POST /upload` : Upload et traitement d'un fichier KML
- `GET /sample-files` : Liste des fichiers d'exemple
- `GET /load-sample/<filename>` : Chargement d'un fichier d'exemple

## Format KML supporté

L'application supporte les éléments KML suivants :

- **Placemark** avec **LineString** : Traces GPS (polylignes)
- **Placemark** avec **Point** : Points d'intérêt (marqueurs)
- **Métadonnées** : Nom et description des éléments

## Sécurité

- Limitation de la taille des fichiers à 16MB
- Validation des extensions de fichiers (.kml uniquement)
- Sécurisation des noms de fichiers avec `secure_filename`
- Parsing XML sécurisé avec ElementTree

## Développement

### Variables d'environnement

- `FLASK_ENV` : Mode de développement (`development` ou `production`)
- `FLASK_APP` : Point d'entrée de l'application (`app.py`)

### Mode développement

```bash
export FLASK_ENV=development
python app.py
```

L'application se rechargera automatiquement lors des modifications du code.

## Dépannage

### Problèmes courants

1. **Port déjà utilisé** :
   - Modifiez le port dans `docker-compose.yml` (ligne `ports`)
   - Ou arrêtez le service utilisant le port 8080

2. **Fichier KML non reconnu** :
   - Vérifiez que le fichier est un KML valide
   - Assurez-vous que l'encodage est UTF-8

3. **Carte ne s'affiche pas** :
   - Vérifiez votre connexion internet (les tuiles sont chargées en ligne)
   - Consultez la console du navigateur pour les erreurs JavaScript

## Licence

Ce projet est libre d'utilisation.
