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

// Variables pour l'overlay de sélection KML
let kmlOverlay = null;
let kmlLayers = new Map(); // Stockage des couches individuelles
let kmlData = null; // Données KML actuelles

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
    // Vérifier s'il y a une erreur dans les données
    if (data.error) {
        showAlert(`<i class="fas fa-exclamation-triangle me-2"></i>Erreur: ${data.error}`, 'danger');
        return;
    }
    
    // Stocker les données pour l'overlay
    kmlData = data;
    
    // Supprimer la couche précédente et l'overlay si ils existent
    if (currentKmlLayer) {
        map.removeLayer(currentKmlLayer);
    }
    removeKmlOverlay();
    removeScreenOverlays();
    
    // Réinitialiser les variables de navigation
    allPoints = data.points || [];
    allFeatures = data.features || [];
    currentPointIndex = -1;
    pointMarkers = [];
    kmlLayers.clear();
    
    // Créer un groupe de couches pour les features KML
    currentKmlLayer = L.layerGroup();
    
    let bounds = L.latLngBounds();
    let featureCount = 0;
    
    // Créer les couches individuelles pour chaque feature
    data.features.forEach((feature, index) => {
        let layer = null;
        const style = getFeatureStyle(feature, data.metadata);
        
        if (feature.type === 'polyline' || feature.type === 'track' || feature.type === 'multitrack') {
            layer = L.polyline(feature.coordinates, {
                color: style.lineColor,
                weight: style.width,
                opacity: 0.8
            });
            
            layer.bindPopup(`
                <div class="popup-content">
                    <strong>${feature.name}</strong><br>
                    ${feature.description || 'Trace GPS'}
                </div>
            `);
            
            bounds.extend(feature.coordinates);
            featureCount++;
            
        } else if (feature.type === 'polygon') {
            layer = drawPolygonOnMap(feature, data.metadata);
            
            if (layer) {
                bounds.extend(feature.coordinates);
                featureCount++;
            }
            
        } else if (feature.type === 'marker') {
            // Créer un marqueur avec la couleur du style
            const markerColor = style.color;
            const markerIcon = L.divIcon({
                className: 'custom-marker',
                html: `<div style="background-color: ${markerColor}; width: 12px; height: 12px; border-radius: 50%; border: 2px solid white; box-shadow: 0 2px 4px rgba(0,0,0,0.3);"></div>`,
                iconSize: [16, 16],
                iconAnchor: [8, 8]
            });
            
            layer = L.marker(feature.coordinates, { icon: markerIcon });
            
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
            
            layer.bindPopup(popupContent);
            
            // Ajouter un gestionnaire de clic pour la navigation et mise en surbrillance
            layer.on('click', function() {
                selectPoint(feature.index);
            });
            
            pointMarkers.push(layer);
            bounds.extend(feature.coordinates);
            featureCount++;
            
        } else if (feature.type === 'screen_overlay') {
            // Gérer les screen overlays
            const overlayElement = positionScreenOverlay(feature, data.metadata);
            if (overlayElement) {
                // Ajouter l'overlay à la page
                document.body.appendChild(overlayElement);
                
                // Stocker la référence pour pouvoir le supprimer plus tard
                if (!window.screenOverlays) {
                    window.screenOverlays = [];
                }
                window.screenOverlays.push(overlayElement);
                featureCount++;
            }
        }
        
        if (layer) {
            // Stocker la couche avec un identifiant unique
            const layerId = `feature_${index}`;
            kmlLayers.set(layerId, layer);
            
            // Ajouter la couche au groupe principal et à la carte
            currentKmlLayer.addLayer(layer);
        }
    });
    
    // Ajouter la couche à la carte
    currentKmlLayer.addTo(map);
    
    // Ajuster la vue sur les données
    if (bounds.isValid()) {
        map.fitBounds(bounds, { padding: [20, 20] });
    }
    
    // Créer l'overlay de sélection
    createKmlOverlay(data);
    
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

// Création de l'overlay de sélection KML
function createKmlOverlay(data) {
    // Supprimer l'overlay existant s'il y en a un
    removeKmlOverlay();
    
    // Créer le conteneur de l'overlay
    kmlOverlay = document.createElement('div');
    kmlOverlay.id = 'kmlOverlay';
    kmlOverlay.className = 'kml-overlay';
    
    // Style CSS inline pour l'overlay
    kmlOverlay.style.cssText = `
        position: absolute;
        top: 10px;
        right: 10px;
        background: rgba(255, 255, 255, 0.9);
        border: 1px solid #ccc;
        border-radius: 8px;
        padding: 15px;
        box-shadow: 0 2px 10px rgba(0,0,0,0.1);
        z-index: 1000;
        max-width: 320px;
        max-height: 450px;
        overflow-y: auto;
        font-size: 14px;
        backdrop-filter: blur(5px);
    `;
    
    // Empêcher la propagation des événements de scroll vers la carte
    kmlOverlay.addEventListener('wheel', function(e) {
        e.stopPropagation();
    });
    
    // Empêcher la propagation des événements de souris vers la carte
    kmlOverlay.addEventListener('mousedown', function(e) {
        e.stopPropagation();
    });
    
    kmlOverlay.addEventListener('mousemove', function(e) {
        e.stopPropagation();
    });
    
    kmlOverlay.addEventListener('mouseup', function(e) {
        e.stopPropagation();
    });
    
    // Obtenir le nom du document KML (tronqué si nécessaire)
    let documentName = 'Fichier KML';
    if (data.metadata && data.metadata.title) {
        documentName = data.metadata.title.length > 25 ?
            data.metadata.title.substring(0, 22) + '...' :
            data.metadata.title;
    }
    
    // Créer le contenu de l'overlay
    let overlayContent = `
        <div style="margin-bottom: 10px; font-weight: bold; color: #333; border-bottom: 1px solid #eee; padding-bottom: 8px; display: flex; justify-content: space-between; align-items: center;">
            <span><i class="fas fa-layer-group me-2"></i>Couches KML</span>
            <i class="fas fa-compress-alt" id="toggleAllCategories"
               style="cursor: pointer; font-size: 12px; color: #666; padding: 4px;"
               title="Réduire/Développer tout"
               onclick="toggleAllCategories()"></i>
        </div>
        <div style="margin-bottom: 15px;">
            <label style="display: flex; align-items: center; cursor: pointer; font-weight: 500;" title="${data.metadata?.title || 'Fichier KML'}">
                <input type="checkbox" id="kmlMasterCheckbox" checked style="margin-right: 8px; transform: scale(1.1);">
                <i class="fas fa-file-code me-2" style="color: #667eea;"></i>
                ${documentName}
            </label>
        </div>
        <div style="margin-left: 20px;">
    `;
    
    // Organiser les features par catégorie
    const categories = organizeFeaturesByCategory(data.features || [], data.metadata);
    
    // Ajouter chaque catégorie
    Object.keys(categories).forEach(categoryName => {
        const categoryFeatures = categories[categoryName];
        if (categoryFeatures.length > 0) {
            const categoryInfo = getCategoryInfo(categoryName);
            
            overlayContent += `
                <div style="margin-bottom: 12px;" class="kml-category-container">
                    <div style="display: flex; align-items: center; margin-bottom: 8px;">
                        <i class="fas fa-chevron-down category-toggle-icon"
                           style="cursor: pointer; margin-right: 6px; font-size: 12px; color: #666; transition: transform 0.2s;"
                           onclick="toggleCategoryCollapse('${categoryName}')"></i>
                        <label style="display: flex; align-items: center; cursor: pointer; font-weight: 500; flex: 1;">
                            <input type="checkbox" class="kml-category-checkbox" data-category="${categoryName}" checked style="margin-right: 8px;">
                            <i class="fas ${categoryInfo.icon} me-2" style="color: ${categoryInfo.color}; width: 16px;"></i>
                            ${categoryName} (${categoryFeatures.length})
                        </label>
                    </div>
                    <div class="category-content" data-category="${categoryName}" style="margin-left: 20px; transition: max-height 0.3s ease-out, opacity 0.3s ease-out; overflow: hidden;">
            `;
            
            categoryFeatures.forEach((feature, index) => {
                const colorPreview = createColorPreview(feature, data.metadata);
                
                overlayContent += `
                    <label style="display: flex; align-items: center; cursor: pointer; margin-bottom: 6px; padding: 3px; border-radius: 3px; transition: background-color 0.2s;"
                           onmouseover="this.style.backgroundColor='rgba(0,0,0,0.05)'"
                           onmouseout="this.style.backgroundColor='transparent'">
                        <input type="checkbox" class="kml-feature-checkbox" data-layer-id="feature_${feature.originalIndex}" data-category="${categoryName}" checked style="margin-right: 8px;">
                        ${colorPreview}
                        <span style="flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 13px;" title="${feature.name}">
                            ${feature.name}
                        </span>
                    </label>
                `;
            });
            
            overlayContent += `
                    </div>
                </div>
            `;
        }
    });
    
    // Ajouter les métadonnées si disponibles
    if (data.metadata && Object.keys(data.metadata).length > 0) {
        overlayContent += `
            <div style="margin-bottom: 12px;" class="kml-category-container">
                <div style="display: flex; align-items: center; margin-bottom: 8px;">
                    <i class="fas fa-chevron-down category-toggle-icon"
                       style="cursor: pointer; margin-right: 6px; font-size: 12px; color: #666; transition: transform 0.2s;"
                       onclick="toggleCategoryCollapse('metadata')"></i>
                    <label style="display: flex; align-items: center; cursor: pointer; font-weight: 500; flex: 1;">
                        <input type="checkbox" class="kml-category-checkbox" data-category="metadata" checked style="margin-right: 8px;">
                        <i class="fas fa-info-circle me-2" style="color: #28a745; width: 16px;"></i>
                        Métadonnées
                    </label>
                </div>
                <div class="category-content" data-category="metadata" style="margin-left: 20px; transition: max-height 0.3s ease-out, opacity 0.3s ease-out; overflow: hidden;">
        `;
        
        // Afficher les métadonnées principales
        const metadataItems = getDisplayableMetadata(data.metadata);
        metadataItems.forEach((item, index) => {
            overlayContent += `
                <label style="display: flex; align-items: center; cursor: pointer; margin-bottom: 6px; padding: 3px; border-radius: 3px; transition: background-color 0.2s;"
                       onmouseover="this.style.backgroundColor='rgba(0,0,0,0.05)'"
                       onmouseout="this.style.backgroundColor='transparent'">
                    <input type="checkbox" class="kml-metadata-checkbox" data-meta-id="meta_${index}" data-category="metadata" checked style="margin-right: 8px;">
                    <i class="fas fa-tag me-2" style="color: #6c757d; width: 14px; font-size: 12px;"></i>
                    <span style="flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 13px;" title="${item.name}: ${item.value}">
                        ${item.name}
                    </span>
                </label>
            `;
        });
        
        overlayContent += `
                </div>
            </div>
        `;
    }
    
    overlayContent += `</div>`;
    
    kmlOverlay.innerHTML = overlayContent;
    
    // Ajouter l'overlay à la carte
    map.getContainer().appendChild(kmlOverlay);
    
    // Configurer les gestionnaires d'événements
    setupKmlOverlayEvents();
    
    // Initialiser l'état des catégories (toutes développées par défaut)
    initializeCategoryStates();
}

// Configuration des événements de l'overlay KML
function setupKmlOverlayEvents() {
    if (!kmlOverlay) return;
    
    const masterCheckbox = kmlOverlay.querySelector('#kmlMasterCheckbox');
    const categoryCheckboxes = kmlOverlay.querySelectorAll('.kml-category-checkbox');
    const featureCheckboxes = kmlOverlay.querySelectorAll('.kml-feature-checkbox');
    const metadataCheckboxes = kmlOverlay.querySelectorAll('.kml-metadata-checkbox');
    const allChildCheckboxes = [...categoryCheckboxes, ...featureCheckboxes, ...metadataCheckboxes];
    
    // Gestionnaire pour la case parent (master)
    if (masterCheckbox) {
        masterCheckbox.addEventListener('change', function() {
            const isChecked = this.checked;
            
            // Mettre à jour toutes les cases enfants
            allChildCheckboxes.forEach(checkbox => {
                checkbox.checked = isChecked;
                if (checkbox.classList.contains('kml-feature-checkbox') ||
                    checkbox.classList.contains('kml-metadata-checkbox')) {
                    toggleKmlLayer(checkbox);
                }
            });
            
            // Mettre à jour les états des catégories
            categoryCheckboxes.forEach(categoryCheckbox => {
                updateCategoryCheckboxState(categoryCheckbox.dataset.category);
            });
        });
    }
    
    // Gestionnaires pour les cases de catégorie
    categoryCheckboxes.forEach(categoryCheckbox => {
        categoryCheckbox.addEventListener('change', function() {
            const category = this.dataset.category;
            const isChecked = this.checked;
            
            // Mettre à jour toutes les features de cette catégorie
            const categoryFeatures = kmlOverlay.querySelectorAll(`[data-category="${category}"]`);
            categoryFeatures.forEach(checkbox => {
                if (checkbox !== this) { // Ne pas se mettre à jour soi-même
                    checkbox.checked = isChecked;
                    if (checkbox.classList.contains('kml-feature-checkbox') ||
                        checkbox.classList.contains('kml-metadata-checkbox')) {
                        toggleKmlLayer(checkbox);
                    }
                }
            });
            
            updateMasterCheckboxState();
        });
    });
    
    // Gestionnaires pour les cases individuelles (features et métadonnées)
    [...featureCheckboxes, ...metadataCheckboxes].forEach(checkbox => {
        checkbox.addEventListener('change', function() {
            toggleKmlLayer(this);
            
            // Mettre à jour l'état de la catégorie parent
            const category = this.dataset.category;
            if (category) {
                updateCategoryCheckboxState(category);
            }
            
            updateMasterCheckboxState();
        });
    });
    
    // Fonction pour mettre à jour l'état d'une case de catégorie
    function updateCategoryCheckboxState(category) {
        const categoryCheckbox = kmlOverlay.querySelector(`[data-category="${category}"].kml-category-checkbox`);
        if (!categoryCheckbox) return;
        
        const categoryItems = kmlOverlay.querySelectorAll(`[data-category="${category}"]:not(.kml-category-checkbox)`);
        const checkedItems = Array.from(categoryItems).filter(cb => cb.checked);
        
        if (checkedItems.length === 0) {
            categoryCheckbox.checked = false;
            categoryCheckbox.indeterminate = false;
        } else if (checkedItems.length === categoryItems.length) {
            categoryCheckbox.checked = true;
            categoryCheckbox.indeterminate = false;
        } else {
            categoryCheckbox.checked = false;
            categoryCheckbox.indeterminate = true;
        }
    }
    
    // Fonction pour mettre à jour l'état de la case parent (master)
    function updateMasterCheckboxState() {
        if (!masterCheckbox) return;
        
        const allItems = [...featureCheckboxes, ...metadataCheckboxes];
        const checkedItems = allItems.filter(cb => cb.checked);
        
        if (checkedItems.length === 0) {
            masterCheckbox.checked = false;
            masterCheckbox.indeterminate = false;
        } else if (checkedItems.length === allItems.length) {
            masterCheckbox.checked = true;
            masterCheckbox.indeterminate = false;
        } else {
            masterCheckbox.checked = false;
            masterCheckbox.indeterminate = true;
        }
    }
}

// Fonction pour basculer l'état collapse/expand d'une catégorie
function toggleCategoryCollapse(categoryName) {
    const categoryContent = kmlOverlay.querySelector(`.category-content[data-category="${categoryName}"]`);
    const toggleIcon = kmlOverlay.querySelector(`[onclick="toggleCategoryCollapse('${categoryName}')"]`);
    
    if (!categoryContent || !toggleIcon) return;
    
    const isCollapsed = categoryContent.style.maxHeight === '0px' || categoryContent.style.display === 'none';
    
    if (isCollapsed) {
        // Développer la catégorie
        categoryContent.style.display = 'block';
        categoryContent.style.maxHeight = categoryContent.scrollHeight + 'px';
        categoryContent.style.opacity = '1';
        toggleIcon.style.transform = 'rotate(0deg)';
        toggleIcon.classList.remove('fa-chevron-right');
        toggleIcon.classList.add('fa-chevron-down');
    } else {
        // Réduire la catégorie
        categoryContent.style.maxHeight = '0px';
        categoryContent.style.opacity = '0';
        toggleIcon.style.transform = 'rotate(-90deg)';
        toggleIcon.classList.remove('fa-chevron-down');
        toggleIcon.classList.add('fa-chevron-right');
        
        // Masquer complètement après l'animation
        setTimeout(() => {
            if (categoryContent.style.maxHeight === '0px') {
                categoryContent.style.display = 'none';
            }
        }, 300);
    }
}

// Fonction pour initialiser l'état des catégories (toutes développées par défaut)
function initializeCategoryStates() {
    if (!kmlOverlay) return;
    
    const categoryContents = kmlOverlay.querySelectorAll('.category-content');
    categoryContents.forEach(content => {
        // Définir la hauteur maximale pour l'animation
        content.style.maxHeight = content.scrollHeight + 'px';
        content.style.opacity = '1';
        content.style.display = 'block';
    });
    
    // S'assurer que toutes les icônes sont dans le bon état
    const toggleIcons = kmlOverlay.querySelectorAll('.category-toggle-icon');
    toggleIcons.forEach(icon => {
        icon.style.transform = 'rotate(0deg)';
        icon.classList.remove('fa-chevron-right');
        icon.classList.add('fa-chevron-down');
    });
}

// Fonction pour développer/réduire toutes les catégories
function toggleAllCategories() {
    if (!kmlOverlay) return;
    
    const toggleButton = kmlOverlay.querySelector('#toggleAllCategories');
    const categoryContents = kmlOverlay.querySelectorAll('.category-content');
    const toggleIcons = kmlOverlay.querySelectorAll('.category-toggle-icon');
    
    // Vérifier si au moins une catégorie est développée
    const hasExpandedCategory = Array.from(categoryContents).some(content =>
        content.style.maxHeight !== '0px' && content.style.display !== 'none'
    );
    
    if (hasExpandedCategory) {
        // Réduire toutes les catégories
        categoryContents.forEach(content => {
            content.style.maxHeight = '0px';
            content.style.opacity = '0';
            setTimeout(() => {
                if (content.style.maxHeight === '0px') {
                    content.style.display = 'none';
                }
            }, 300);
        });
        
        toggleIcons.forEach(icon => {
            icon.style.transform = 'rotate(-90deg)';
            icon.classList.remove('fa-chevron-down');
            icon.classList.add('fa-chevron-right');
        });
        
        // Changer l'icône du bouton principal
        toggleButton.classList.remove('fa-compress-alt');
        toggleButton.classList.add('fa-expand-alt');
        toggleButton.title = 'Développer tout';
        
    } else {
        // Développer toutes les catégories
        categoryContents.forEach(content => {
            content.style.display = 'block';
            content.style.maxHeight = content.scrollHeight + 'px';
            content.style.opacity = '1';
        });
        
        toggleIcons.forEach(icon => {
            icon.style.transform = 'rotate(0deg)';
            icon.classList.remove('fa-chevron-right');
            icon.classList.add('fa-chevron-down');
        });
        
        // Changer l'icône du bouton principal
        toggleButton.classList.remove('fa-expand-alt');
        toggleButton.classList.add('fa-compress-alt');
        toggleButton.title = 'Réduire tout';
    }
}

// Fonctions utilitaires pour l'overlay KML

// Organise les features par dossier KML (Folders)
function organizeFeaturesByCategory(features, metadata) {
    const categories = {};
    
    // Si on a des informations sur les dossiers dans les métadonnées, les utiliser
    if (metadata && metadata.folders && metadata.folders.length > 0) {
        metadata.folders.forEach(folder => {
            categories[folder.name] = [];
        });
    } else {
        // Sinon, utiliser les catégories par défaut basées sur le sample.kml
        categories['Placemarks'] = [];
        categories['Styles and Markup'] = [];
        categories['Ground Overlays'] = [];
        categories['Screen Overlays'] = [];
        categories['Paths'] = [];
        categories['Polygons'] = [];
    }
    
    features.forEach((feature, index) => {
        // Ajouter l'index original pour le mapping
        feature.originalIndex = index;
        
        // Essayer de déterminer le dossier d'origine de la feature
        let targetCategory = null;
        
        // Si la feature a une information de dossier, l'utiliser
        if (feature.folder) {
            targetCategory = feature.folder;
        } else {
            // Sinon, catégoriser par type
            switch (feature.type) {
                case 'marker':
                    targetCategory = 'Placemarks';
                    break;
                case 'polyline':
                case 'track':
                case 'multitrack':
                    targetCategory = 'Paths';
                    break;
                case 'polygon':
                    targetCategory = 'Polygons';
                    break;
                case 'ground_overlay':
                    targetCategory = 'Ground Overlays';
                    break;
                case 'screen_overlay':
                    targetCategory = 'Screen Overlays';
                    break;
                default:
                    targetCategory = 'Placemarks';
                    break;
            }
        }
        
        // Ajouter la feature à la catégorie appropriée
        if (categories[targetCategory]) {
            categories[targetCategory].push(feature);
        } else {
            // Si la catégorie n'existe pas, la créer
            categories[targetCategory] = [feature];
        }
    });
    
    return categories;
}

// Retourne les informations d'affichage pour chaque catégorie
function getCategoryInfo(categoryName) {
    const categoryInfos = {
        'Placemarks': { icon: 'fa-map-marker-alt', color: '#dc3545' },
        'Paths': { icon: 'fa-route', color: '#007bff' },
        'Polygons': { icon: 'fa-draw-polygon', color: '#28a745' },
        'Ground Overlays': { icon: 'fa-image', color: '#fd7e14' },
        'Screen Overlays': { icon: 'fa-desktop', color: '#6f42c1' },
        'Styles and Markup': { icon: 'fa-palette', color: '#e83e8c' }
    };
    
    return categoryInfos[categoryName] || { icon: 'fa-question', color: '#6c757d' };
}

// Retourne l'icône appropriée pour un type de feature
function getFeatureIcon(featureType) {
    const icons = {
        'marker': 'fa-map-marker-alt',
        'polyline': 'fa-route',
        'track': 'fa-route',
        'multitrack': 'fa-route',
        'polygon': 'fa-draw-polygon'
    };
    
    return icons[featureType] || 'fa-circle';
}

// Retourne la couleur appropriée pour une feature
function getFeatureColor(feature) {
    switch (feature.type) {
        case 'marker':
            return feature.is_annotation ? '#ff6b6b' : '#4ecdc4';
        case 'polyline':
        case 'track':
        case 'multitrack':
            return '#667eea';
        case 'polygon':
            return '#28a745';
        default:
            return '#6c757d';
    }
}

// Extrait les métadonnées affichables
function getDisplayableMetadata(metadata) {
    const displayableItems = [];
    
    // Métadonnées principales à afficher
    const mainFields = {
        'title': 'Titre',
        'description': 'Description',
        'author': 'Auteur'
    };
    
    Object.keys(mainFields).forEach(key => {
        if (metadata[key]) {
            displayableItems.push({
                name: mainFields[key],
                value: metadata[key],
                key: key
            });
        }
    });
    
    // Informations sur les dossiers
    if (metadata.folders && metadata.folders.length > 0) {
        displayableItems.push({
            name: 'Dossiers',
            value: `${metadata.folders.length} dossier(s)`,
            key: 'folders'
        });
    }
    
    // Informations sur les styles
    if (metadata.styles && Object.keys(metadata.styles).length > 0) {
        displayableItems.push({
            name: 'Styles',
            value: `${Object.keys(metadata.styles).length} style(s)`,
            key: 'styles'
        });
    }
    
    // Extensions Google
    if (metadata.google_extensions) {
        const ext = metadata.google_extensions;
        if (ext.tours && ext.tours.length > 0) {
            displayableItems.push({
                name: 'Tours Google',
                value: `${ext.tours.length} tour(s)`,
                key: 'google_tours'
            });
        }
        if (ext.track_count) {
            displayableItems.push({
                name: 'Traces Google',
                value: `${ext.track_count} trace(s)`,
                key: 'google_tracks'
            });
        }
    }
    
    return displayableItems;
}

// Basculer l'affichage d'une couche KML
function toggleKmlLayer(checkbox) {
    const layerId = checkbox.dataset.layerId;
    const metaId = checkbox.dataset.metaId;
    
    if (layerId) {
        // Gérer les features
        const layer = kmlLayers.get(layerId);
        if (layer) {
            if (checkbox.checked) {
                if (!currentKmlLayer.hasLayer(layer)) {
                    currentKmlLayer.addLayer(layer);
                }
            } else {
                if (currentKmlLayer.hasLayer(layer)) {
                    currentKmlLayer.removeLayer(layer);
                }
            }
        }
    } else if (metaId) {
        // Gérer les métadonnées (pour l'instant, juste un log)
        // Les métadonnées pourraient être affichées dans un panneau d'information
        console.log(`Métadonnée ${metaId} ${checkbox.checked ? 'affichée' : 'masquée'}`);
        
        // Ici on pourrait implémenter l'affichage/masquage des métadonnées
        // par exemple dans un panneau d'information ou comme overlay sur la carte
        toggleMetadataDisplay(metaId, checkbox.checked);
    }
}

// Afficher/masquer les métadonnées
function toggleMetadataDisplay(metaId, show) {
    // Cette fonction peut être étendue pour afficher les métadonnées
    // dans un panneau d'information ou comme overlay sur la carte
    
    if (!kmlData || !kmlData.metadata) return;
    
    const metaIndex = parseInt(metaId.replace('meta_', ''));
    const metadata = kmlData.metadata[metaIndex];
    
    if (!metadata) return;
    
    // Pour l'instant, on affiche juste une notification
    if (show) {
        console.log(`Affichage de la métadonnée: ${metadata.name} = ${metadata.value}`);
    } else {
        console.log(`Masquage de la métadonnée: ${metadata.name}`);
    }
}

// Supprimer l'overlay KML
function removeKmlOverlay() {
    if (kmlOverlay && kmlOverlay.parentNode) {
        kmlOverlay.parentNode.removeChild(kmlOverlay);
        kmlOverlay = null;
    }
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

// Supprimer les screen overlays
function removeScreenOverlays() {
    if (window.screenOverlays) {
        window.screenOverlays.forEach(overlay => {
            if (overlay.parentNode) {
                overlay.parentNode.removeChild(overlay);
            }
        });
        window.screenOverlays = [];
    }
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
    
    // Nettoyer l'overlay au chargement de la page
    removeKmlOverlay();
});
// Fonctions pour gérer les styles et couleurs KML
function getFeatureStyle(feature, metadata) {
    console.log('🎨 getFeatureStyle - Feature:', feature.name, 'Style ref:', feature.style);
    console.log('🎨 getFeatureStyle - Metadata styles:', metadata?.styles);
    
    if (!feature.style || !metadata || !metadata.styles) {
        console.log('🎨 getFeatureStyle - Utilisation du style par défaut (pas de style ou métadonnées)');
        return getDefaultStyle(feature.type);
    }
    
    // Nettoyer la référence de style (enlever le #)
    const styleId = feature.style.replace('#', '');
    const style = metadata.styles[styleId];
    
    console.log('🎨 getFeatureStyle - Style ID:', styleId, 'Style trouvé:', style);
    
    if (!style) {
        console.log('🎨 getFeatureStyle - Style non trouvé, utilisation du style par défaut');
        return getDefaultStyle(feature.type);
    }
    
    const parsedStyle = {
        color: parseKmlColor(style.line_color || style.poly_color),
        fillColor: parseKmlColor(style.poly_color),
        lineColor: parseKmlColor(style.line_color),
        width: style.line_width ? parseInt(style.line_width) : 2,
        icon: style.icon
    };
    
    console.log('🎨 getFeatureStyle - Style parsé:', parsedStyle);
    return parsedStyle;
}

function getDefaultStyle(featureType) {
    const defaults = {
        'marker': { color: '#4ecdc4', fillColor: '#4ecdc4', lineColor: '#4ecdc4', width: 2 },
        'polyline': { color: '#667eea', fillColor: '#667eea', lineColor: '#667eea', width: 2 },
        'track': { color: '#667eea', fillColor: '#667eea', lineColor: '#667eea', width: 2 },
        'multitrack': { color: '#667eea', fillColor: '#667eea', lineColor: '#667eea', width: 2 },
        'polygon': { color: '#28a745', fillColor: '#28a745', lineColor: '#28a745', width: 2 }
    };
    
    return defaults[featureType] || defaults['marker'];
}

function parseKmlColor(kmlColor) {
    console.log('🌈 parseKmlColor - Couleur KML reçue:', kmlColor);
    
    if (!kmlColor) {
        console.log('🌈 parseKmlColor - Pas de couleur, retour par défaut #666666');
        return '#666666';
    }
    
    // Format KML: aabbggrr (alpha, blue, green, red)
    if (kmlColor.length === 8) {
        const alpha = kmlColor.substring(0, 2);
        const blue = kmlColor.substring(2, 4);
        const green = kmlColor.substring(4, 6);
        const red = kmlColor.substring(6, 8);
        
        // Convertir en format CSS #rrggbb
        const cssColor = `#${red}${green}${blue}`;
        console.log('🌈 parseKmlColor - Conversion:', kmlColor, '->', cssColor);
        return cssColor;
    }
    
    const result = kmlColor.startsWith('#') ? kmlColor : `#${kmlColor}`;
    console.log('🌈 parseKmlColor - Couleur directe:', kmlColor, '->', result);
    return result;
}

function createColorPreview(feature, metadata) {
    const style = getFeatureStyle(feature, metadata);
    
    switch (feature.type) {
        case 'polygon':
            return `<div style="width: 12px; height: 12px; background-color: ${style.fillColor}; border: 1px solid ${style.lineColor}; display: inline-block; margin-right: 6px;"></div>`;
        
        case 'polyline':
        case 'track':
        case 'multitrack':
            return `<div style="width: 16px; height: 3px; background-color: ${style.lineColor}; display: inline-block; margin-right: 6px; margin-top: 4px;"></div>`;
        
        case 'marker':
        default:
            return `<div style="width: 8px; height: 8px; background-color: ${style.color}; border-radius: 50%; border: 1px solid white; box-shadow: 0 1px 2px rgba(0,0,0,0.3); display: inline-block; margin-right: 6px;"></div>`;
    }
}

// Fonction pour dessiner les polygones sur la carte
function drawPolygonOnMap(feature, metadata) {
    console.log('🔷 drawPolygonOnMap - Feature:', feature.name, 'Type:', feature.type);
    
    if (feature.type !== 'polygon' || !feature.coordinates) {
        console.log('🔷 drawPolygonOnMap - Pas un polygone ou pas de coordonnées');
        return null;
    }
    
    const style = getFeatureStyle(feature, metadata);
    console.log('🔷 drawPolygonOnMap - Style appliqué:', style);
    let polygon = null;
    
    try {
        // Gérer différents types de structures de coordonnées pour les polygones
        let coordinates = feature.coordinates;
        
        // Vérifier si c'est un multi-polygone ou un polygone simple
        if (Array.isArray(coordinates) && coordinates.length > 0) {
            // Si le premier élément est un tableau de tableaux, c'est probablement un polygone avec des trous
            if (Array.isArray(coordinates[0]) && Array.isArray(coordinates[0][0])) {
                // Structure: [[[lat, lng], [lat, lng], ...], [[hole_lat, hole_lng], ...]]
                // Le premier tableau est le contour extérieur, les suivants sont les trous
                const outerRing = coordinates[0];
                const holes = coordinates.slice(1);
                
                // Vérifier que le contour extérieur a au moins 3 points
                if (outerRing.length >= 3) {
                    if (holes.length > 0) {
                        // Polygone avec trous
                        polygon = L.polygon([outerRing, ...holes], {
                            color: style.lineColor,
                            fillColor: style.fillColor,
                            weight: style.width,
                            opacity: 0.8,
                            fillOpacity: 0.4
                        });
                    } else {
                        // Polygone simple
                        polygon = L.polygon(outerRing, {
                            color: style.lineColor,
                            fillColor: style.fillColor,
                            weight: style.width,
                            opacity: 0.8,
                            fillOpacity: 0.4
                        });
                    }
                }
            } else if (Array.isArray(coordinates[0]) && typeof coordinates[0][0] === 'number') {
                // Structure simple: [[lat, lng], [lat, lng], ...]
                if (coordinates.length >= 3) {
                    polygon = L.polygon(coordinates, {
                        color: style.lineColor,
                        fillColor: style.fillColor,
                        weight: style.width,
                        opacity: 0.8,
                        fillOpacity: 0.4
                    });
                }
            } else {
                // Essayer de traiter comme un multi-polygone
                const validPolygons = coordinates.filter(poly =>
                    Array.isArray(poly) && poly.length >= 3
                );
                
                if (validPolygons.length > 0) {
                    polygon = L.polygon(validPolygons, {
                        color: style.lineColor,
                        fillColor: style.fillColor,
                        weight: style.width,
                        opacity: 0.8,
                        fillOpacity: 0.4
                    });
                }
            }
        }
        
        if (!polygon) {
            console.warn('Impossible de créer le polygone:', feature.name, 'Coordonnées:', coordinates);
            return null;
        }
        
        // Calculer des statistiques sur le polygone
        const area = calculatePolygonArea(feature.coordinates);
        const perimeter = calculatePolygonPerimeter(feature.coordinates);
        
        // Ajouter un popup avec les informations détaillées du polygone
        const popupContent = `
            <div class="popup-content">
                <strong>${feature.name}</strong><br>
                ${feature.description ? `<div class="mb-2">${feature.description}</div>` : ''}
                <div class="polygon-stats">
                    <small class="text-muted">
                        <i class="fas fa-draw-polygon me-1"></i>Polygone<br>
                        ${area ? `<i class="fas fa-expand-arrows-alt me-1"></i>Surface: ~${formatArea(area)}<br>` : ''}
                        ${perimeter ? `<i class="fas fa-ruler me-1"></i>Périmètre: ~${formatDistance(perimeter)}<br>` : ''}
                        <i class="fas fa-palette me-1"></i>Style: ${style.fillColor}
                    </small>
                </div>
                <div class="mt-2">
                    <button class="btn btn-sm btn-outline-primary" onclick="centerOnPolygon('${feature.name}')">
                        <i class="fas fa-crosshairs"></i> Centrer
                    </button>
                </div>
            </div>
        `;
        
        polygon.bindPopup(popupContent);
        
        // Ajouter des événements pour l'interaction
        polygon.on('mouseover', function(e) {
            this.setStyle({
                weight: style.width + 1,
                opacity: 1.0,
                fillOpacity: 0.6
            });
        });
        
        polygon.on('mouseout', function(e) {
            this.setStyle({
                weight: style.width,
                opacity: 0.8,
                fillOpacity: 0.4
            });
        });
        
        // Stocker les informations du polygone pour référence
        polygon._polygonInfo = {
            name: feature.name,
            area: area,
            perimeter: perimeter,
            coordinates: feature.coordinates
        };
        
        return polygon;
        
    } catch (error) {
        console.error('Erreur lors de la création du polygone:', feature.name, error);
        return null;
    }
}

// Fonction pour calculer approximativement l'aire d'un polygone
function calculatePolygonArea(coordinates) {
    if (!coordinates || !Array.isArray(coordinates) || coordinates.length < 3) {
        return null;
    }
    
    try {
        // Utiliser la formule de Shoelace pour calculer l'aire approximative
        let area = 0;
        let coords = coordinates;
        
        // Si c'est un polygone avec des trous, utiliser seulement le contour extérieur
        if (Array.isArray(coordinates[0]) && Array.isArray(coordinates[0][0])) {
            coords = coordinates[0];
        }
        
        if (coords.length < 3) return null;
        
        for (let i = 0; i < coords.length; i++) {
            const j = (i + 1) % coords.length;
            area += coords[i][0] * coords[j][1];
            area -= coords[j][0] * coords[i][1];
        }
        
        area = Math.abs(area) / 2;
        
        // Conversion approximative en mètres carrés (très approximative)
        // 1 degré ≈ 111 km à l'équateur
        const metersPerDegree = 111000;
        return area * metersPerDegree * metersPerDegree;
        
    } catch (error) {
        console.warn('Erreur lors du calcul de l\'aire:', error);
        return null;
    }
}

// Fonction pour calculer approximativement le périmètre d'un polygone
function calculatePolygonPerimeter(coordinates) {
    if (!coordinates || !Array.isArray(coordinates) || coordinates.length < 3) {
        return null;
    }
    
    try {
        let perimeter = 0;
        let coords = coordinates;
        
        // Si c'est un polygone avec des trous, utiliser seulement le contour extérieur
        if (Array.isArray(coordinates[0]) && Array.isArray(coordinates[0][0])) {
            coords = coordinates[0];
        }
        
        if (coords.length < 3) return null;
        
        for (let i = 0; i < coords.length; i++) {
            const j = (i + 1) % coords.length;
            const lat1 = coords[i][0];
            const lng1 = coords[i][1];
            const lat2 = coords[j][0];
            const lng2 = coords[j][1];
            
            // Distance euclidienne approximative
            const dlat = lat2 - lat1;
            const dlng = lng2 - lng1;
            const distance = Math.sqrt(dlat * dlat + dlng * dlng);
            
            perimeter += distance;
        }
        
        // Conversion approximative en mètres
        const metersPerDegree = 111000;
        return perimeter * metersPerDegree;
        
    } catch (error) {
        console.warn('Erreur lors du calcul du périmètre:', error);
        return null;
    }
}

// Fonction pour formater l'aire
function formatArea(area) {
    if (area < 1000000) {
        return `${Math.round(area)} m²`;
    } else if (area < 1000000000) {
        return `${(area / 1000000).toFixed(2)} km²`;
    } else {
        return `${(area / 1000000).toFixed(0)} km²`;
    }
}

// Fonction pour formater la distance
function formatDistance(distance) {
    if (distance < 1000) {
        return `${Math.round(distance)} m`;
    } else {
        return `${(distance / 1000).toFixed(2)} km`;
    }
}

// Fonction pour centrer la carte sur un polygone
function centerOnPolygon(polygonName) {
    if (!currentKmlLayer) return;
    
    currentKmlLayer.eachLayer(function(layer) {
        if (layer._polygonInfo && layer._polygonInfo.name === polygonName) {
            const bounds = layer.getBounds();
            map.fitBounds(bounds, { padding: [20, 20] });
            layer.openPopup();
        }
    });
}

// Fonction pour positionner les screen overlays
function positionScreenOverlay(feature, metadata) {
    if (feature.type !== 'screen_overlay') {
        return null;
    }
    
    // Les screen overlays sont positionnés par rapport à l'écran, pas à la carte
    // Ils nécessitent des coordonnées d'écran (pixels) plutôt que géographiques
    
    const overlayDiv = document.createElement('div');
    overlayDiv.className = 'screen-overlay';
    overlayDiv.style.cssText = `
        position: fixed;
        z-index: 1001;
        pointer-events: none;
    `;
    
    // Appliquer les paramètres de positionnement du KML
    if (feature.screen_xy) {
        overlayDiv.style.left = feature.screen_xy.x + 'px';
        overlayDiv.style.top = feature.screen_xy.y + 'px';
    }
    
    if (feature.size) {
        overlayDiv.style.width = feature.size.x + 'px';
        overlayDiv.style.height = feature.size.y + 'px';
    }
    
    // Ajouter le contenu (image ou texte)
    if (feature.icon) {
        const img = document.createElement('img');
        img.src = feature.icon;
        img.style.width = '100%';
        img.style.height = '100%';
        overlayDiv.appendChild(img);
    } else if (feature.name) {
        overlayDiv.innerHTML = `<div style="background: rgba(255,255,255,0.9); padding: 5px; border-radius: 3px;">${feature.name}</div>`;
    }
    
    return overlayDiv;
}