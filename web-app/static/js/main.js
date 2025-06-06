// Variables globales
let map;
let currentKmlLayer;
let baseLayers = {};
let allPoints = [];
let allFeatures = [];
let currentPointIndex = -1;
let pointMarkers = [];

// Variables pour l'analyse - Phase 3
let currentAnalysis = null;
let elevationChart = null;
let speedChart = null;

// Initialisation de la carte
function initMap() {
    map = L.map('map').setView([46.603354, 1.888334], 6); // Centre de la France
    
    // Définition des couches de base
    baseLayers = {
        'osm': L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
            attribution: '© OpenStreetMap contributors'
        }),
        'satellite': L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', {
            attribution: '© Esri'
        }),
        'terrain': L.tileLayer('https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png', {
            attribution: '© OpenTopoMap contributors'
        }),
        'cartodb': L.tileLayer('https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png', {
            attribution: '© CartoDB'
        }),
        'stamen': L.tileLayer('https://stamen-tiles-{s}.a.ssl.fastly.net/toner/{z}/{x}/{y}{r}.png', {
            attribution: '© Stamen Design'
        })
    };
    
    // Ajouter la couche par défaut
    baseLayers['osm'].addTo(map);
    
    // Gestionnaire de changement de couche
    document.querySelectorAll('input[name="baseLayer"]').forEach(radio => {
        radio.addEventListener('change', function() {
            // Supprimer toutes les couches de base
            Object.values(baseLayers).forEach(layer => {
                map.removeLayer(layer);
            });
            
            // Ajouter la nouvelle couche
            baseLayers[this.value].addTo(map);
        });
    });
}

// Affichage des alertes en popup
function showAlert(message, type = 'info') {
    const alertContainer = document.getElementById('alertContainer');
    const alertDiv = document.createElement('div');
    alertDiv.className = `alert-popup alert-${type}`;
    alertDiv.innerHTML = `
        <div class="alert-content">${message}</div>
        <button type="button" class="btn-close" onclick="this.parentElement.remove()">&times;</button>
    `;
    
    // Positionner les alertes les unes au-dessus des autres
    const existingAlerts = alertContainer.querySelectorAll('.alert-popup');
    let bottomOffset = 20;
    existingAlerts.forEach(alert => {
        bottomOffset += alert.offsetHeight + 10;
    });
    alertDiv.style.bottom = bottomOffset + 'px';
    
    alertContainer.appendChild(alertDiv);
    
    // Animation d'entrée
    setTimeout(() => {
        alertDiv.classList.add('show');
    }, 100);
    
    // Auto-suppression après 10 secondes
    setTimeout(() => {
        if (alertDiv.parentNode) {
            alertDiv.classList.remove('show');
            setTimeout(() => {
                if (alertDiv.parentNode) {
                    alertDiv.remove();
                    // Réajuster les positions des alertes restantes
                    repositionAlerts();
                }
            }, 300);
        }
    }, 3000);
}

// Fonction pour repositionner les alertes après suppression
function repositionAlerts() {
    const alerts = document.querySelectorAll('.alert-popup');
    let bottomOffset = 20;
    alerts.forEach(alert => {
        alert.style.bottom = bottomOffset + 'px';
        bottomOffset += alert.offsetHeight + 10;
    });
}

// Chargement des fichiers d'exemple
function loadSampleFiles() {
    fetch('/api/sample-files')
        .then(response => response.json())
        .then(data => {
            const container = document.getElementById('sampleFiles');
            container.innerHTML = '';
            
            data.files.forEach(file => {
                const button = document.createElement('button');
                button.className = 'btn btn-outline-primary btn-sm sample-file-btn';
                
                // Tronquer le nom si trop long
                const displayName = file.name.length > 25 ? file.name.substring(0, 22) + '...' : file.name;
                button.textContent = displayName;
                button.setAttribute('data-full-name', file.name);
                button.title = file.name; // Fallback pour les navigateurs qui ne supportent pas les pseudo-éléments
                button.onclick = () => loadSampleFile(file.name);
                container.appendChild(button);
            });
        })
        .catch(error => {
            console.error('Erreur lors du chargement des fichiers d\'exemple:', error);
        });
}

// Chargement d'un fichier d'exemple
function loadSampleFile(filename) {
    showAlert(`<i class="fas fa-spinner fa-spin me-2"></i>Chargement de ${filename}...`, 'info');
    
    // Récupérer le mode d'affichage sélectionné
    const displayMode = document.querySelector('input[name="displayMode"]:checked').value;
    
    fetch(`/api/load-sample/${filename}?display_mode=${displayMode}`)
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                displayKmlData(data);
                showAlert(`<i class="fas fa-check me-2"></i>Fichier ${filename} chargé avec succès!`, 'success');
            } else {
                showAlert(`<i class="fas fa-exclamation-triangle me-2"></i>Erreur: ${data.error}`, 'danger');
            }
        })
        .catch(error => {
            showAlert(`<i class="fas fa-exclamation-triangle me-2"></i>Erreur lors du chargement: ${error.message}`, 'danger');
        });
}

// Affichage des données KML sur la carte
function displayKmlData(data) {
    // Supprimer la couche précédente si elle existe
    if (currentKmlLayer) {
        map.removeLayer(currentKmlLayer);
    }
    
    // Réinitialiser les variables de navigation
    allPoints = data.points || [];
    allFeatures = data.features || [];
    currentPointIndex = -1;
    pointMarkers = [];
    
    // Créer un groupe de couches pour les features KML
    currentKmlLayer = L.layerGroup();
    
    let bounds = L.latLngBounds();
    let featureCount = 0;
    
    data.features.forEach((feature, index) => {
        if (feature.type === 'polyline') {
            const polyline = L.polyline(feature.coordinates, {
                color: '#667eea',
                weight: 4,
                opacity: 0.8
            });
            
            polyline.bindPopup(`
                <div class="popup-content">
                    <strong>${feature.name}</strong><br>
                    ${feature.description || 'Trace GPS'}
                </div>
            `);
            
            currentKmlLayer.addLayer(polyline);
            bounds.extend(feature.coordinates);
            featureCount++;
            
        } else if (feature.type === 'marker') {
            // Créer un marqueur avec une couleur différente selon le type
            const markerColor = feature.is_annotation ? '#ff6b6b' : '#4ecdc4';
            const markerIcon = L.divIcon({
                className: 'custom-marker',
                html: `<div style="background-color: ${markerColor}; width: 12px; height: 12px; border-radius: 50%; border: 2px solid white; box-shadow: 0 2px 4px rgba(0,0,0,0.3);"></div>`,
                iconSize: [16, 16],
                iconAnchor: [8, 8]
            });
            
            const marker = L.marker(feature.coordinates, { icon: markerIcon });
            
            const popupContent = `
                <div class="popup-content">
                    <strong>${feature.name}</strong><br>
                    ${feature.description || 'Point d\'intérêt'}<br>
                    ${feature.altitude ? `<small>Altitude: ${Math.round(feature.altitude)}m</small><br>` : ''}
                    <small>Point ${feature.index + 1}/${data.total_points}</small>
                    <div class="mt-2">
                        <button class="btn btn-sm btn-outline-primary" onclick="navigateToPoint(${feature.index})">
                            <i class="fas fa-crosshairs"></i> Centrer
                        </button>
                    </div>
                </div>
            `;
            
            marker.bindPopup(popupContent);
            
            // Ajouter un gestionnaire de clic pour la navigation et mise en surbrillance
            marker.on('click', function() {
                selectPoint(feature.index);
            });
            
            currentKmlLayer.addLayer(marker);
            pointMarkers.push(marker);
            bounds.extend(feature.coordinates);
            featureCount++;
        }
    });
    
    // Ajouter la couche à la carte
    currentKmlLayer.addTo(map);
    
    // Ajuster la vue sur les données
    if (bounds.isValid()) {
        map.fitBounds(bounds, { padding: [20, 20] });
    }
    
    // Afficher les informations
    // displayFeatureInfo(data.features);
    
    // Générer le listing des points
    displayPointsList();
    
    // Afficher les contrôles de navigation si il y a des points
    if (allPoints.length > 0) {
        showNavigationControls();
        showAlert(`<i class="fas fa-check me-2"></i>${featureCount} élément(s) affiché(s) sur la carte. Utilisez les flèches ← → pour naviguer entre les ${allPoints.length} points.`, 'success');
    } else {
        hideNavigationControls();
        showAlert(`<i class="fas fa-check me-2"></i>${featureCount} élément(s) affiché(s) sur la carte`, 'success');
    }
    
    // Lancer l'analyse de trajectoire - Phase 3
    analyzeTrajectory(data.features);
}

// Affichage des informations sur les features
function displayFeatureInfo(features) {
    const infoDiv = document.getElementById('featureInfo');
    const detailsDiv = document.getElementById('featureDetails');
    
    if (features.length === 0) {
        infoDiv.style.display = 'none';
        return;
    }
    
    let html = '<ul class="list-unstyled">';
    let pointCount = 0;
    let traceCount = 0;
    
    features.forEach((feature, index) => {
        if (feature.type === 'marker') {
            pointCount++;
        } else if (feature.type === 'polyline') {
            traceCount++;
        }
        
        html += `
            <li class="mb-2">
                <i class="fas fa-${feature.type === 'polyline' ? 'route' : 'map-marker-alt'} me-2"></i>
                <strong>${feature.name}</strong>
                ${feature.description ? `<br><small class="text-muted">${feature.description}</small>` : ''}
                ${feature.type === 'marker' ? `<br><small class="badge bg-${feature.is_annotation ? 'warning' : 'info'}">${feature.is_annotation ? 'Annotation' : 'Point principal'}</small>` : ''}
            </li>
        `;
    });
    html += '</ul>';
    
    if (pointCount > 0) {
        html += `<div class="mt-3 p-2 bg-light rounded">
            <small><strong>Résumé:</strong> ${traceCount} trace(s), ${pointCount} point(s)</small>
        </div>`;
    }
    
    detailsDiv.innerHTML = html;
    infoDiv.style.display = 'block';
}

// Navigation entre les points
function navigateToPoint(index) {
    if (index < 0 || index >= allPoints.length) return;
    
    selectPoint(index);
    const point = allPoints[index];
    
    // Centrer la carte sur le point
    map.setView(point.coordinates, Math.max(map.getZoom(), 15));
    
    // Ouvrir le popup du marqueur correspondant
    if (pointMarkers[index]) {
        pointMarkers[index].openPopup();
    }
}

function navigateToNextPoint() {
    if (allPoints.length === 0) return;
    
    const nextIndex = (currentPointIndex + 1) % allPoints.length;
    navigateToPoint(nextIndex);
}

function navigateToPreviousPoint() {
    if (allPoints.length === 0) return;
    
    const prevIndex = currentPointIndex <= 0 ? allPoints.length - 1 : currentPointIndex - 1;
    navigateToPoint(prevIndex);
}

function updateNavigationInfo() {
    const navInfo = document.getElementById('navigationInfo');
    if (currentPointIndex >= 0 && allPoints.length > 0) {
        const point = allPoints[currentPointIndex];
        navInfo.innerHTML = `
            <strong>Point ${currentPointIndex + 1}/${allPoints.length}</strong><br>
            <small>${point.name}</small>
        `;
        navInfo.style.display = 'block';
    } else {
        navInfo.style.display = 'none';
    }
}

function showNavigationControls() {
    const navControls = document.getElementById('navigationControls');
    if (navControls) {
        navControls.style.display = 'block';
    }
}

function hideNavigationControls() {
    const navControls = document.getElementById('navigationControls');
    if (navControls) {
        navControls.style.display = 'none';
    }
}

// Génération du listing des points
function displayPointsList() {
    const pointsList = document.getElementById('pointsList');
    const pointsListContent = document.getElementById('pointsListContent');
    const pointsCount = document.getElementById('pointsCount');
    
    if (allPoints.length === 0) {
        pointsList.style.display = 'none';
        return;
    }
    
    // Mettre à jour le compteur
    pointsCount.textContent = allPoints.length;
    
    // Générer le contenu du listing
    let html = '';
    allPoints.forEach((point, index) => {
        const markerColor = point.is_annotation ? '#ff6b6b' : '#4ecdc4';
        const badgeClass = point.is_annotation ? 'bg-warning' : 'bg-info';
        const badgeText = point.is_annotation ? 'Annotation' : 'Point principal';
        
        html += `
            <div class="point-item" data-point-index="${index}" onclick="selectPointFromList(${index})">
                <div class="d-flex align-items-start">
                    <div class="me-3 mt-1">
                        <div style="background-color: ${markerColor}; width: 12px; height: 12px; border-radius: 50%; border: 2px solid white; box-shadow: 0 2px 4px rgba(0,0,0,0.3);"></div>
                    </div>
                    <div class="flex-grow-1">
                        <div class="point-name">${point.name}</div>
                        ${point.description ? `<div class="point-description">${point.description}</div>` : ''}
                        <div class="point-meta">
                            <span class="badge ${badgeClass} point-badge me-2">${badgeText}</span>
                            <small class="text-muted">Point ${index + 1}/${allPoints.length}</small>
                            ${point.altitude ? `<small class="text-muted ms-2">Alt: ${Math.round(point.altitude)}m</small>` : ''}
                        </div>
                    </div>
                </div>
            </div>
        `;
    });
    
    pointsListContent.innerHTML = html;
    pointsList.style.display = 'block';
}

// Sélection d'un point depuis le listing
function selectPointFromList(index) {
    selectPoint(index);
    
    // Centrer la carte sur le point
    const point = allPoints[index];
    map.setView(point.coordinates, Math.max(map.getZoom(), 15));
    
    // Ouvrir le popup du marqueur correspondant
    if (pointMarkers[index]) {
        pointMarkers[index].openPopup();
    }
}

// Sélection d'un point (fonction commune)
function selectPoint(index) {
    if (index < 0 || index >= allPoints.length) return;
    
    currentPointIndex = index;
    
    // Mettre à jour la navigation
    updateNavigationInfo();
    
    // Mettre en surbrillance dans le listing
    updatePointsListHighlight();
}

// Mise à jour de la surbrillance dans le listing des points
function updatePointsListHighlight() {
    // Supprimer toutes les surbrillances existantes
    document.querySelectorAll('.point-item').forEach(item => {
        item.classList.remove('active');
    });
    
    // Ajouter la surbrillance au point actuel
    if (currentPointIndex >= 0) {
        const activeItem = document.querySelector(`[data-point-index="${currentPointIndex}"]`);
        if (activeItem) {
            activeItem.classList.add('active');
            
            // Faire défiler pour rendre visible le point sélectionné
            activeItem.scrollIntoView({
                behavior: 'smooth',
                block: 'nearest'
            });
        }
    }
}

// Gestion des touches du clavier
function setupKeyboardNavigation() {
    document.addEventListener('keydown', function(e) {
        if (allPoints.length === 0) return;
        
        switch(e.key) {
            case 'ArrowLeft':
                e.preventDefault();
                navigateToPreviousPoint();
                break;
            case 'ArrowRight':
                e.preventDefault();
                navigateToNextPoint();
                break;
        }
    });
}

// Gestion du drag & drop et sélection de fichier
function setupFileHandling() {
    const fileUploadArea = document.getElementById('fileUploadArea');
    const fileInput = document.getElementById('fileInput');
    
    // Clic sur la zone de téléchargement
    fileUploadArea.addEventListener('click', () => {
        fileInput.click();
    });
    
    // Sélection de fichier
    fileInput.addEventListener('change', (e) => {
        if (e.target.files.length > 0) {
            uploadFile(e.target.files[0]);
        }
    });
    
    // Drag & Drop
    fileUploadArea.addEventListener('dragover', (e) => {
        e.preventDefault();
        fileUploadArea.classList.add('dragover');
    });
    
    fileUploadArea.addEventListener('dragleave', () => {
        fileUploadArea.classList.remove('dragover');
    });
    
    fileUploadArea.addEventListener('drop', (e) => {
        e.preventDefault();
        fileUploadArea.classList.remove('dragover');
        
        if (e.dataTransfer.files.length > 0) {
            uploadFile(e.dataTransfer.files[0]);
        }
    });
}

// Upload de fichier
function uploadFile(file) {
    if (!file.name.toLowerCase().endsWith('.kml')) {
        showAlert('<i class="fas fa-exclamation-triangle me-2"></i>Seuls les fichiers .kml sont acceptés', 'warning');
        return;
    }
    
    const formData = new FormData();
    formData.append('file', file);
    
    // Ajouter le mode d'affichage sélectionné
    const displayMode = document.querySelector('input[name="displayMode"]:checked').value;
    formData.append('display_mode', displayMode);
    
    showAlert(`<i class="fas fa-spinner fa-spin me-2"></i>Traitement de ${file.name}...`, 'info');
    
    fetch('/api/upload', {
        method: 'POST',
        body: formData
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            displayKmlData(data);
            showAlert(`<i class="fas fa-check me-2"></i>Fichier ${file.name} traité avec succès!`, 'success');
        } else {
            showAlert(`<i class="fas fa-exclamation-triangle me-2"></i>Erreur: ${data.error}`, 'danger');
        }
    })
    .catch(error => {
        showAlert(`<i class="fas fa-exclamation-triangle me-2"></i>Erreur lors de l'upload: ${error.message}`, 'danger');
    });
}

// Fonction pour recharger les données avec un nouveau mode d'affichage
function reloadWithDisplayMode() {
    // Cette fonction sera appelée quand on change le mode d'affichage
    // Pour l'instant, on affiche juste un message à l'utilisateur
    showAlert('<i class="fas fa-info-circle me-2"></i>Veuillez recharger votre fichier KML pour appliquer le nouveau mode d\'affichage', 'info');
}

// Gestionnaire pour le changement de mode d'affichage
function setupDisplayModeHandling() {
    document.querySelectorAll('input[name="displayMode"]').forEach(radio => {
        radio.addEventListener('change', function() {
            // Si des données sont déjà chargées, informer l'utilisateur
            if (allPoints.length > 0) {
                reloadWithDisplayMode();
            }
        });
    });
}

// Gestion de la sidebar
function setupSidebar() {
    const sidebar = document.getElementById('sidebar');
    const sidebarToggle = document.getElementById('sidebarToggle');
    const mainContent = document.getElementById('mainContent');
    
    sidebarToggle.addEventListener('click', function() {
        sidebar.classList.toggle('collapsed');
        mainContent.classList.toggle('expanded');
        
        // Changer l'icône du bouton
        const icon = sidebarToggle.querySelector('i');
        if (sidebar.classList.contains('collapsed')) {
            icon.className = 'fas fa-chevron-right';
        } else {
            icon.className = 'fas fa-chevron-left';
        }
        
        // Redimensionner la carte après l'animation
        setTimeout(() => {
            if (map) {
                map.invalidateSize();
            }
        }, 300);
    });
}

// Initialisation
document.addEventListener('DOMContentLoaded', function() {
    initMap();
    setupFileHandling();
    loadSampleFiles();
    setupKeyboardNavigation();
    setupDisplayModeHandling();
    setupSidebar();
});