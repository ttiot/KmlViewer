// Pont entre la bibliothèque et le visualiseur : ouverture d'une activité stockée
// (/?activity=<id>), enregistrement des envois et rattachement des fichiers ouverts à
// leur activité.
'use strict';

const SAVE_TO_LIBRARY_PREF = 'kmlviewer-save-to-library';

// /?activity=<id> : charge l'activité demandée par le tableau de bord ou la page Tronçons.
async function loadLibraryActivity() {
    const id = new URLSearchParams(window.location.search).get('activity');
    if (!id || !/^\d+$/.test(id)) return;
    try {
        const data = await App.api(`/api/activities/${id}/parsed`);
        if (data.success) addUploadedFile(data.filename || `Activité ${id}`, data);
        else showAlert(data.error || 'Activité introuvable', 'danger');
    } catch (error) {
        showAlert(`Chargement de l'activité impossible : ${error.message}`, 'danger');
    }
}

// Rattache les fichiers ouverts par un envoi à leur activité de bibliothèque : leur analyse
// avancée peut alors être enregistrée et retrouvée.
// Les deux requêtes de l'envoi (/api/multi-upload et /api/activities) portent les mêmes
// fichiers dans le même ordre et répondent dans cet ordre : un fichier est rattaché par sa
// position dans le lot, jamais par son seul nom (deux fichiers ouverts peuvent le partager).
//   sent    : nombre de fichiers envoyés
//   parsed  : réponses de /api/multi-upload, par position
//   opened  : fichier ouvert à chaque position (null si refusé)
//   results : réponses de /api/activities, par position (null si rien n'a été enregistré)
function linkToLibrary(sent, parsed, opened, results) {
    if (!Array.isArray(results)) return;
    // Garde-fou : au moindre écart de longueur, l'ordre n'est plus sûr, on ne rattache rien
    if (results.length !== sent || parsed.length !== sent) return;
    results.forEach((result, position) => {
        const file = opened[position];
        if (!file || !result || !result.activity_id || result.status === 'error') return;
        // Second garde-fou : les deux réponses doivent parler du même fichier
        if (result.filename !== parsed[position].filename) return;
        // Fichier fermé, déjà rattaché ou modifié entre-temps : jamais réaffecté
        if (!Viewer.files.has(file.id) || file.activityId || file.edited) return;
        file.activityId = result.activity_id;
        // Un résultat déjà calculé, ou en cours de calcul, reste celui de la session ;
        // sinon on regarde si l'activité a déjà une analyse enregistrée
        if (file.id === Viewer.activeFileId && !file.advanced && !file.computing) restoreAdvancedAnalysis();
    });
}

// Enregistre dans la bibliothèque de l'utilisateur les traces envoyées au visualiseur (si
// la case est cochée), via le même pipeline que le dossier surveillé. Appelée par
// uploadFiles avec les fichiers eux-mêmes : aucun second écouteur ne relit le champ de
// saisie, que le premier vide pour permettre de renvoyer le même fichier.
// Retourne les résultats par position, ou null si rien n'a été enregistré.
async function saveToLibrary(files) {
    const toggle = document.getElementById('saveToLibrary');
    if (!toggle || !toggle.checked || files.length === 0) return null;
    const form = new FormData();
    files.forEach((file) => form.append('files', file));
    try {
        const data = await App.api('/api/activities', { method: 'POST', body: form });
        const results = Array.isArray(data.results) ? data.results : [];
        const created = results.filter((r) => r.status === 'created').length;
        const errors = results.filter((r) => r.status === 'error').length;
        if (created) showAlert(`${created} trace(s) enregistrée(s) dans ta bibliothèque`, 'success');
        if (errors) showAlert(`${errors} trace(s) non enregistrée(s) dans la bibliothèque`, 'warning');
        return results;
    } catch (error) {
        showAlert(`Enregistrement dans la bibliothèque impossible : ${error.message}`, 'warning');
        return null;
    }
}

function setupLibrary() {
    // Choix « Enregistrer dans ma bibliothèque » mémorisé ; coché tant que rien n'est mémorisé
    const toggle = document.getElementById('saveToLibrary');
    if (toggle) {
        const stored = Prefs.read(SAVE_TO_LIBRARY_PREF, ['1', '0']);
        if (stored) toggle.checked = stored === '1';
        toggle.addEventListener('change', () => Prefs.write(SAVE_TO_LIBRARY_PREF, toggle.checked ? '1' : '0'));
    }
    loadLibraryActivity();
}
