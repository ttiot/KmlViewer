// Visualiseur : fichiers ouverts. Chargement (envoi, exemples), onglets, fichier actif,
// visibilité sur la carte et fermeture. L'interface d'ouverture (menu, état vide,
// glisser-déposer) est dans opener.js : elle passe toujours par uploadFiles.
'use strict';

const VIEWER_FILE_PATTERN = /\.(kml|kmz|gpx)$/i;

function setFileVisibility(fileId, visible) {
    const file = Viewer.files.get(fileId);
    if (!file) return;
    file.visible = visible;
    if (visible) Viewer.map.addLayer(file.layer); else Viewer.map.removeLayer(file.layer);
    file.tab.classList.toggle('is-hidden', !visible);
    const eye = file.tab.querySelector('.file-tab-eye');
    eye.setAttribute('aria-pressed', visible ? 'true' : 'false');
    eye.querySelector('i').className = visible ? 'fas fa-eye' : 'fas fa-eye-slash';
    if (fileId === Viewer.activeFileId) {
        updateSelectionMarker();
        setProfileCursor(Panel.t); // le repère du profil suit la visibilité du fichier
    }
}

// Le fichier actif alimente la liste des points, l'analyse, l'édition et l'export.
function setActiveFile(fileId, { force = false } = {}) {
    if (!Viewer.files.has(fileId)) return;
    if (Viewer.activeFileId === fileId && !force) return;
    cancelEditing();
    Viewer.activeFileId = fileId;
    Viewer.currentPointIndex = -1;
    Viewer.files.forEach((file) => {
        const active = file.id === fileId;
        file.tab.classList.toggle('active', active);
        const button = file.tab.querySelector('.nav-link');
        button.classList.toggle('active', active);
        if (active) button.setAttribute('aria-current', 'true'); else button.removeAttribute('aria-current');
        // Beaucoup de fichiers : la barre défile, l'onglet actif doit rester en vue
        if (active) file.tab.scrollIntoView({ block: 'nearest', inline: 'nearest' });
    });
    updatePanel();
    displayPointsList();
    restoreAdvancedAnalysis();
    analyzeTrajectory();
}

// Étiquette d'un fichier ouvert : son nom (rend le fichier actif), un œil (affiche ou masque sa
// couche) et une croix (ferme le fichier). Le nom vient du fichier : nœuds texte et
// attributs posés par setAttribute, jamais de HTML.
function fileTab(file) {
    const { el, icon } = App;
    return el('li', { class: 'file-tab', dataset: { fileId: file.id } },
        el('button', { type: 'button', class: 'nav-link', title: file.name, dataset: { fileId: file.id },
            onclick: () => setActiveFile(file.id) }, file.name),
        el('button', { type: 'button', class: 'file-tab-action file-tab-eye', 'aria-pressed': 'true',
            title: 'Afficher sur la carte', 'aria-label': `Afficher ${file.name} sur la carte`,
            onclick: () => setFileVisibility(file.id, !file.visible) }, icon('eye')),
        el('button', { type: 'button', class: 'file-tab-action file-tab-close',
            title: 'Fermer le fichier', 'aria-label': `Fermer ${file.name}`,
            onclick: () => closeFile(file.id) }, icon('xmark')));
}

// Ajoute un fichier parsé (réponse de l'API) à la carte et aux onglets.
function addUploadedFile(filename, data, { activate = true, fit = true } = {}) {
    const id = `file_${Viewer.fileCounter++}`;
    const file = {
        id, name: String(filename), layer: null, markers: [], tab: null, visible: true,
        activityId: data.activity_id || null,
        activity: null, edited: false, unsaved: false, revision: 0, advanced: null, computing: false,
        data: { ...data, features: data.features || [], points: data.points || [] },
    };
    Viewer.files.set(id, file);
    try {
        renderFile(file, { fit });
    } catch (error) {
        // Géométrie que Leaflet refuse : le fichier n'est pas ouvert, les suivants le seront
        if (file.layer) Viewer.map.removeLayer(file.layer);
        Viewer.files.delete(id);
        throw error;
    }
    file.tab = fileTab(file);
    document.getElementById('fileTabs').appendChild(file.tab);

    if (activate || !Viewer.activeFileId) setActiveFile(id);
    return file;
}

// Ferme un fichier : couche, onglet et état. Le fichier actif cède la place au dernier
// fichier encore ouvert ; s'il n'en reste aucun, l'écran revient à l'état vide.
function closeFile(fileId) {
    const file = Viewer.files.get(fileId);
    if (!file) return;
    if (file.unsaved && !window.confirm(
        `Fermer « ${file.name} » ? Ses modifications non exportées seront perdues.`)) return;
    const wasActive = fileId === Viewer.activeFileId;
    if (wasActive) {
        cancelEditing();
        Viewer.map.closePopup();
    }
    if (file.layer) Viewer.map.removeLayer(file.layer);
    file.tab.remove();
    Viewer.files.delete(fileId);

    let next = wasActive ? null : Viewer.files.get(Viewer.activeFileId);
    if (wasActive) {
        Viewer.activeFileId = null;
        Viewer.currentPointIndex = -1;
        next = Array.from(Viewer.files.values()).pop() || null;
        if (next) {
            setActiveFile(next.id);
        } else {
            updatePanel();
            displayPointsList();
            restoreAdvancedAnalysis();
            analyzeTrajectory();
        }
    }
    // Le bouton qui avait le focus vient de disparaître : on le rend à un voisin
    const focusTarget = next ? next.tab.querySelector('.nav-link') : document.getElementById('openMenuButton');
    if (focusTarget) focusTarget.focus();
}

// Envoie des fichiers au serveur et ouvre ceux qu'il a su lire. Seul chemin d'envoi :
// le menu « Ouvrir », l'état vide et le glisser-déposer y aboutissent tous.
async function uploadFiles(fileList) {
    const files = Array.from(fileList || []).filter((file) => VIEWER_FILE_PATTERN.test(file.name));
    if (files.length === 0) {
        showAlert('Aucun fichier valide : formats acceptés .kml, .kmz et .gpx', 'warning');
        return;
    }
    const form = new FormData();
    files.forEach((file) => form.append('files', file));
    showAlert(`Traitement de ${files.length} fichier(s)…`, 'info', { spinner: true });
    // En parallèle : l'affichage n'attend pas l'enregistrement en bibliothèque
    const saved = saveToLibrary(files);

    try {
        const data = await App.api('/api/multi-upload', { method: 'POST', body: form });
        const results = Array.isArray(data.files) ? data.files : [];
        let first = true;
        // Fichier ouvert à chaque position de l'envoi (null si refusé) : c'est par cette
        // position que le fichier sera rattaché à son activité de bibliothèque
        const opened = results.map((result) => {
            if (!result || !result.success) {
                showAlert(`${(result && result.filename) || 'Fichier'} : ${(result && result.error) || 'illisible'}`, 'danger');
                return null;
            }
            try {
                const file = addUploadedFile(result.filename, result, { activate: first });
                first = false;
                showAlert(`Fichier ${result.filename} chargé`, 'success');
                return file;
            } catch (error) {
                console.error('Affichage impossible :', error);
                showAlert(`${result.filename || 'Fichier'} : affichage impossible`, 'danger');
                return null;
            }
        });
        linkToLibrary(files.length, results, opened, await saved);
    } catch (error) {
        showAlert(`Erreur lors de l'envoi : ${error.message}`, 'danger');
    }
}

async function loadSampleFile(filename) {
    showAlert(`Chargement de ${filename}…`, 'info', { spinner: true });
    try {
        const data = await App.api(
            `/api/load-sample/${encodeURIComponent(filename)}`);
        if (data.success) addUploadedFile(filename, data);
        else showAlert(`Erreur : ${data.error || 'fichier illisible'}`, 'danger');
    } catch (error) {
        showAlert(`Erreur lors du chargement : ${error.message}`, 'danger');
    }
}
