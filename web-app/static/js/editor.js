// ===== FONCTIONS D'ÉDITION - PHASE 4 =====

// Activer le mode ajout de point
function enableAddPointMode() {
    editingMode = 'add';
    map.getContainer().style.cursor = 'crosshair';
    showAlert('<i class="fas fa-info-circle me-2"></i>Cliquez sur la carte pour ajouter un point', 'info');
    
    // Ajouter un gestionnaire de clic temporaire
    map.once('click', handleAddPoint);
}

// Activer le mode édition de point
function enableEditPointMode() {
    showAlert(
        `<i class="fas fa-exclamation-triangle me-2"></i>Erreur: not implemented yet`,
        "danger"
    );
    // editingMode = 'edit';
    // map.getContainer().style.cursor = 'pointer';
    // showAlert('<i class="fas fa-info-circle me-2"></i>Cliquez sur un point pour l\'éditer', 'info');
}

// Activer le mode suppression de point
function enableDeletePointMode() {
    showAlert(
        `<i class="fas fa-exclamation-triangle me-2"></i>Erreur: not implemented yet`,
        "danger"
    );
    // editingMode = 'delete';
    // map.getContainer().style.cursor = 'not-allowed';
    // showAlert('<i class="fas fa-info-circle me-2"></i>Cliquez sur un point pour le supprimer', 'warning');
}

// Gérer l'ajout d'un point
function handleAddPoint(e) {
    const latlng = e.latlng;
    
    const data = {
        features: allFeatures,
        coordinates: [latlng.lat, latlng.lng, 0],
        name: `Nouveau point ${allPoints.length + 1}`,
        description: "Point ajouté manuellement",
        is_annotation: true, // Modifier en fonction de ta logique métier
    };

    fetch('/api/editor/add-point', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify(data)
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            // Ajouter le marqueur à la carte
            const marker = L.marker([latlng.lat, latlng.lng], {
                icon: L.divIcon({
                    className: 'custom-marker',
                    html: '<div style="background-color: #4ecdc4; width: 12px; height: 12px; border-radius: 50%; border: 2px solid white; box-shadow: 0 2px 4px rgba(0,0,0,0.3);"></div>',
                    iconSize: [16, 16],
                    iconAnchor: [8, 8]
                })
            });
            
            marker.bindPopup(`
                <div class="popup-content">
                    <strong>Nouveau point ${allPoints.length + 1}</strong><br>
                    Point ajouté manuellement
                </div>
            `);
            
            currentKmlLayer.addLayer(marker);
            pointMarkers.push(marker);
            
            // Ajouter aux points
            allPoints.push({
                name: `Nouveau point ${allPoints.length + 1}`,
                description: 'Point ajouté manuellement',
                coordinates: [latlng.lat, latlng.lng],
                is_annotation: false
            });
            
            displayPointsList();
            showAlert('<i class="fas fa-check me-2"></i>Point ajouté avec succès', 'success');
        } else {
            showAlert(`<i class="fas fa-exclamation-triangle me-2"></i>Erreur: ${data.error}`, 'danger');
        }
    })
    .catch(error => {
        showAlert(`<i class="fas fa-exclamation-triangle me-2"></i>Erreur: ${error.message}`, 'danger');
    });
    
    // Réinitialiser le mode
    editingMode = null;
    map.getContainer().style.cursor = '';
}

// Simplifier la trace actuelle
function simplifyCurrentTrace() {
    if (!currentKmlLayer) {
        showAlert('<i class="fas fa-exclamation-triangle me-2"></i>Aucune trace chargée', 'warning');
        return;
    }
    
    showAlert('<i class="fas fa-spinner fa-spin me-2"></i>Simplification en cours...', 'info');
    
    fetch('/api/editor/simplify-trace', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify({
            tolerance: 0.0001 // Tolérance par défaut
        })
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            showAlert(`<i class="fas fa-check me-2"></i>Trace simplifiée: ${data.original_points} → ${data.simplified_points} points`, 'success');
            // Recharger les données simplifiées
            // TODO: Implémenter le rechargement
        } else {
            showAlert(`<i class="fas fa-exclamation-triangle me-2"></i>Erreur: ${data.error}`, 'danger');
        }
    })
    .catch(error => {
        showAlert(`<i class="fas fa-exclamation-triangle me-2"></i>Erreur: ${error.message}`, 'danger');
    });
}

// Sauvegarder l'édition d'un point
function savePointEdit() {
    // TODO: Implémenter la sauvegarde
    showAlert('<i class="fas fa-check me-2"></i>Point sauvegardé', 'success');
    cancelPointEdit();
}

// Annuler l'édition d'un point
function cancelPointEdit() {
    document.getElementById('pointEditForm').style.display = 'none';
    selectedPointForEdit = null;
    editingMode = null;
    map.getContainer().style.cursor = '';
}

// ===== FONCTIONS D'EXPORT - PHASE 4 =====

// Exporter les données dans différents formats
function exportData(format) {
    if (!currentKmlLayer) {
        showAlert('<i class="fas fa-exclamation-triangle me-2"></i>Aucune donnée à exporter', 'warning');
        return;
    }
    
    const filename = document.getElementById('exportFilename').value || 'trajectory';
    const includeTraces = document.getElementById('includeTraces').checked;
    
    showAlert(`<i class="fas fa-spinner fa-spin me-2"></i>Export ${format.toUpperCase()} en cours...`, 'info');
    
    fetch(`/api/export/${format}`, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify({
            filename: filename,
            include_traces: includeTraces,
            features: allFeatures
        })
    })
    .then(response => {
        if (response.ok) {
            return response.blob();
        } else {
            throw new Error('Erreur lors de l\'export');
        }
    })
    .then(blob => {
        // Créer un lien de téléchargement
        const url = window.URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `${filename}.${format}`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        window.URL.revokeObjectURL(url);
        
        showAlert(`<i class="fas fa-check me-2"></i>Export ${format.toUpperCase()} terminé`, 'success');
    })
    .catch(error => {
        showAlert(`<i class="fas fa-exclamation-triangle me-2"></i>Erreur: ${error.message}`, 'danger');
    });
}