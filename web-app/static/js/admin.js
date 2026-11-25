// Page d'administration. Règle : toute donnée du serveur passe par textContent (App.el) ; les confirmations sont dans la page,
// jamais une boîte du navigateur ; liens et codes ne sont jamais écrits dans le stockage du navigateur.
'use strict';

(() => {
    const { api, el, notify, dateTime } = App;
    const $ = (id) => document.getElementById(id);
    const state = { me: null, mode: null, idpAdmin: false, users: [], auditBefore: null };

    // -- confirmation dans la page ------------------------------------------------------------------------------------
    function confirmButton(label, question, action, cls = 'btn btn-sm btn-outline-secondary') {
        const holder = el('span', {});
        const reset = () => holder.replaceChildren(start);
        const start = el('button', { type: 'button', class: cls, onclick: () => holder.replaceChildren(
            el('span', { class: 'me-1' }, question),
            el('button', { type: 'button', class: 'btn btn-sm btn-danger me-1', onclick: async () => { reset(); await action(); } }, 'Confirmer'),
            el('button', { type: 'button', class: 'btn btn-sm btn-link', onclick: reset }, 'Annuler')) }, label);
        holder.append(start);
        return holder;
    }

    // -- valeur montrée une seule fois (lien, codes) -------------------------------------------------------------------------
    function showOnce(text, value) {
        $('oneShotText').textContent = text;
        $('oneShotValue').value = value;
        $('oneShot').classList.remove('d-none');
        $('oneShotValue').select();
    }
    $('oneShotCopy').addEventListener('click', async () => {
        try { await navigator.clipboard.writeText($('oneShotValue').value); notify('Copié.', 'info'); } catch (_) { $('oneShotValue').select(); }
    });
    $('oneShotClose').addEventListener('click', () => { $('oneShotValue').value = ''; $('oneShot').classList.add('d-none'); });

    async function act(promise, done) {
        try { const result = await promise; if (done) done(result); return result; } catch (error) { notify(error.message, 'danger'); return null; }
    }

    // -- onglets -----------------------------------------------------------------------------------------------------------
    const loaders = { utilisateurs: loadUsers, invitations: loadInvitations, fonctions: loadFeatures, reglages: loadSettings,
        secrets: loadSecrets, journal: () => loadAudit(true) };

    function openTab() {
        const wanted = (window.location.hash || '#utilisateurs').slice(1).split('/')[0];        // `#reglages/<section>`
        const tab = loaders[wanted] ? wanted : 'utilisateurs';
        if (tab !== 'reglages') AdminSettings.leave();             // à la reprise, les valeurs sont relues ; les saisies sont gardées
        document.querySelectorAll('[data-panel]').forEach((panel) => { panel.hidden = panel.dataset.panel !== tab; });
        document.querySelectorAll('#adminTabs a').forEach((link) => {
            if (link.dataset.tab === tab) link.setAttribute('aria-current', 'page'); else link.removeAttribute('aria-current');
        });
        loaders[tab]();
    }

    // -- utilisateurs ------------------------------------------------------------------------------------------------------
    function toggle(user, field, disabled) {
        return el('input', { type: 'checkbox', class: 'form-check-input', checked: user[field], disabled,
            'aria-label': `${user.username} : ${field === 'is_admin' ? 'administrateur' : 'actif'}`,
            onchange: async (event) => {
                await act(api(`/api/admin/users/${user.id}`, { method: 'PATCH', body: { [field]: event.target.checked } }));
                loadUsers();
            } });
    }

    function creditsInput(user) {
        const input = el('input', { type: 'number', min: 0, max: 1000, value: user.invite_credits, class: 'form-control form-control-sm credits-input',
            'aria-label': `Crédits de ${user.username}` });
        input.addEventListener('change', async () => {
            await act(api(`/api/admin/users/${user.id}`, { method: 'PATCH', body: { invite_credits: Number(input.value) } }));
            loadUsers();
        });
        return input;
    }

    function userActions(user, self) {
        const post = (path, done) => act(api(`/api/admin/users/${user.id}/${path}`, { method: 'POST', body: {} }), done);
        const buttons = [];
        if (user.auth_method === 'password') {
            buttons.push(el('button', { type: 'button', class: 'btn btn-sm btn-outline-secondary', onclick: () => post('reset-link',
                (r) => showOnce(`Lien de réinitialisation pour ${user.username} (24 h, usage unique${r.sent_by_mail ? ', envoyé aussi par e-mail' : ''}) :`, r.link)) }, 'Lien de réinitialisation'));
            if (!self) buttons.push(confirmButton('Réinitialiser le 2e facteur', 'Retirer tous ses facteurs ?',
                () => post('mfa-reset', (r) => { notify(r.demoted ? 'Facteurs retirés ; compte rétrogradé.' : 'Facteurs retirés.'); loadUsers(); })));
            if (user.pending_email) buttons.push(el('button', { type: 'button', class: 'btn btn-sm btn-outline-secondary',
                onclick: () => post('resend-verification', () => notify('Validation renvoyée.')) }, 'Renvoyer la validation'));
        }
        if (user.auth_method === 'oidc' && state.mode === 'password' && user.is_active) {
            buttons.push(el('button', { type: 'button', class: 'btn btn-sm btn-outline-secondary', onclick: () => post('convert',
                (r) => showOnce(`Lien « définir un mot de passe » pour ${user.username} (24 h) :`, r.link)) }, 'Convertir en mot de passe'));
        }
        buttons.push(confirmButton('Révoquer les sessions', 'Fermer ses sessions ?', () => post('revoke-sessions', () => notify('Sessions révoquées.'))));
        if (!self) buttons.push(confirmButton('Supprimer', `Supprimer ${user.username} et TOUTES ses activités ?`,
            () => act(api(`/api/admin/users/${user.id}`, { method: 'DELETE' }), loadUsers), 'btn btn-sm btn-outline-danger'));
        return el('div', { class: 'd-flex flex-wrap gap-1' }, buttons);
    }

    function userRow(user) {
        const self = user.id === state.me.id;
        const f = user.factors;
        const factors = user.auth_method === 'password' ? `${f.totp ? 'TOTP ' : ''}${f.keys ? `clés ×${f.keys} ` : ''}${f.recovery_codes ? `secours ×${f.recovery_codes}` : ''}`.trim() || 'aucun' : 'IdP';
        return el('tr', {},
            el('td', {}, el('div', {}, user.display_name || user.username), el('div', { class: 'mono hint' }, user.username), self ? el('span', { class: 'chip' }, 'toi') : null),
            el('td', {}, user.auth_method),
            el('td', {}, user.email || '—', user.pending_email ? el('div', { class: 'hint' }, `en attente : ${user.pending_email}`) : null),
            el('td', {}, factors),
            el('td', { class: 'text-end' }, creditsInput(user)),
            el('td', { class: 'text-center' }, toggle(user, 'is_active', self)),
            el('td', { class: 'text-center' }, toggle(user, 'is_admin', self || state.idpAdmin)),
            el('td', {}, userActions(user, self)));
    }

    async function loadUsers() {
        const data = await act(api('/api/admin/users'));
        if (!data) return;
        state.idpAdmin = data.admin_managed_by_idp;
        $('usersBody').replaceChildren(...data.users.map(userRow));
        $('adminCount').textContent = `${data.users.length} compte${data.users.length > 1 ? 's' : ''}`;
        const banner = $('collisionBanner');
        banner.classList.toggle('d-none', data.collisions.length === 0);
        banner.textContent = data.collisions.length
            ? `Collision d'identité : ${data.collisions.map((c) => `${c.field} partagé par les comptes ${c.user_ids.join(', ')}`).join(' ; ')}.` : '';
    }

    // -- invitations -------------------------------------------------------------------------------------------------------
    const INVITE_STATUS = { pending: 'En attente', used: 'Acceptée', expired: 'Expirée', revoked: 'Révoquée' };

    async function loadInvitations() {
        const info = await act(api('/api/admin/secrets'));          // `disabled` / `missing` : comptes à mot de passe indisponibles (CA-61)
        if (info && info.disabled) {
            $('inviteForm').hidden = true;
            $('invitesBody').replaceChildren(el('tr', {}, el('td', { colspan: 6, class: 'hint', role: 'status' },
                `Invitations indisponibles : variable(s) manquante(s) — ${(info.missing || []).join(', ')} (mode oidc : SECRETS_KEY et PUBLIC_URL sont requises).`)));
            return;
        }
        $('inviteForm').hidden = false;
        const data = await act(api('/api/admin/invitations'));
        if (!data) return;
        $('invitesBody').replaceChildren(...data.invitations.map((i) => el('tr', {},
            el('td', {}, i.email || '—'), el('td', {}, i.created_by_name || 'compte supprimé'),
            el('td', {}, INVITE_STATUS[i.status] || i.status,
                i.status === 'used' ? el('div', { class: 'hint' }, `compte créé : ${i.used_by_name || 'compte supprimé'}`) : null),
            el('td', {}, dateTime(i.created_at)), el('td', {}, dateTime(i.expires_at)),
            el('td', {}, i.status === 'pending' ? confirmButton('Révoquer', 'Révoquer (crédit non rendu) ?',
                () => act(api(`/api/invitations/${i.id}`, { method: 'DELETE' }), loadInvitations)) : null))));
    }

    $('inviteForm').addEventListener('submit', async (event) => {
        event.preventDefault();
        const body = { email: $('inviteEmail').value.trim(), note: $('inviteNote').value.trim() || undefined,
            initial_credits: Number($('inviteCredits').value) || 0 };
        const result = await act(api('/api/invitations', { method: 'POST', body }));
        if (result) {
            if (result.link) showOnce('Lien d’invitation (montré une seule fois) :', result.link);
            $('inviteEmail').value = '';
            loadInvitations();
        }
    });

    // -- fonctions bêta ----------------------------------------------------------------------------------------------------
    // Diagnostic du moteur (E-21) : lecture humaine, jamais le JSON brut ni l'epoch ; tout passe par textContent (App.el).
    const PROFILE_LABELS = ['foot', 'hike', 'bike', 'racingbike', 'mtb', 'gravel'];
    function ago(epochSeconds, timezone) {
        const minutes = Math.max(0, Math.round((Date.now() / 1000 - epochSeconds) / 60));
        const when = minutes < 1 ? "à l'instant" : minutes < 60 ? `il y a ${minutes} min` : `il y a ${Math.round(minutes / 60)} h`;
        let local = '';
        try {
            local = new Intl.DateTimeFormat('fr-FR', { dateStyle: 'short', timeStyle: 'short', timeZone: timezone || undefined })
                .format(new Date(epochSeconds * 1000));
        } catch (_) { local = new Date(epochSeconds * 1000).toLocaleString('fr-FR'); }
        return `Vérifié ${when} (${local})`;
    }
    function extent(bbox) {
        if (!Array.isArray(bbox) || bbox.length !== 4) return null;
        const [south, west, north, east] = bbox.map((v) => Number(v).toFixed(2));
        return `sud ${south}°, ouest ${west}°, nord ${north}°, est ${east}°`;
    }
    function renderDiagnostic(d, timezone) {
        if (typeof d === 'string') return el('p', { class: 'hint' }, d);
        const badge = (text, kind) => el('span', { class: `badge-soft is-${kind}` }, text);
        const state = !d.configured ? badge('Moteur non configuré', 'warn')
            : d.available ? badge('Moteur disponible', 'good') : badge('Moteur indisponible', 'bad');
        const lines = [el('div', { class: 'diagnostic-line' }, state,
            d.checked_at ? el('span', { class: 'hint' }, ago(d.checked_at, timezone)) : el('span', { class: 'hint' }, 'Pas encore vérifié'))];
        if (d.coverage_contained !== null && d.coverage_contained !== undefined) {
            lines.push(el('div', { class: 'diagnostic-line' }, el('span', {}, 'Zone couverte :'),
                d.coverage_contained ? badge('oui', 'good') : badge('non', 'warn'),
                d.coverage_contained ? null : el('span', { class: 'hint' },
                    "la zone des traces (réglage routing.coverage_bbox) dépasse l'étendue du moteur : certains points seront refusés.")));
        }
        const area = extent(d.info_bbox);
        if (area) lines.push(el('div', { class: 'diagnostic-line' }, el('span', {}, 'Étendue du moteur :'), el('span', { class: 'hint' }, area)));
        if (Array.isArray(d.profiles) && d.profiles.length) {
            const known = PROFILE_LABELS.filter((p) => d.profiles.includes(p));
            const others = d.profiles.filter((p) => !PROFILE_LABELS.includes(p));
            lines.push(el('div', { class: 'diagnostic-line' }, el('span', {}, 'Profils :'), [...known, ...others].map((p) => badge(p, 'info'))));
        }
        if (d.configured) {
            const result = el('span', { role: 'status', class: 'hint' });
            lines.push(el('div', { class: 'diagnostic-line' }, el('button', { type: 'button', class: 'btn btn-sm btn-secondary', onclick: async () => {
                result.textContent = '…';
                const r = await act(api('/api/admin/secrets/routing.api_key/test', { method: 'POST', body: {} }));
                result.textContent = r ? (r.ok ? 'Moteur joignable.' : 'Moteur injoignable.') : 'Test impossible.';
            } }, 'Tester le moteur'), result));
        }
        return el('div', { class: 'diagnostic' }, lines);
    }

    async function loadFeatures() {
        const [features, users] = await Promise.all([act(api('/api/admin/features')), act(api('/api/admin/users'))]);
        if (!features || !users) return;
        $('featuresBox').replaceChildren(...features.features.map((feature) => {
            const picked = new Set(feature.users.map((u) => u.id));
            const select = el('select', { class: 'form-select', 'aria-label': `État de ${feature.label}` },
                ['off', 'users', 'all'].map((value) => el('option', { value, selected: feature.state === value },
                    { off: 'Désactivée', users: 'Pour une liste de comptes', all: 'Pour tous' }[value])));
            const list = el('div', { class: 'd-flex flex-wrap gap-2' }, users.users.map((u) => {
                const box = el('input', { type: 'checkbox', class: 'form-check-input', checked: picked.has(u.id), value: u.id });
                return el('label', { class: 'chip' }, box, ' ', u.username);
            }));
            const diagnostic = feature.diagnostic ? renderDiagnostic(feature.diagnostic, features.timezone) : null;
            return el('div', { class: 'mb-3' }, el('h2', { class: 'h5' }, feature.label), el('p', { class: 'hint' }, feature.description), diagnostic,
                select, el('p', { class: 'mt-2 mb-1' }, 'Comptes (liste conservée quel que soit l’état) :'), list,
                el('button', { type: 'button', class: 'btn btn-primary mt-2', onclick: async () => {
                    const ids = [...list.querySelectorAll('input:checked')].map((b) => Number(b.value));
                    await act(api(`/api/admin/features/${feature.key}`, { method: 'PATCH', body: { state: select.value, users: ids } }), () => notify('Enregistré.'));
                    loadFeatures();
                } }, 'Enregistrer'));
        }));
    }

    // -- réglages : sous-onglets, éditeurs, enregistrement par onglet (static/js/admin-settings.js, §5.6b) ---------------------
    function loadSettings() { return AdminSettings.open(window.location.hash.split('/')[1] || ''); }

    // -- clés API et e-mail -----------------------------------------------------------------------------------------------
    async function loadSecrets() {
        const data = await act(api('/api/admin/secrets'));
        if (!data) return;
        if (data.disabled) {            // E-10, EF-0.4 : formulaire masqué, explication qui nomme les variables manquantes
            $('secretsBox').replaceChildren(el('p', { class: 'hint', role: 'status' },
                `La saisie des clés API et de l'e-mail est désactivée : variable(s) manquante(s) — ${(data.missing || []).join(', ')} (mode oidc : SECRETS_KEY et PUBLIC_URL sont requises).`));
            return;
        }
        $('secretsBox').replaceChildren(...data.secrets.map((secret) => {
            const input = el('input', { type: 'password', class: 'form-control', autocomplete: 'off', maxlength: 1024, 'aria-label': `Valeur de ${secret.name}` });
            const status = secret.configured
                ? `Configuré (${dateTime(secret.updated_at)})${secret.last4 ? ` · …${secret.last4}` : ''}${secret.bound_is_effective ? '' : ' · destination différente : à ressaisir'}`
                : 'Non configuré';
            const result = el('span', { role: 'status', class: 'ms-2' });
            return el('div', { class: 'mb-3' }, el('h2', { class: 'h5 mono' }, secret.name), el('p', { class: 'hint' }, status), input,
                el('button', { type: 'button', class: 'btn btn-primary mt-2 me-1', onclick: async () => {
                    await act(api(`/api/admin/secrets/${secret.name}`, { method: 'PUT', body: { value: input.value } }), () => notify('Secret enregistré.'));
                    input.value = '';
                    loadSecrets();
                } }, 'Enregistrer'),
                el('button', { type: 'button', class: 'btn btn-secondary mt-2 me-1', onclick: async () => {
                    result.textContent = '…';
                    const r = await act(api(`/api/admin/secrets/${secret.name}/test`, { method: 'POST', body: {} }));
                    result.textContent = r ? (r.ok ? 'ok' : 'ko') : 'ko';
                } }, 'Tester'),
                confirmButton('Effacer', 'Effacer ce secret ?', () => act(api(`/api/admin/secrets/${secret.name}`, { method: 'DELETE' }), loadSecrets), 'btn btn-outline-danger mt-2'),
                result);
        }));
    }

    $('mailTest').addEventListener('click', async () => {
        $('mailTestResult').textContent = '…';
        const r = await act(api('/api/admin/mail/test', { method: 'POST', body: {} }));
        $('mailTestResult').textContent = r ? (r.ok ? 'ok' : 'ko') : 'ko';
    });

    // -- journal -----------------------------------------------------------------------------------------------------------
    // Détails d'une ligne du journal (E-21) : liste « clé : valeur » compacte, détail brut replié ; tout en textContent.
    const ISO_DATE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}/;
    function readable(key, value, depth = 0) {
        if (value === null || value === undefined) return '—';
        if (typeof value === 'boolean') return value ? 'oui' : 'non';
        if (typeof value === 'number') {
            if (/(_s|_seconds)$/.test(key)) {
                const m = Math.floor(value / 60), r = Math.round(value % 60);
                return m ? `${m} min${r ? ` ${r} s` : ''}` : `${value} s`;
            }
            return String(value);
        }
        if (typeof value === 'string') return ISO_DATE.test(value) ? dateTime(value) : (value.length > 120 ? `${value.slice(0, 117)}…` : value);
        if (Array.isArray(value)) return value.slice(0, 20).map((v) => readable(key, v, depth + 1)).join(', ') + (value.length > 20 ? ', …' : '');
        if (depth >= 2) return '…';
        return Object.entries(value).map(([k, v]) => `${k} ${readable(k, v, depth + 1)}`).join(' · ');
    }
    function renderDetails(details) {
        if (!details || (typeof details === 'object' && !Object.keys(details).length)) return '';
        if (typeof details !== 'object') return String(details);
        const isEmpty = (v) => v === null || v === undefined || v === '' || (Array.isArray(v) && !v.length)
            || (typeof v === 'object' && !Array.isArray(v) && !Object.keys(v).length);
        const shown = Object.entries(details).filter(([, value]) => !isEmpty(value));          // champs vides omis (le brut les garde)
        const list = el('ul', { class: 'list-unstyled mb-1 audit-details' }, shown.length ? shown.map(([key, value]) =>
            el('li', {}, el('span', { class: 'mono' }, `${key} : `), readable(key, value))) : el('li', {}, '—'));
        const raw = el('details', {}, el('summary', {}, 'Voir le détail'), el('pre', { class: 'mono mb-0' }, JSON.stringify(details, null, 2)));
        return el('div', {}, list, raw);
    }

    async function loadAudit(fresh) {
        if (fresh) { state.auditBefore = null; $('auditBody').replaceChildren(); }
        const params = new URLSearchParams({ limit: '50' });
        if ($('auditAction').value.trim()) params.set('action', $('auditAction').value.trim());
        if ($('auditActor').value) params.set('actor_type', $('auditActor').value);
        if (state.auditBefore) params.set('before_id', state.auditBefore);
        const data = await act(api(`/api/admin/audit?${params}`));
        if (!data) return;
        $('auditBody').append(...data.entries.map((e) => el('tr', {}, el('td', { class: 'text-nowrap' }, dateTime(e.at)),
            el('td', {}, `${e.actor_type}${e.actor_label ? ` · ${e.actor_label}` : ''}`), el('td', { class: 'mono' }, e.action),
            el('td', {}, e.target_type ? `${e.target_type} ${e.target_id ?? ''}` : '—'),
            el('td', { class: 'hint' }, renderDetails(e.details)))));
        state.auditBefore = data.next_before_id;
        $('auditMore').hidden = !data.next_before_id;
    }
    $('auditForm').addEventListener('submit', (event) => { event.preventDefault(); loadAudit(true); });
    $('auditMore').addEventListener('click', () => loadAudit(false));

    document.addEventListener('DOMContentLoaded', async () => {
        try {
            const info = await api('/api/me');
            state.me = info.user;
            state.mode = info.auth_mode;
        } catch (error) { notify(error.message, 'danger'); return; }
        AdminSettings.setup();
        window.addEventListener('hashchange', openTab);
        openTab();
    });
})();
