// Page de connexion par mot de passe. Aucune donnée du serveur n'est insérée autrement que par textContent.
'use strict';

(() => {
    const csrf = document.querySelector('meta[name="csrf-token"]').content;
    const card = document.getElementById('loginCard');
    const form = document.getElementById('loginForm');
    const message = document.getElementById('loginMessage');
    const submit = document.getElementById('loginSubmit');

    form.addEventListener('submit', async (event) => {
        event.preventDefault();
        message.textContent = '';
        submit.disabled = true;
        try {
            const response = await fetch('/auth/password/login', {
                method: 'POST', credentials: 'same-origin',
                headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
                body: JSON.stringify({
                    username: document.getElementById('loginUsername').value.trim(),
                    password: document.getElementById('loginPassword').value,
                    next: card.dataset.next || '',
                }),
            });
            let data = {};
            try { data = await response.json(); } catch (_) { /* corps non JSON */ }
            if (response.ok && data.success && typeof data.next === 'string' && data.next.startsWith('/')) {
                window.location.assign(data.next);
                return;
            }
            document.getElementById('loginPassword').value = '';
            message.textContent = data.error || 'La connexion a échoué.';
        } catch (_) {
            message.textContent = 'Impossible de contacter le serveur.';
        } finally {
            submit.disabled = false;
        }
    });
})();
