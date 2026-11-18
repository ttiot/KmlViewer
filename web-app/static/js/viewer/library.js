// Pont entre la bibliothèque et le visualiseur : /?activity=<id> charge une activité stockée.
'use strict';

document.addEventListener('DOMContentLoaded', () => {
    const id = new URLSearchParams(window.location.search).get('activity');
    if (!id || !/^\d+$/.test(id)) return;
    fetch(`/api/activities/${id}/parsed`, { credentials: 'same-origin' })
        .then((response) => response.json())
        .then((data) => {
            if (data.success) {
                addUploadedFile(data.filename || `Activité ${id}`, data);
            } else {
                showAlert('Activité introuvable', 'danger');
            }
        })
        .catch(() => showAlert("Erreur lors du chargement de l'activité", 'danger'));
});

// Les traces chargées dans le visualiseur sont aussi enregistrées dans la bibliothèque
// de l'utilisateur (si la case est cochée), via le même pipeline que le dossier surveillé.
document.addEventListener('DOMContentLoaded', () => {
    const input = document.getElementById('fileInput');
    const dropArea = document.getElementById('fileUploadArea');
    const toggle = document.getElementById('saveToLibrary');
    if (!input || !dropArea || !toggle) return;

    function save(fileList) {
        const files = Array.from(fileList || []).filter((f) => /\.(gpx|kml|kmz)$/i.test(f.name));
        if (!toggle.checked || files.length === 0) return;
        const form = new FormData();
        files.forEach((f) => form.append('files', f));
        fetch('/api/activities', { method: 'POST', body: form, credentials: 'same-origin' })
            .then((response) => response.json())
            .then((data) => {
                const results = data.results || [];
                const created = results.filter((r) => r.status === 'created').length;
                const errors = results.filter((r) => r.status === 'error').length;
                if (created) showAlert(`${created} trace(s) enregistrée(s) dans ta bibliothèque`, 'success');
                if (errors) showAlert(`${errors} trace(s) non enregistrée(s) dans la bibliothèque`, 'warning');
            })
            .catch(() => showAlert("Enregistrement dans la bibliothèque impossible", 'warning'));
    }

    input.addEventListener('change', () => save(input.files));
    dropArea.addEventListener('drop', (event) => save(event.dataTransfer && event.dataTransfer.files));
});

// La carte change de largeur quand la liste des points apparaît ou que la barre latérale
// se replie : Leaflet doit recalculer sa taille, sinon une bande grise reste affichée.
document.addEventListener('DOMContentLoaded', () => {
    const container = document.getElementById('map');
    if (!container || typeof ResizeObserver === 'undefined') return;
    new ResizeObserver(() => { if (Viewer.map) Viewer.map.invalidateSize(); }).observe(container);
});
