[![Build and Push Docker Image](https://github.com/ttiot/KmlViewer/actions/workflows/docker.yml/badge.svg)](https://github.com/ttiot/KmlViewer/actions/workflows/docker.yml)

# 🗺️ KmlViewer

Application web auto-hébergée pour visualiser, analyser et archiver des traces GPS (KML, KMZ, GPX) :
un visualiseur cartographique, et une bibliothèque d'activités avec tableau de bord et tronçons chronométrés.

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

```bash
cd web-app
pip install --require-hashes -r requirements.txt -r requirements-dev.txt
python -m ruff check .
python -m pytest --cov=app
```

## 🏃 Bibliothèque, tableau de bord, tronçons et comptes

Dossier surveillé, import depuis l'interface, statistiques par semaine / mois / année, tronçons
personnels chronométrés, correction d'altitude par MNT, authentification OIDC (FusionAuth),
sécurité et sauvegarde : voir [docs/bibliotheque.md](docs/bibliotheque.md).
Configuration : copier `.env.example` en `.env`.

## ✨ Visualiseur

### 📁 Fichiers

- **Formats** : KML, KMZ et GPX, jusqu'à 16 Mo, plusieurs fichiers à la fois (glisser-déposer ou sélection)
- **Fichiers d'exemple** : les traces placées dans `web-app/sample_files/` sont proposées dans la barre
  latérale, à tous les comptes
- **Multi-fichiers** : chaque fichier a sa case d'affichage et son onglet ; l'onglet actif alimente
  la liste des points, l'analyse, l'édition et l'export
- **Modes d'affichage** : double unité (km/h + kts, ft + m) ou unité simple

### 🗺️ Carte

- **5 fonds de carte** : OpenStreetMap, Satellite (Esri), Terrain (OpenTopoMap), CartoDB Positron, CartoDB Dark Matter
- Traces, polygones et points aux couleurs des styles KML, popups de détail
- **Navigation entre les points** : flèches ← → du clavier, boutons Précédent/Suivant, clic dans la liste ou sur un marqueur

### 📊 Analyse

- **Stats, Élévation, Vitesse** : distance, durée et temps en mouvement mesurés, vitesses, dénivelés, profils en graphique (rien n'est estimé : une trace non horodatée n'a ni durée ni vitesse)
- **Avancé** (à la demande) : arrêts détectés, segments homogènes, zones de vitesse, pentes du terrain

### ✏️ Édition

Les modifications portent sur le fichier affiché, dans le navigateur ; l'export les enregistre.

- **Ajouter un point** : bouton, puis clic sur la carte, puis nom et description
- **Modifier / supprimer un point** : bouton, puis clic sur le point (carte ou liste) ; Échap annule
- **Simplifier les traces** : algorithme de Douglas-Peucker, tolérance réglable en mètres

### 📤 Export

GPX, CSV, GeoJSON ou KML, avec nom de fichier au choix. Les points de trace peuvent être
exclus du CSV.

## 🛠️ Scripts utilitaires

| Script | Description |
|--------|-------------|
| `./start.sh` | Démarre l'application avec Docker |
| `./stop.sh` | Arrête l'application |
| `./restart.sh` | Redémarre l'application |
| `./logs.sh` | Affiche les logs en temps réel |
| `./cleanup.sh` | Nettoyage complet du projet, **données comprises** (demande confirmation) |

Commandes d'exploitation (`docker compose exec kml-viewer flask <commande>`) :

| Commande | Description |
|----------|-------------|
| `backup [--keep N]` | Copie cohérente de la base dans `/data/backups` |
| `inbox-scan` | Passe une fois sur le dossier surveillé |
| `recompute-stats` | Recalcule distance, durées et records des activités déjà stockées (à lancer après une mise à jour qui corrige ces calculs) |
| `user-admin <login>` | Donne (ou retire, `--revoke`) le rôle admin |

## 🏗️ Architecture

```text
web-app/
├── kml_viewer.py          point d'entrée (gunicorn kml_viewer:app)
├── app/
│   ├── __init__.py        factory Flask
│   ├── errors.py          réponses d'erreur JSON, sans détail interne
│   ├── security.py        CSP, en-têtes, contrôle d'origine (CSRF)
│   ├── auth/              OIDC, sessions, contrôle d'accès
│   ├── api/               routes REST (visualiseur, analyse, export, bibliothèque, tronçons)
│   ├── services/          parsing KML/GPX/KMZ, analyse, ingestion, MNT, tâche de fond
│   ├── db.py              SQLite et migrations
│   └── cli.py             commandes d'exploitation
├── static/
│   ├── js/viewer/         visualiseur : core, map, points, files, analysis, editor, main
│   ├── js/                pages bibliothèque (dashboard, segments, users) et utilitaires
│   └── vendor/            Leaflet, Bootstrap, Chart.js, Font Awesome (embarqués, sans CDN)
├── templates/
└── tests/
```

- **Backend** : Python 3.15, Flask, gunicorn, SQLite, numpy ; dépendances verrouillées avec empreintes
- **Frontend** : JavaScript sans framework ni étape de build ; toute donnée issue d'un fichier est
  insérée comme texte, et la CSP n'autorise que les scripts de l'application
- **Déploiement** : image non-root, système de fichiers en lecture seule, port publié sur
  `127.0.0.1:8080` (à placer derrière un reverse proxy TLS)

### API du visualiseur

| Route | Rôle |
|-------|------|
| `POST /api/upload`, `POST /api/multi-upload` | Parse un ou plusieurs fichiers |
| `GET /api/sample-files`, `GET /api/load-sample/<nom>` | Fichiers d'exemple |
| `POST /api/analysis/*` | Analyses de trajectoire |
| `POST /api/editor/simplify-trace` | Simplification d'une trace |
| `POST /api/export/<gpx\|csv\|geojson\|kml>` | Export |
| `GET /api/health` | Santé et version |
| `GET /api/stats` | Temps d'exécution cumulés (admin) |

L'API de la bibliothèque est décrite dans [docs/bibliotheque.md](docs/bibliotheque.md).

## 🔧 Configuration

Tout passe par des variables d'environnement, listées et commentées dans `.env.example`
(clé de session, OIDC, dossier surveillé, correction d'altitude, fuseau horaire).
Pour changer le port, modifier `ports` dans `docker-compose.yml`.

Les fonds de carte se déclarent dans `web-app/static/js/viewer/map.js` (`initMap`) et
`web-app/templates/sidebar.html`.

## 🐛 Dépannage

- **Port déjà utilisé** : changer le port dans `docker-compose.yml`, ou trouver le service qui
  l'occupe (`sudo lsof -i :8080`).
- **Fichier refusé** : le message indique la cause (extension, encodage UTF-8 attendu, archive
  KMZ illisible). Les détails d'une erreur interne sont dans les logs : `./logs.sh`.
- **Fond de carte vide** : les tuiles viennent de serveurs tiers, une connexion Internet est
  nécessaire ; le reste de l'application fonctionne hors ligne.
- **Flèches du clavier sans effet** : un fichier contenant des points doit être chargé, et le
  curseur ne doit pas être dans un champ de saisie.

## 📄 Licence

Ce projet est libre d'utilisation.
