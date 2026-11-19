// Visualiseur : édition des points, simplification des traces et export.
// L'édition porte sur les données du fichier actif, en mémoire dans le navigateur ;
// l'export envoie ces données au serveur, qui les sérialise.
'use strict';

const Editor = {
    mode: null,          // 'add' | 'edit' | 'delete' | null
    target: null,        // { index } pour une modification, { latlng } pour un ajout
    mapClick: null,      // écouteur de carte en attente (mode ajout)
};

const EDITOR_MODES = {
    add: { button: 'addPointBtn', cursor: 'crosshair', hint: 'Clique sur la carte pour placer le nouveau point.' },
    edit: { button: 'editPointBtn', cursor: 'pointer', hint: 'Clique sur un point (carte ou liste) pour le modifier.' },
    delete: { button: 'deletePointBtn', cursor: 'not-allowed', hint: 'Clique sur un point (carte ou liste) pour le supprimer.' },
};

function setEditorHint(text) {
    const hint = document.getElementById('editorHint');
    hint.textContent = text || '';
    hint.hidden = !text;
}

function closePointForm() {
    setVisible('pointEditForm', false);
    Editor.target = null;
}

// Quitte le mode d'édition en cours et referme le formulaire.
function cancelEditing() {
    if (Editor.mapClick && Viewer.map) Viewer.map.off('click', Editor.mapClick);
    Editor.mapClick = null;
    Editor.mode = null;
    if (Viewer.map) Viewer.map.getContainer().style.cursor = '';
    Object.values(EDITOR_MODES).forEach(({ button }) => {
        const node = document.getElementById(button);
        if (node) node.classList.remove('active');
    });
    if (document.getElementById('editorHint')) setEditorHint('');
    if (document.getElementById('pointEditForm')) closePointForm();
}

function setEditorMode(mode) {
    const wasActive = Editor.mode === mode;
    cancelEditing();
    if (wasActive) return; // second clic sur le bouton : on sort du mode
    if (!Viewer.activeFile()) {
        showAlert('Charge un fichier avant de modifier ses points', 'warning');
        return;
    }
    if (mode !== 'add' && Viewer.points().length === 0) {
        showAlert('Ce fichier ne contient aucun point', 'warning');
        return;
    }
    const config = EDITOR_MODES[mode];
    Editor.mode = mode;
    document.getElementById(config.button).classList.add('active');
    Viewer.map.getContainer().style.cursor = config.cursor;
    setEditorHint(config.hint);
    if (mode === 'add') {
        Editor.mapClick = (event) => {
            Editor.mapClick = null;
            openPointForm({ latlng: event.latlng });
        };
        Viewer.map.once('click', Editor.mapClick);
    }
}

// Description d'un point telle qu'on la saisit : une ligne par <br>, sans balises.
function editableDescription(description) {
    return String(description || '').split(/<br\s*\/?>/i).map((part) => plainText(part).trim())
        .filter(Boolean).join('\n');
}

function openPointForm(target) {
    const points = Viewer.points();
    const point = target.index !== undefined ? points[target.index] : null;
    Editor.target = target;
    document.getElementById('pointEditTitle').textContent = point ? 'Modifier le point' : 'Nouveau point';
    document.getElementById('editPointName').value = point ? (point.name || '') : `Nouveau point ${points.length + 1}`;
    document.getElementById('editPointDescription').value = point ? editableDescription(point.description) : '';
    setVisible('pointEditForm', true);
    Viewer.map.getContainer().style.cursor = '';
    setEditorHint(point ? '' : 'Complète le formulaire puis enregistre le point.');
    document.getElementById('editPointName').focus();
}

// Feature « marker » correspondant à points[index] : le k-ième marqueur des features.
function markerFeatureAt(features, index) {
    let count = -1;
    return features.findIndex((feature) => feature.type === 'marker' && ++count === index);
}

// Redessine un fichier après une modification de ses données. Le fichier est passé par
// l'appelant : après une opération asynchrone (simplification), le fichier modifié n'est
// plus forcément le fichier actif, et c'est bien lui qu'il faut marquer et redessiner.
function refreshFile(file) {
    if (!file || !Viewer.files.has(file.id)) return; // fermé entre-temps
    file.data.total_points = file.data.points.length;
    // La trace ne correspond plus au fichier d'origine : l'analyse avancée affichée est
    // périmée, et celle enregistrée en bibliothèque ne décrit plus ce qui est à l'écran.
    file.edited = true;
    file.unsaved = true;
    file.revision += 1;
    file.advanced = null;
    file.tab.classList.add('is-unsaved');
    renderFile(file);
    if (file.id !== Viewer.activeFileId) return;
    displayPointsList();
    restoreAdvancedAnalysis();
    analyzeTrajectory();
}

function savePointForm(event) {
    event.preventDefault();
    const file = Viewer.activeFile();
    const target = Editor.target;
    if (!file || !target) return;
    const name = document.getElementById('editPointName').value.trim().slice(0, 200);
    const text = document.getElementById('editPointDescription').value.trim().slice(0, 2000);
    if (!name) {
        showAlert('Donne un nom au point', 'warning');
        return;
    }
    // La liste affiche une ligne par <br> ; le texte saisi reste du texte à l'affichage
    const description = text.split('\n').map((line) => line.trim()).filter(Boolean).join('<br>');
    const { features, points } = file.data;

    if (target.index !== undefined) {
        const point = points[target.index];
        const featureIndex = markerFeatureAt(features, target.index);
        if (!point) return;
        // Description réellement changée : elle sera affichée même pour un point de mesure,
        // dont la description est sinon composée à partir de ses mesures (pointDescription)
        if (text !== editableDescription(point.description)) point.description_edited = true;
        Object.assign(point, { name, description, raw_description: text });
        if (featureIndex !== -1) {
            Object.assign(features[featureIndex], { name, description: text });
        }
        showAlert('Point modifié', 'success');
    } else {
        const lat = Number(target.latlng.lat.toFixed(6));
        const lon = Number(target.latlng.lng.toFixed(6));
        const base = {
            type: 'marker', name, coordinates: [lat, lon], altitude: 0, style: null, visibility: '1',
            time_info: {}, properties: {},
            parsed_info: { speed_kmh: null, speed_kts: null, altitude_ft: null, altitude_m: null,
                latitude: lat, longitude: lon, heading: null, raw_description: text },
        };
        features.push({ ...base, description: text });
        points.push({ ...base, description, raw_description: text, index: points.length });
        showAlert('Point ajouté', 'success');
    }
    cancelEditing();
    refreshFile(file);
    if (target.index === undefined) selectPoint(points.length - 1);
}

function deletePoint(index) {
    const file = Viewer.activeFile();
    const point = file && file.data.points[index];
    if (!point) return;
    if (!window.confirm(`Supprimer le point « ${point.name || index + 1} » ?`)) return;
    const { features, points } = file.data;
    const featureIndex = markerFeatureAt(features, index);
    points.splice(index, 1);
    if (featureIndex !== -1) features.splice(featureIndex, 1);
    points.forEach((item, position) => { item.index = position; });
    Viewer.currentPointIndex = -1;
    cancelEditing();
    refreshFile(file);
    showAlert('Point supprimé', 'success');
}

// Appelé au clic sur un point (carte ou liste). Retourne true si l'éditeur a pris le clic.
function handleEditorPointClick(index) {
    if (Editor.mode === 'edit') {
        selectPoint(index);
        openPointForm({ index });
        return true;
    }
    if (Editor.mode === 'delete') {
        deletePoint(index);
        return true;
    }
    return false;
}

// Simplifie toutes les traces du fichier actif (Douglas-Peucker côté serveur).
async function simplifyActiveTraces() {
    const file = Viewer.activeFile();
    const lines = file ? file.data.features.filter((feature) =>
        ['polyline', 'track'].includes(feature.type) && Array.isArray(feature.coordinates) && feature.coordinates.length >= 3) : [];
    if (lines.length === 0) {
        showAlert('Aucune trace à simplifier', 'warning');
        return;
    }
    const meters = Number(document.getElementById('simplifyTolerance').value);
    if (!Number.isFinite(meters) || meters <= 0 || meters > 1000) {
        showAlert('Tolérance invalide : entre 1 et 1000 m', 'warning');
        return;
    }
    cancelEditing();
    showAlert('Simplification en cours…', 'info', { spinner: true });
    let before = 0, after = 0;
    try {
        for (const feature of lines) {
            const data = await App.api('/api/editor/simplify-trace', {
                method: 'POST', body: { coordinates: feature.coordinates, tolerance: meters / METERS_PER_DEGREE } });
            const count = feature.coordinates.length;
            const kept = data.kept_indices;
            // Les horodatages suivent la trace point par point : on garde les mêmes positions
            [feature.timestamps, feature.time_info && feature.time_info.timestamps].forEach((stamps, slot) => {
                if (!Array.isArray(stamps) || stamps.length !== count) return;
                const filtered = kept.map((i) => stamps[i]);
                if (slot === 0) feature.timestamps = filtered; else feature.time_info.timestamps = filtered;
            });
            feature.coordinates = data.simplified_coordinates;
            before += data.original_points;
            after += data.simplified_points;
        }
    } catch (error) {
        showAlert(`Simplification impossible : ${error.message}`, 'danger');
    }
    if (before) {
        // `file` est celui dont les traces ont été simplifiées, même si l'onglet a changé
        refreshFile(file);
        showAlert(`Trace simplifiée : ${before} → ${after} points`, 'success');
    }
}

async function exportData(format) {
    const file = Viewer.activeFile();
    if (!file || !Viewer.hasData()) {
        showAlert('Aucune donnée à exporter', 'warning');
        return;
    }
    const filename = (document.getElementById('exportFilename').value.trim() || 'trajectory')
        .replace(/[^A-Za-z0-9._-]+/g, '_');
    showAlert(`Export ${format.toUpperCase()} en cours…`, 'info', { spinner: true });
    try {
        // Gestion commune des erreurs et de la session expirée (App.request) ; la réponse
        // est un fichier, pas du JSON
        const revision = file.revision;
        const response = await App.request(`/api/export/${format}`, {
            method: 'POST',
            body: {
                filename,
                include_traces: document.getElementById('includeTraces').checked,
                metadata: { name: file.name },
                features: exportFeatures(file.data),
            },
        });
        const url = URL.createObjectURL(await response.blob());
        const link = App.el('a', { href: url, download: `${filename}.${format}` });
        document.body.appendChild(link);
        link.click();
        link.remove();
        // Révocation différée : certains navigateurs lisent l'URL après le retour de click()
        setTimeout(() => URL.revokeObjectURL(url), 1000);
        // Tout ce qui avait été modifié est maintenant dans le fichier exporté
        if (revision === file.revision) {
            file.unsaved = false;
            if (file.tab) file.tab.classList.remove('is-unsaved');
        }
        showAlert(`Export ${format.toUpperCase()} terminé`, 'success');
    } catch (error) {
        showAlert(`Export impossible : ${error.message}`, 'danger');
    }
}

// Quitter la page (fermeture, rechargement, retour au login après une session expirée)
// jetterait les modifications faites dans le navigateur : le navigateur demande confirmation
// tant qu'un fichier modifié n'a pas été exporté.
function warnBeforeLosingEdits(event) {
    if (!Array.from(Viewer.files.values()).some((file) => file.unsaved)) return;
    event.preventDefault();
    event.returnValue = '';
}

function setupEditor() {
    window.addEventListener('beforeunload', warnBeforeLosingEdits);
    Object.entries(EDITOR_MODES).forEach(([mode, { button }]) => {
        document.getElementById(button).addEventListener('click', () => setEditorMode(mode));
    });
    document.getElementById('simplifyTraceBtn').addEventListener('click', simplifyActiveTraces);
    document.getElementById('editPointForm').addEventListener('submit', savePointForm);
    document.getElementById('cancelPointEditBtn').addEventListener('click', cancelEditing);
    document.querySelectorAll('[data-export-format]').forEach((button) => {
        button.addEventListener('click', () => exportData(button.dataset.exportFormat));
    });
}
