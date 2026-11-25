// Enrôlement du premier facteur. Les codes de secours ne sont jamais écrits dans le stockage du navigateur (P-T15-4).
'use strict';

(() => {
    const csrf = document.querySelector('meta[name="csrf-token"]').content;
    const form = document.getElementById('enrollForm');
    const message = document.getElementById('enrollMessage');
    const submit = document.getElementById('enrollSubmit');
    let nextUrl = '/auth/password/login';

    form.addEventListener('submit', async (event) => {
        event.preventDefault();
        message.textContent = '';
        submit.disabled = true;
        try {
            const response = await fetch('/auth/password/enroll', {
                method: 'POST', credentials: 'same-origin',
                headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
                body: JSON.stringify({ code: document.getElementById('enrollCode').value.trim() }),
            });
            let data = {};
            try { data = await response.json(); } catch (_) { /* corps non JSON */ }
            if (response.ok && data.success) {
                if (typeof data.next === 'string' && data.next.startsWith('/')) { nextUrl = data.next; }
                document.getElementById('recoveryCodes').textContent = (data.recovery_codes || []).join('\n');
                document.getElementById('enrollStep1').hidden = true;
                    const keyBox = document.getElementById('enrollKeyBox');
                    if (keyBox) keyBox.hidden = true;
                document.getElementById('enrollStep2').hidden = false;
                return;
            }
            document.getElementById('enrollCode').value = '';
            message.textContent = data.error || "L'activation a échoué.";
            if (data.restart) { window.setTimeout(() => window.location.assign('/auth/password/login'), 1500); }
        } catch (_) {
            message.textContent = 'Impossible de contacter le serveur.';
        } finally {
            submit.disabled = false;
        }
    });

    document.getElementById('copyCodes').addEventListener('click', async () => {
        const text = document.getElementById('recoveryCodes').textContent;
        try { await navigator.clipboard.writeText(text); } catch (_) {
            const range = document.createRange();
            range.selectNodeContents(document.getElementById('recoveryCodes'));
            const selection = window.getSelection();
            selection.removeAllRanges();
            selection.addRange(range);
        }
    });
    document.getElementById('codesNoted').addEventListener('click', () => { window.location.assign(nextUrl); });

    // Clé de sécurité (T12) : mêmes étapes finales que le TOTP (codes de secours montrés une fois, puis connexion)
    const keyButton = document.getElementById('enrollKey');
    if (keyButton) {
        if (!Webauthn.supported()) { document.getElementById('enrollKeyBox').hidden = true; }
        keyButton.addEventListener('click', async () => {
            message.textContent = '';
            keyButton.disabled = true;
            try {
                const result = await Webauthn.register('/auth/password/enroll/webauthn/options',
                    '/auth/password/enroll/webauthn/verify', csrf, { name: document.getElementById('enrollKeyName').value });
                const data = result.data;
                if (result.ok && data.success) {
                    if (typeof data.next === 'string' && data.next.startsWith('/')) { nextUrl = data.next; }
                    document.getElementById('recoveryCodes').textContent = (data.recovery_codes || []).join('\n');
                    document.getElementById('enrollStep1').hidden = true;
                    const keyBox = document.getElementById('enrollKeyBox');
                    if (keyBox) keyBox.hidden = true;
                    document.getElementById('enrollStep2').hidden = false;
                    return;
                }
                message.textContent = data.error || "L'enregistrement de la clé a échoué.";
                if (data.restart) { window.setTimeout(() => window.location.assign('/auth/password/login'), 1500); }
            } catch (_) {
                message.textContent = 'Clé refusée ou opération annulée.';
            } finally {
                keyButton.disabled = false;
            }
        });
    }
})();
