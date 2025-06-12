// ===== FONCTIONS D'ANALYSE DE TRAJECTOIRE - PHASE 3 =====

// Fonction principale d'analyse de trajectoire
function analyzeTrajectory(features) {
    if (!features || features.length === 0) {
        hideAnalysisPanel();
        return;
    }
    
    // Appeler l'API d'analyse
    fetch('/api/analysis/trajectory', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify({ features: features })
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            currentAnalysis = data.analysis;
            displayAnalysisResults(currentAnalysis);
            showAnalysisPanel();
        } else {
            console.error('Erreur lors de l\'analyse:', data.error);
            hideAnalysisPanel();
        }
    })
    .catch(error => {
        console.error('Erreur lors de l\'analyse:', error);
        hideAnalysisPanel();
    });
}

// Affichage des résultats d'analyse
function displayAnalysisResults(analysis) {
    // Mise à jour des statistiques de base
    updateBasicStats(analysis);
    
    // Création des graphiques
    if (analysis.elevation && analysis.elevation.elevation_profile) {
        createElevationChart(analysis.elevation.elevation_profile);
    }
    
    if (analysis.speed && analysis.speed.speed_profile) {
        createSpeedChart(analysis.speed.speed_profile);
    }
}

// Mise à jour des statistiques de base
function updateBasicStats(analysis) {
    const distance = analysis.distance || {};
    const speed = analysis.speed || {};
    const elevation = analysis.elevation || {};
    const duration = analysis.duration || {};
    
    // Distance totale
    document.getElementById('totalDistance').textContent =
        distance.total_distance_km ? `${distance.total_distance_km} km` : '-';
    
    // Durée estimée
    document.getElementById('totalDuration').textContent =
        duration.estimated_duration_minutes ? `${Math.round(duration.estimated_duration_minutes)} min` : '-';
    
    // Vitesse moyenne
    document.getElementById('avgSpeed').textContent =
        speed.avg_speed_kmh ? `${speed.avg_speed_kmh} km/h` : '-';
    
    // Vitesse maximale
    document.getElementById('maxSpeed').textContent =
        speed.max_speed_kmh ? `${speed.max_speed_kmh} km/h` : '-';
    
    // Dénivelé positif
    document.getElementById('totalAscent').textContent =
        elevation.total_ascent ? `+${elevation.total_ascent} m` : '-';
    
    // Dénivelé négatif
    document.getElementById('totalDescent').textContent =
        elevation.total_descent ? `-${elevation.total_descent} m` : '-';
    
    // Statistiques détaillées d'élévation
    document.getElementById('minElevation').textContent =
        elevation.min_elevation ? `${elevation.min_elevation} m` : '-';
    document.getElementById('maxElevation').textContent =
        elevation.max_elevation ? `${elevation.max_elevation} m` : '-';
    
    // Statistiques détaillées de vitesse
    document.getElementById('minSpeed').textContent =
        speed.min_speed_kmh ? `${speed.min_speed_kmh} km/h` : '-';
    document.getElementById('avgSpeedDetail').textContent =
        speed.avg_speed_kmh ? `${speed.avg_speed_kmh} km/h` : '-';
    document.getElementById('maxSpeedDetail').textContent =
        speed.max_speed_kmh ? `${speed.max_speed_kmh} km/h` : '-';
}

// Création du graphique d'élévation
function createElevationChart(elevationProfile) {
    const ctx = document.getElementById('elevationChart').getContext('2d');
    
    // Détruire le graphique existant s'il y en a un
    if (elevationChart) {
        elevationChart.destroy();
    }
    
    const labels = elevationProfile.map(point => (point.distance / 1000).toFixed(1));
    const data = elevationProfile.map(point => point.elevation);
    
    elevationChart = new Chart(ctx, {
        type: 'line',
        data: {
            labels: labels,
            datasets: [{
                label: 'Élévation (m)',
                data: data,
                borderColor: '#667eea',
                backgroundColor: 'rgba(102, 126, 234, 0.1)',
                fill: true,
                tension: 0.1
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: {
                    display: false
                }
            },
            scales: {
                x: {
                    title: {
                        display: true,
                        text: 'Distance (km)'
                    }
                },
                y: {
                    title: {
                        display: true,
                        text: 'Altitude (m)'
                    }
                }
            }
        }
    });
}

// Création du graphique de vitesse
function createSpeedChart(speedProfile) {
    const ctx = document.getElementById('speedChart').getContext('2d');
    
    // Détruire le graphique existant s'il y en a un
    if (speedChart) {
        speedChart.destroy();
    }
    
    const labels = speedProfile.map((point, index) => index + 1);
    const data = speedProfile.map(point => point.speed_kmh);
    
    speedChart = new Chart(ctx, {
        type: 'line',
        data: {
            labels: labels,
            datasets: [{
                label: 'Vitesse (km/h)',
                data: data,
                borderColor: '#28a745',
                backgroundColor: 'rgba(40, 167, 69, 0.1)',
                fill: true,
                tension: 0.1
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: {
                    display: false
                }
            },
            scales: {
                x: {
                    title: {
                        display: true,
                        text: 'Point'
                    }
                },
                y: {
                    title: {
                        display: true,
                        text: 'Vitesse (km/h)'
                    }
                }
            }
        }
    });
}

// Affichage du panneau d'analyse
function showAnalysisPanel() {
    const analysisPanel = document.getElementById('analysisPanel');
    const pointsList = document.getElementById('pointsList');
    
    if (analysisPanel) {
        analysisPanel.style.display = 'block';
        
        // Réorganiser les colonnes : carte 4, points 4, analyse 4
        const mapCol = document.querySelector('.col-lg-8');
        const pointsCol = document.querySelector('.col-lg-4');
        
        if (mapCol && pointsCol) {
            mapCol.className = 'col-lg-8';
            pointsCol.className = 'col-lg-4';
        }
        
        // Redimensionner la carte
        setTimeout(() => {
            if (map) {
                map.invalidateSize();
            }
        }, 300);
    }
}

// Masquage du panneau d'analyse
function hideAnalysisPanel() {
    const analysisPanel = document.getElementById('analysisPanel');
    
    if (analysisPanel) {
        analysisPanel.style.display = 'none';
        
        // Restaurer les colonnes originales : carte 8, points 4
        const mapCol = document.querySelector('.col-lg-4');
        const pointsCol = mapCol ? mapCol.nextElementSibling : null;
        
        if (mapCol && pointsCol) {
            mapCol.className = 'col-lg-8';
            pointsCol.className = 'col-lg-4';
        }
        
        // Redimensionner la carte
        setTimeout(() => {
            if (map) {
                map.invalidateSize();
            }
        }, 300);
    }
}

// ===== FONCTIONS D'ANALYSE AVANCÉE - PHASE 4 =====

// Variables pour l'analyse avancée
let currentAdvancedAnalysis = null;
let editingMode = null; // 'add', 'edit', 'delete'
let selectedPointForEdit = null;

// Lancer l'analyse avancée
function runAdvancedAnalysis() {
    if (!currentFileLayer) {
        showAlert('<i class="fas fa-exclamation-triangle me-2"></i>Aucune donnée KML chargée', 'warning');
        return;
    }
    
    showAlert('<i class="fas fa-spinner fa-spin me-2"></i>Analyse avancée en cours...', 'info');
    
    fetch('/api/analysis/advanced', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify({ features: allFeatures, points: allPoints })
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            currentAdvancedAnalysis = data.analysis;
            displayAdvancedAnalysisResults(currentAdvancedAnalysis);
            showAlert('<i class="fas fa-check me-2"></i>Analyse avancée terminée', 'success');
        } else {
            showAlert(`<i class="fas fa-exclamation-triangle me-2"></i>Erreur: ${data.error}`, 'danger');
        }
    })
    .catch(error => {
        showAlert(`<i class="fas fa-exclamation-triangle me-2"></i>Erreur 2: ${error.message}`, 'danger');
    });
}

// Afficher les résultats de l'analyse avancée
function displayAdvancedAnalysisResults(analysis) {
    document.getElementById('advancedResults').style.display = 'block';
    
    // Afficher les arrêts
    if (analysis.stops) {
        displayStops(analysis.stops);
    }
    
    // Afficher les segments
    if (analysis.segments) {
        displaySegments(analysis.segments);
    }
    
    // Afficher les zones de vitesse
    if (analysis.speed_zones) {
        displaySpeedZones(analysis.speed_zones);
    }
    
    // Afficher l'analyse du terrain
    if (analysis.terrain) {
        displayTerrainAnalysis(analysis.terrain);
    }
}

// Afficher les arrêts détectés
function displayStops(stops) {
    const stopsCount = document.getElementById('stopsCount');
    const stopsList = document.getElementById('stopsList');
    
    stopsCount.textContent = stops.length;
    
    let html = '';
    stops.forEach((stop, index) => {
        html += `
            <div class="card mb-2">
                <div class="card-body p-2">
                    <h6 class="card-title mb-1">Arrêt ${index + 1}</h6>
                    <small class="text-muted">
                        Durée: ${Math.round(stop.duration_minutes)} min<br>
                        Position: ${stop.lat.toFixed(6)}, ${stop.lng.toFixed(6)}
                    </small>
                </div>
            </div>
        `;
    });
    
    stopsList.innerHTML = html || '<p class="text-muted">Aucun arrêt détecté</p>';
}

// Afficher les segments
function displaySegments(segments) {
    const segmentsCount = document.getElementById('segmentsCount');
    const segmentsList = document.getElementById('segmentsList');
    
    segmentsCount.textContent = segments.length;
    
    let html = '';
    segments.forEach((segment, index) => {
        html += `
            <div class="card mb-2">
                <div class="card-body p-2">
                    <h6 class="card-title mb-1">Segment ${index + 1}</h6>
                    <small class="text-muted">
                        Distance: ${segment.distance_km.toFixed(2)} km<br>
                        Vitesse moy: ${segment.avg_speed_kmh.toFixed(1)} km/h<br>
                        Type: ${segment.type}
                    </small>
                </div>
            </div>
        `;
    });
    
    segmentsList.innerHTML = html || '<p class="text-muted">Aucun segment détecté</p>';
}

// Afficher les zones de vitesse
function displaySpeedZones(speedZones) {
    const speedZonesList = document.getElementById('speedZonesList');
    
    let html = '';
    if (speedZones.zones && speedZones.zones.length) {
        speedZones.zones.forEach((zone) => {
            html += `
                <div class="card mb-2">
                    <div class="card-body p-2">
                        <h6 class="card-title mb-1">
                            <span class="badge" style="background-color: ${zone.color}">${zone.type}</span>
                        </h6>
                        <small class="text-muted">
                            Average: ${zone.avg_speed.toFixed(2)} km/h<br>
                            Min: ${zone.min_speed.toFixed(1)} km/h<br>
                            Max: ${zone.max_speed.toFixed(1)} km/h
                        </small>
                    </div>
                </div>
            `;
        });
    }
    
    speedZonesList.innerHTML = html || '<p class="text-muted">Aucune zone de vitesse détectée</p>';
}

// Afficher l'analyse du terrain
function displayTerrainAnalysis(terrain) {
    const terrainAnalysis = document.getElementById('terrainAnalysis');
    
    if (!terrain) {
        terrainAnalysis.innerHTML = '<p class="text-muted">Aucune donnée d\'analyse de terrain disponible</p>';
        return;
    }
    
    let html = `
        <div class="mb-4">
            <!-- <h6 class="mb-3"><i class="fas fa-mountain me-2"></i>Analyse du terrain</h6> -->
            
            <!-- Statistiques globales de pente -->
            <div class="row g-2 mb-3">
                <div class="col-4">
                    <div class="stat-card">
                        <div class="stat-value">${terrain.avg_slope ? terrain.avg_slope.toFixed(1) : '0'}%</div>
                        <div class="stat-label">Pente moyenne</div>
                    </div>
                </div>
                <div class="col-4">
                    <div class="stat-card">
                        <div class="stat-value text-success">${terrain.max_slope ? terrain.max_slope.toFixed(1) : '0'}%</div>
                        <div class="stat-label">Pente max</div>
                    </div>
                </div>
                <div class="col-4">
                    <div class="stat-card">
                        <div class="stat-value text-danger">${terrain.min_slope ? terrain.min_slope.toFixed(1) : '0'}%</div>
                        <div class="stat-label">Pente min</div>
                    </div>
                </div>
            </div>
            
            <!-- Résumé de l'analyse du terrain -->
            ${terrain.terrain_analysis ? `
            <div class="card mb-3">
                <div class="card-body p-3">
                    <h6 class="card-title mb-2">Résumé du terrain</h6>
                    <div class="row g-2">
                        <div class="col-6">
                            <small class="text-muted">Terrain dominant:</small>
                            <div class="fw-bold text-primary">${getTerrainTypeLabel(terrain.terrain_analysis.dominant_terrain)}</div>
                        </div>
                        <div class="col-6">
                            <small class="text-muted">Points analysés:</small>
                            <div class="fw-bold">${terrain.terrain_analysis.total_points || 0}</div>
                        </div>
                    </div>
                    
                    <!-- Distribution des types de terrain -->
                    ${terrain.terrain_analysis.terrain_distribution ? `
                    <div class="mt-3">
                        <small class="text-muted d-block mb-2">Distribution du terrain:</small>
                        <div class="row g-1">
                            <div class="col-3 text-center">
                                <div class="badge bg-success">${terrain.terrain_analysis.terrain_distribution.flat || 0}</div>
                                <small class="d-block">Plat</small>
                            </div>
                            <div class="col-3 text-center">
                                <div class="badge bg-info">${terrain.terrain_analysis.terrain_distribution.gentle || 0}</div>
                                <small class="d-block">Doux</small>
                            </div>
                            <div class="col-3 text-center">
                                <div class="badge bg-warning">${terrain.terrain_analysis.terrain_distribution.moderate || 0}</div>
                                <small class="d-block">Modéré</small>
                            </div>
                            <div class="col-3 text-center">
                                <div class="badge bg-danger">${terrain.terrain_analysis.terrain_distribution.steep || 0}</div>
                                <small class="d-block">Raide</small>
                            </div>
                        </div>
                    </div>
                    ` : ''}
                </div>
            </div>
            ` : ''}
            
            <!-- Segments de pente -->
            ${terrain.slope_segments && terrain.slope_segments.length > 0 ? `
            <div class="card">
                <div class="card-body p-3">
                    <h6 class="card-title mb-3">Segments de pente (${terrain.slope_segments.length})</h6>
                    <div class="accordion" id="slopeSegmentsAccordion">
                        ${terrain.slope_segments.map((segment, index) => `
                            <div class="accordion-item">
                                <h2 class="accordion-header">
                                    <button class="accordion-button collapsed" type="button" data-bs-toggle="collapse" data-bs-target="#segment${index}">
                                        <span class="badge ${getTerrainBadgeClass(segment.type)} me-2">${getTerrainTypeLabel(segment.type)}</span>
                                        Segment ${index + 1} - ${segment.length || 0} points - Pente moy: ${segment.avg_slope ? segment.avg_slope.toFixed(1) : '0'}%
                                    </button>
                                </h2>
                                <div id="segment${index}" class="accordion-collapse collapse" data-bs-parent="#slopeSegmentsAccordion">
                                    <div class="accordion-body">
                                        <div class="row g-2 mb-3">
                                            <div class="col-4">
                                                <small class="text-muted">Type:</small>
                                                <div class="fw-bold">${getTerrainTypeLabel(segment.type)}</div>
                                            </div>
                                            <div class="col-4">
                                                <small class="text-muted">Longueur:</small>
                                                <div class="fw-bold">${segment.length || 0} points</div>
                                            </div>
                                            <div class="col-4">
                                                <small class="text-muted">Pente moyenne:</small>
                                                <div class="fw-bold">${segment.avg_slope ? segment.avg_slope.toFixed(1) : '0'}%</div>
                                            </div>
                                        </div>
                                        
                                        <!-- Détails des pentes individuelles -->
                                        ${segment.slopes && segment.slopes.length > 0 ? `
                                        <div class="table-responsive">
                                            <table class="table table-sm table-striped">
                                                <thead>
                                                    <tr>
                                                        <th>Point</th>
                                                        <th>Coordonnées</th>
                                                        <th>Altitude</th>
                                                        <th>Distance H.</th>
                                                        <th>Pente (°)</th>
                                                        <th>Pente (%)</th>
                                                    </tr>
                                                </thead>
                                                <tbody>
                                                    ${segment.slopes.slice(0, 10).map(slope => `
                                                        <tr>
                                                            <td>${slope.index || 0}</td>
                                                            <td>
                                                                ${slope.coordinates ?
                                                                    `${slope.coordinates[0].toFixed(6)}, ${slope.coordinates[1].toFixed(6)}` :
                                                                    'N/A'
                                                                }
                                                            </td>
                                                            <td>${slope.coordinates && slope.coordinates[2] ? slope.coordinates[2].toFixed(1) + 'm' : 'N/A'}</td>
                                                            <td>${slope.horizontal_distance ? slope.horizontal_distance.toFixed(1) + 'm' : 'N/A'}</td>
                                                            <td class="${slope.slope_degrees > 0 ? 'text-success' : slope.slope_degrees < 0 ? 'text-danger' : ''}">${slope.slope_degrees ? slope.slope_degrees.toFixed(1) : '0'}°</td>
                                                            <td class="${slope.slope_percent > 0 ? 'text-success' : slope.slope_percent < 0 ? 'text-danger' : ''}">${slope.slope_percent ? slope.slope_percent.toFixed(1) : '0'}%</td>
                                                        </tr>
                                                    `).join('')}
                                                    ${segment.slopes.length > 10 ? `
                                                        <tr>
                                                            <td colspan="6" class="text-center text-muted">
                                                                <small>... et ${segment.slopes.length - 10} autres points</small>
                                                            </td>
                                                        </tr>
                                                    ` : ''}
                                                </tbody>
                                            </table>
                                        </div>
                                        ` : '<p class="text-muted">Aucun détail de pente disponible</p>'}
                                    </div>
                                </div>
                            </div>
                        `).join('')}
                    </div>
                </div>
            </div>
            ` : ''}
        </div>
    `;
    
    terrainAnalysis.innerHTML = html;
}

// Fonction utilitaire pour obtenir le libellé d'un type de terrain
function getTerrainTypeLabel(type) {
    const labels = {
        'flat': 'Plat',
        'gentle': 'Doux',
        'moderate': 'Modéré',
        'steep': 'Raide'
    };
    return labels[type] || type;
}

// Fonction utilitaire pour obtenir la classe CSS d'un badge de terrain
function getTerrainBadgeClass(type) {
    const classes = {
        'flat': 'bg-success',
        'gentle': 'bg-info',
        'moderate': 'bg-warning',
        'steep': 'bg-danger'
    };
    return classes[type] || 'bg-secondary';
}

// Obtenir la couleur d'une zone de vitesse
function getSpeedZoneColor(zone) {
    const colors = {
        'Très lent': '#dc3545',
        'Lent': '#fd7e14',
        'Modéré': '#ffc107',
        'Rapide': '#28a745',
        'Très rapide': '#007bff'
    };
    return colors[zone] || '#6c757d';
}

// Effacer l'analyse avancée
function clearAdvancedAnalysis() {
    currentAdvancedAnalysis = null;
    document.getElementById('advancedResults').style.display = 'none';
    showAlert('<i class="fas fa-check me-2"></i>Analyse avancée effacée', 'info');
}