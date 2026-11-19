// Visualiseur : ouverture de fichiers. Menu « Ouvrir » (sélecteur de fichiers, fichiers
// d'exemple), état vide posé sur la carte et glisser-déposer sur toute la page.
// Un seul champ de fichiers et un seul chemin d'envoi : uploadFiles (files.js).
'use strict';

// L'état vide n'est affiché que tant qu'aucun fichier n'est ouvert.
function updateEmptyState() {
    const empty = document.getElementById('emptyState');
    if (empty) empty.hidden = Viewer.files.size > 0;
}

function openMenu() {
    return bootstrap.Dropdown.getOrCreateInstance(document.getElementById('openMenuButton'));
}

function openFileDialog() {
    document.getElementById('fileInput').click();
}

async function loadSampleFiles() {
    const { el } = App;
    const container = document.getElementById('sampleFiles');
    try {
        const data = await App.api('/api/sample-files');
        const files = (data.files || []).filter((file) => file && typeof file.name === 'string');
        if (files.length === 0) return; // le gabarit dit déjà « Aucun fichier d'exemple »
        container.replaceChildren(...files.map((file) => el('button', {
            type: 'button', class: 'dropdown-item sample-file-item', title: file.name,
            onclick: () => { openMenu().hide(); loadSampleFile(file.name); },
        }, file.name)));
        document.getElementById('emptySamplesButton').hidden = false;
    } catch (error) {
        console.error("Chargement des fichiers d'exemple impossible :", error);
    }
}

// Glisser-déposer sur toute la page. Pendant le survol avec des fichiers, un voile couvre
// l'écran ; il ne capte pas la souris (pointer-events: none), donc n'émet lui-même aucun
// dragenter / dragleave.
function setupPageDrop() {
    const overlay = document.getElementById('dropOverlay');
    // dragenter et dragleave se répondent élément par élément : le voile ne tombe que
    // lorsque le compteur revient à zéro, pas à chaque passage d'un élément à son voisin.
    let depth = 0;
    let watchdog = 0;
    const carriesFiles = (event) => Boolean(event.dataTransfer)
        && Array.from(event.dataTransfer.types || []).includes('Files');
    const hide = () => {
        depth = 0;
        clearTimeout(watchdog);
        overlay.hidden = true;
    };
    // Garde-fou : si un dragleave manque (sortie de la fenêtre, touche Échap), le voile
    // tombe dès que les dragover, émis en continu pendant le survol, cessent d'arriver.
    const keepAlive = () => {
        clearTimeout(watchdog);
        watchdog = setTimeout(hide, 1500);
    };

    window.addEventListener('dragenter', (event) => {
        if (!carriesFiles(event)) return;
        event.preventDefault();
        depth += 1;
        overlay.hidden = false;
        keepAlive();
    });
    window.addEventListener('dragover', (event) => {
        if (!carriesFiles(event)) return;
        // Sans preventDefault, le navigateur refuserait le dépôt (ou ouvrirait le fichier)
        event.preventDefault();
        event.dataTransfer.dropEffect = 'copy';
        if (overlay.hidden) { depth = Math.max(depth, 1); overlay.hidden = false; }
        keepAlive();
    });
    window.addEventListener('dragleave', (event) => {
        if (!carriesFiles(event)) return;
        depth = Math.max(0, depth - 1);
        if (depth === 0) hide();
    });
    window.addEventListener('drop', (event) => {
        if (!carriesFiles(event)) return;
        event.preventDefault(); // où que tombe le fichier, le navigateur ne l'ouvre pas
        hide();
        if (event.dataTransfer.files.length > 0) uploadFiles(event.dataTransfer.files);
    });
}

function setupOpening() {
    const input = document.getElementById('fileInput');
    input.addEventListener('change', () => {
        if (input.files.length > 0) uploadFiles(input.files);
        input.value = ''; // permet de recharger le même fichier
    });
    document.getElementById('chooseFilesButton').addEventListener('click', () => {
        openMenu().hide();
        openFileDialog();
    });
    document.getElementById('emptyChooseButton').addEventListener('click', openFileDialog);
    document.getElementById('emptySamplesButton').addEventListener('click', (event) => {
        // Le clic ne doit pas remonter jusqu'au document : Bootstrap y verrait un clic hors
        // du menu et le refermerait aussitôt
        event.stopPropagation();
        openMenu().show();
        const first = document.querySelector('#sampleFiles .dropdown-item');
        if (first) first.focus();
    });
    setupPageDrop();
    updateEmptyState();
    loadSampleFiles();
}
