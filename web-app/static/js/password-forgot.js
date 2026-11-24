// Mot de passe oublié : la réponse est la même pour tout identifiant, le message aussi.
'use strict';

(() => {
    const csrf = document.querySelector('meta[name="csrf-token"]').content;
    const form = document.getElementById('forgotForm');
    const message = document.getElementById('forgotMessage');
    const submit = document.getElementById('forgotSubmit');

    form.addEventListener('submit', async (event) => {
        event.preventDefault();
        submit.disabled = true;
        try {
            const response = await fetch('/auth/password/forgot', {
                method: 'POST', credentials: 'same-origin',
                headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
                body: JSON.stringify({ identifier: document.getElementById('forgotIdentifier').value.trim() }),
            });
            let data = {};
            try { data = await response.json(); } catch (_) { /* corps non JSON */ }
            message.textContent = data.message || data.error || 'La demande a échoué.';
        } catch (_) {
            message.textContent = 'Impossible de contacter le serveur.';
        } finally {
            submit.disabled = false;
        }
    });
})();
