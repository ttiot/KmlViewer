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

// Rattache les fichiers ouverts à leur activité de bibliothèque : leur analyse avancée
// peut alors être enregistrée et retrouvée. Le nom de fichier sert de clé le temps d'un
// envoi ; un fichier déjà rattaché ou modifié n'est jamais réaffecté.
function linkToLibrary(results) {
    results.filter((r) => r.activity_id && r.status !== 'error').forEach((r) => {
        Viewer.libraryIds.set(r.filename, r.activity_id);
        Viewer.files.forEach((file) => {
            if (file.name !== r.filename || file.activityId || file.edited) return;
            file.activityId = r.activity_id;
            // Un résultat calculé avant le rattachement reste celui de la session ;
            // sinon on regarde si l'activité a déjà une analyse enregistrée
            if (file.id === Viewer.activeFileId && !file.advanced) restoreAdvancedAnalysis();
        });
    });
}

// Enregistre dans la bibliothèque de l'utilisateur les traces envoyées au visualiseur (si
// la case est cochée), via le même pipeline que le dossier surveillé. Appelée par
// uploadFiles avec les fichiers eux-mêmes : aucun second écouteur ne relit le champ de
// saisie, que le premier vide pour permettre de renvoyer le même fichier.
async function saveToLibrary(files) {
    const toggle = document.getElementById('saveToLibrary');
    // Un nouvel envoi du même nom peut avoir un autre contenu : l'ancien rattachement
    // ne doit pas servir au fichier qui arrive (retiré avant toute réponse du serveur)
    files.forEach((file) => Viewer.libraryIds.delete(file.name));
    if (!toggle || !toggle.checked || files.length === 0) return;
    const form = new FormData();
    files.forEach((file) => form.append('files', file));
    try {
        const data = await App.api('/api/activities', { method: 'POST', body: form });
        const results = data.results || [];
        linkToLibrary(results);
        const created = results.filter((r) => r.status === 'created').length;
        const errors = results.filter((r) => r.status === 'error').length;
        if (created) showAlert(`${created} trace(s) enregistrée(s) dans ta bibliothèque`, 'success');
        if (errors) showAlert(`${errors} trace(s) non enregistrée(s) dans la bibliothèque`, 'warning');
    } catch (error) {
        showAlert('Enregistrement dans la bibliothèque impossible', 'warning');
    }
}

// La carte change de largeur quand la liste des points apparaît ou que la barre latérale
// se replie : Leaflet doit recalculer sa taille, sinon une bande grise reste affichée.
document.addEventListener('DOMContentLoaded', () => {
    const container = document.getElementById('map');
    if (!container || typeof ResizeObserver === 'undefined') return;
    new ResizeObserver(() => { if (Viewer.map) Viewer.map.invalidateSize(); }).observe(container);
});
