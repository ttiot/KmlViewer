// WebAuthn côté navigateur, sans bibliothèque externe (EF-2.8, CSP inchangée). Les options arrivent du serveur en JSON (champs
// binaires en base64url) ; la réponse repart en JSON de la même forme. Aucune donnée de clé n'est conservée dans la page.
'use strict';

const Webauthn = (() => {
    const toBytes = (text) => {
        const pad = '='.repeat((4 - (text.length % 4)) % 4);
        const binary = atob((text + pad).replace(/-/g, '+').replace(/_/g, '/'));
        return Uint8Array.from(binary, (c) => c.charCodeAt(0));
    };
    const toText = (buffer) => {
        const bytes = new Uint8Array(buffer);
        let binary = '';
        bytes.forEach((b) => { binary += String.fromCharCode(b); });
        return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
    };
    const supported = () => !!(window.PublicKeyCredential && navigator.credentials);

    async function post(path, body, csrf) {
        const response = await fetch(path, { method: 'POST', credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf }, body: JSON.stringify(body || {}) });
        let data = {};
        try { data = await response.json(); } catch (_) { /* corps non JSON */ }
        return { ok: response.ok, status: response.status, data };
    }

    // Enregistrement : options -> navigator.credentials.create -> vérification
    async function register(optionsUrl, verifyUrl, csrf, extra = {}) {
        const first = await post(optionsUrl, {}, csrf);
        if (!first.ok) return first;
        const options = first.data;
        options.challenge = toBytes(options.challenge);
        options.user.id = toBytes(options.user.id);
        (options.excludeCredentials || []).forEach((c) => { c.id = toBytes(c.id); });
        const created = await navigator.credentials.create({ publicKey: options });
        const credential = {
            id: created.id, rawId: toText(created.rawId), type: created.type,
            response: { clientDataJSON: toText(created.response.clientDataJSON),
                attestationObject: toText(created.response.attestationObject),
                transports: created.response.getTransports ? created.response.getTransports() : [] },
            clientExtensionResults: created.getClientExtensionResults ? created.getClientExtensionResults() : {},
        };
        return post(verifyUrl, { credential, ...extra }, csrf);
    }

    // Authentification : options -> navigator.credentials.get -> vérification
    async function authenticate(optionsUrl, verifyUrl, csrf) {
        const first = await post(optionsUrl, {}, csrf);
        if (!first.ok) return first;
        const options = first.data;
        options.challenge = toBytes(options.challenge);
        (options.allowCredentials || []).forEach((c) => { c.id = toBytes(c.id); });
        const got = await navigator.credentials.get({ publicKey: options });
        const credential = {
            id: got.id, rawId: toText(got.rawId), type: got.type,
            response: { clientDataJSON: toText(got.response.clientDataJSON), authenticatorData: toText(got.response.authenticatorData),
                signature: toText(got.response.signature),
                userHandle: got.response.userHandle ? toText(got.response.userHandle) : null },
            clientExtensionResults: got.getClientExtensionResults ? got.getClientExtensionResults() : {},
        };
        return post(verifyUrl, { credential }, csrf);
    }

    return { supported, register, authenticate, post, toBytes, toText };
})();
