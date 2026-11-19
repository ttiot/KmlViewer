// Visualiseur : chargement des fichiers (envoi, exemples, bibliothèque) et fichier actif.
'use strict';

const VIEWER_FILE_PATTERN = /\.(kml|kmz|gpx)$/i;

function toggleFileVisibility(fileId, visible) {
    const file = Viewer.files.get(fileId);
    if (!file) return;
    file.visible = visible;
    if (visible) Viewer.map.addLayer(file.layer); else Viewer.map.removeLayer(file.layer);
    if (fileId === Viewer.activeFileId) updateSelectionMarker();
}

// Le fichier actif alimente la liste des points, l'analyse, l'édition et l'export.
function setActiveFile(fileId, { force = false } = {}) {
    if (!Viewer.files.has(fileId)) return;
    if (Viewer.activeFileId === fileId && !force) return;
    cancelEditing();
    Viewer.activeFileId = fileId;
    Viewer.currentPointIndex = -1;
    document.querySelectorAll('#fileTabs .nav-link').forEach((button) => {
        button.classList.toggle('active', button.dataset.fileId === fileId);
    });
    displayPointsList();
    restoreAdvancedAnalysis();
    analyzeTrajectory();
}

// Ajoute un fichier parsé (réponse de l'API) à la carte, à la barre latérale et aux onglets.
function addUploadedFile(filename, data, { activate = true } = {}) {
    const { el } = App;
    const id = `file_${Viewer.fileCounter++}`;
    const file = {
        id, name: filename, layer: null, markers: [], visible: true,
        activityId: data.activity_id || Viewer.libraryIds.get(filename) || null,
        edited: false, revision: 0, advanced: null,
        data: { ...data, features: data.features || [], points: data.points || [] },
    };
    Viewer.files.set(id, file);
    renderFile(file, { fit: true });

    const checkboxId = `chk_${id}`;
    document.getElementById('uploadedFiles').appendChild(el('div', { class: 'form-check' },
        el('input', { class: 'form-check-input', type: 'checkbox', id: checkboxId, checked: true,
            onchange: (event) => toggleFileVisibility(id, event.target.checked) }),
        el('label', { class: 'form-check-label', for: checkboxId }, filename)));

    document.getElementById('fileTabs').appendChild(el('li', { class: 'nav-item' },
        el('button', { type: 'button', class: 'nav-link', dataset: { fileId: id },
            onclick: () => setActiveFile(id) }, filename)));

    if (activate || !Viewer.activeFileId) setActiveFile(id);
    return file;
}

async function uploadFiles(fileList) {
    const files = Array.from(fileList || []).filter((file) => VIEWER_FILE_PATTERN.test(file.name));
    if (files.length === 0) {
        showAlert('Aucun fichier valide : formats acceptés .kml, .kmz et .gpx', 'warning');
        return;
    }
    const form = new FormData();
    files.forEach((file) => form.append('files', file));
    showAlert(`Traitement de ${files.length} fichier(s)…`, 'info', { spinner: true });
    saveToLibrary(files); // en parallèle : l'affichage n'attend pas l'enregistrement

    try {
        const data = await App.api('/api/multi-upload', { method: 'POST', body: form });
        let first = true;
        (data.files || []).forEach((result) => {
            if (result.success) {
                addUploadedFile(result.filename, result, { activate: first });
                first = false;
                showAlert(`Fichier ${result.filename} chargé`, 'success');
            } else {
                showAlert(`${result.filename || 'Fichier'} : ${result.error || 'illisible'}`, 'danger');
            }
        });
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

async function loadSampleFiles() {
    const { el } = App;
    const container = document.getElementById('sampleFiles');
    try {
        const data = await App.api('/api/sample-files');
        container.replaceChildren(...(data.files || []).map((file) => el('button', {
            type: 'button', class: 'btn btn-outline-primary btn-sm sample-file-btn', title: file.name,
            'data-full-name': file.name, onclick: () => loadSampleFile(file.name),
        }, file.name.length > 25 ? `${file.name.substring(0, 22)}...` : file.name)));
    } catch (error) {
        console.error("Chargement des fichiers d'exemple impossible :", error);
    }
}

function setupFileHandling() {
    const area = document.getElementById('fileUploadArea');
    const input = document.getElementById('fileInput');

    area.addEventListener('click', () => input.click());
    input.addEventListener('change', () => {
        if (input.files.length > 0) uploadFiles(input.files);
        input.value = ''; // permet de recharger le même fichier
    });
    area.addEventListener('dragover', (event) => {
        event.preventDefault();
        area.classList.add('dragover');
    });
    area.addEventListener('dragleave', () => area.classList.remove('dragover'));
    area.addEventListener('drop', (event) => {
        event.preventDefault();
        area.classList.remove('dragover');
        if (event.dataTransfer.files.length > 0) uploadFiles(event.dataTransfer.files);
    });

    document.querySelectorAll('input[name="displayMode"]').forEach((radio) => {
        // Les descriptions sont composées à l'affichage : liste et popups suivent aussitôt
        radio.addEventListener('change', () => {
            if (Viewer.map) Viewer.map.closePopup();
            displayPointsList();
        });
    });
}

function setupSidebar() {
    const sidebar = document.getElementById('sidebar');
    const toggle = document.getElementById('sidebarToggle');
    const mainContent = document.getElementById('mainContent');
    toggle.addEventListener('click', () => {
        sidebar.classList.toggle('collapsed');
        mainContent.classList.toggle('expanded');
        toggle.querySelector('i').className = sidebar.classList.contains('collapsed')
            ? 'fas fa-chevron-right' : 'fas fa-chevron-left';
        setTimeout(() => Viewer.map && Viewer.map.invalidateSize(), 300);
    });
}
