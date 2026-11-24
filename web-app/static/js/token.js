// Page /auth/token : lit le jeton dans le FRAGMENT de l'URL (jamais envoyé au serveur par le navigateur), l'efface de
// l'adresse, puis l'envoie dans le corps de POST /auth/token/inspect et /consume. Aucun GET ne consomme quoi que ce soit.
'use strict';

(() => {
    const csrf = document.querySelector('meta[name="csrf-token"]').content;
    const title = document.getElementById('tokenTitle');
    const message = document.getElementById('tokenMessage');
    const form = document.getElementById('tokenForm');
    const usernameRow = document.getElementById('usernameRow');
    const passwordRow = document.getElementById('passwordRow');
    const submit = document.getElementById('tokenSubmit');

    const match = /^#t=([A-Za-z0-9_-]{43})$/.exec(window.location.hash);
    const token = match ? match[1] : null;
    // Le jeton quitte la barre d'adresse tout de suite : historique, signets et captures ne le gardent pas
    history.replaceState(null, '', window.location.pathname);

    async function post(path, payload) {
        const response = await fetch(path, {
            method: 'POST', credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
            body: JSON.stringify(payload),
        });
        let data = {};
        try { data = await response.json(); } catch (_) { /* corps non JSON */ }
        return { status: response.status, data };
    }

    function show(heading, text) {
        title.textContent = heading;
        message.textContent = text;
    }

    function invalid() {
        form.classList.add('d-none');
        show('Lien invalide', 'Ce lien est invalide, expiré ou déjà utilisé.');
    }

    async function start() {
        if (!token) { invalid(); return; }
        const { status, data } = await post('/auth/token/inspect', { token });
        if (status === 429) { show('Patience', data.error || 'Trop de tentatives.'); return; }
        if (status !== 200 || !data.form) { invalid(); return; }
        if (data.form === 'verify_email') {
            show('Validation de l’adresse', 'Validation en cours…');
            await consume({});
            return;
        }
        const needsUsername = data.form === 'choose_credentials';
        usernameRow.classList.toggle('d-none', !needsUsername);
        passwordRow.classList.remove('d-none');
        show(needsUsername ? 'Crée ton compte' : 'Nouveau mot de passe',
             needsUsername ? 'Choisis un identifiant et un mot de passe.' : 'Choisis un nouveau mot de passe.');
        form.classList.remove('d-none');
        form.dataset.kind = data.form;
    }

    async function consume(fields) {
        submit.disabled = true;
        const { status, data } = await post('/auth/token/consume', { token, ...fields });
        submit.disabled = false;
        if (status === 200 && data.success) {
            show('C’est fait', 'Tu peux maintenant te connecter.');
            form.classList.add('d-none');
            window.setTimeout(() => window.location.assign(data.next || '/auth/login'), 1200);
            return;
        }
        if (data.login_required) { show('Connexion nécessaire', data.error); form.classList.add('d-none'); return; }
        if (status === 404) { invalid(); return; }
        // Erreur de saisie : le formulaire reste, le jeton n'est pas consommé
        message.textContent = data.error || 'La demande a échoué.';
    }

    form.addEventListener('submit', (event) => {
        event.preventDefault();
        const fields = { password: document.getElementById('tokenPassword').value };
        if (form.dataset.kind === 'choose_credentials') {
            fields.username = document.getElementById('tokenUsername').value.trim().toLowerCase();
        }
        consume(fields);
    });

    start().catch(() => show('Erreur', 'Impossible de contacter le serveur.'));
})();
