// Page Réglages de l'administration (admin-space §5.6b, EF-6.9 à EF-6.16) : sous-onglets par section du registre, recherche,
// filtre « modifiées », ligne compacte, éditeurs structurés, enregistrement par onglet avec confirmation avant / après et contrôle
// de version. Le SERVEUR reste l'autorité : les contrôles d'ici ne servent qu'au confort.
// Règles : tout passe par le DOM et textContent (App.el), jamais innerHTML ; aucune boîte du navigateur (confirmations dans la page) ;
// aucune valeur de réglage n'est écrite dans l'URL ni dans le stockage du navigateur (une valeur peut être un hôte interne).
'use strict';

const AdminSettings = (() => {
    const { api, el, notify } = App;
    const $ = (id) => document.getElementById(id);

    // Contrôle de saisie de CHAQUE type du registre ; aucun ne passe par une zone JSON hors du volet « Avancé » (EF-6.12)
    const CONTROL_FOR_TYPE = { bool: 'switch', enum: 'select', int: 'number', float: 'number', str: 'text', url: 'text', email: 'text',
        timezone: 'text', list: 'chips', basemaps: 'basemap-cards', polygon: 'polygon-map' };
    const STEP = 0.0001, MAP_LAT = 85.0511;                  // pas des champs ; limite de la projection Web Mercator (EF-6.21)
    const MARKUP = /[<>&"']/;
    const LINKED_SECRETS = { 'smtp.host': 'smtp.password', 'smtp.port': 'smtp.password', 'routing.base_url': 'routing.api_key' };
    const ALERT_KEYS = { smtp: ['smtp.host', 'smtp.port'], routing: ['routing.base_url'], dem: ['DEM_URL'], tiles: ['map.basemaps'] };

    const S = {
        settings: [], byKey: {}, sections: [], version: null, alerts: [], secrets: {}, edits: {}, errors: {}, tab: null, search: '',
        jsonViews: {}, polygonIssues: {}, onlyModified: false, loaded: false, stale: true, confirm: null, previewMaps: [], registerUnload: false,
    };

    // -- utilitaires -------------------------------------------------------------------------------------------------------
    const fold = (text) => String(text ?? '').normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase();
    const slug = (section) => fold(section).replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');
    const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
    const loadedValue = (key) => S.byKey[key].value;
    const current = (key) => (key in S.edits ? S.edits[key] : loadedValue(key));
    const isDirty = (key) => key in S.edits;
    const sectionSettings = (section) => S.settings.filter((s) => s.section === section && !s.secret);

    function setEdit(key, value, rerender = false) {
        if (same(value, loadedValue(key))) delete S.edits[key]; else S.edits[key] = value;
        delete S.errors[key];
        const holder = document.querySelector(`[data-setting="${CSS.escape(key)}"]`);      // l'erreur affichée disparaît à la saisie
        if (holder) {
            holder.querySelectorAll('.setting-error').forEach((n) => { n.textContent = ''; n.hidden = true; });
            holder.querySelectorAll('[aria-invalid]').forEach((n) => n.removeAttribute('aria-invalid'));
        }
        if (key === 'map.basemaps') refreshDefaultSelect();
        const json = S.jsonViews[key];                  // volet « Avancé » : reflète la saisie en cours (sauf pendant sa propre édition)
        if (json && json.isConnected && document.activeElement !== json) json.value = JSON.stringify(current(key), null, 2);
        if (rerender) renderPanel(); else updateChrome();
    }

    function format(setting, value) {
        const unit = setting.unit ? ` ${setting.unit}` : '';
        switch (setting.type) {
            case 'bool': return value ? 'oui' : 'non';
            case 'int': case 'float': return `${value}${unit}`;
            case 'basemaps': return `${(value || []).length} fond(s) : ${(value || []).map((b) => b.name).join(', ') || '—'}`;
            case 'polygon': return polygonSummary(value);
            case 'list': return (value || []).join(', ') || '—';
            default: return value === '' || value === null || value === undefined ? '(vide)' : String(value);
        }
    }
    function polygonSummary(vertices) {
        if (!Array.isArray(vertices)) return '—';
        if (Zone.check(vertices)) return `${vertices.length} sommets`;
        const { width, height } = Zone.sides(vertices);
        return `${vertices.length} sommets, emprise ≈ ${Math.round(width / 1000)} km × ${Math.round(height / 1000)} km`;
    }
    const originOf = (setting) => (setting.source === 'env' ? 'Environnement' : setting.source === 'base' ? 'Base' : 'Défaut');

    function hint(setting) {
        const parts = [];
        if (setting.type === 'int' || setting.type === 'float') parts.push(`${setting.min} à ${setting.max}${setting.unit ? ` ${setting.unit}` : ''}`);
        else if (['str', 'email', 'url', 'timezone'].includes(setting.type) && setting.max_length) parts.push(`${setting.max_length} caractères au plus`);
        else if (setting.type === 'basemaps') parts.push(`${setting.max_items} fonds au plus`);
        else if (setting.type === 'list') parts.push(`${setting.max_items} éléments au plus`);
        if (setting.env) parts.push(setting.env_overridden ? `variable ${setting.env}, remplacée par la valeur en base` : `variable ${setting.env}`);
        return parts.join(' · ');
    }

    // -- contrôles de la page (le serveur garde les siens) ----------------------------------------------------------------
    function tileHosts(basemap, allowlist) {
        const letters = (basemap.subdomains || 'abc').split('');
        const candidates = basemap.url.includes('{s}') ? letters.map((c) => basemap.url.split('{s}').join(c)) : [basemap.url];
        return candidates.map((u) => {
            try { const parsed = new URL(u.replace(/\{[zxyr]\}/g, '0')); return parsed.host; } catch (_) { return null; }
        }).map((host) => ({ host, ok: host !== null && allowlist.includes(host) }));
    }
    function validateBasemaps(list, setting) {
        const names = new Set();
        for (let index = 0; index < list.length; index += 1) {
            const b = list[index];
            const fail = (message) => ({ message, index });
            if (!b.name || !b.name.trim()) return fail('Le nom est obligatoire.');
            if (MARKUP.test(b.name)) return fail('Nom : texte brut (< > & " \' refusés).');
            if (b.name.length > 40) return fail('Nom : 40 caractères au plus.');
            if (names.has(b.name.trim().toLowerCase())) return fail('Un autre fond porte déjà ce nom.');
            names.add(b.name.trim().toLowerCase());
            if (!/^https:\/\//.test(b.url) || !['{z}', '{x}', '{y}'].every((t) => b.url.includes(t))) {
                return fail("L'URL doit être en https:// et contenir {z}, {x} et {y}.");
            }
            if (b.url.length > 500) return fail('URL : 500 caractères au plus.');
            if (!/^[a-z]{1,4}$/.test(b.subdomains || 'abc') || new Set(b.subdomains || 'abc').size !== (b.subdomains || 'abc').length) return fail('Sous-domaines : 1 à 4 lettres minuscules distinctes.');
            if ((b.attribution || '').length > 200 || MARKUP.test(b.attribution || '')) return fail('Attribution : 200 caractères au plus, texte brut (< > & " \' refusés).');
            if (!Number.isInteger(b.max_zoom) || b.max_zoom < 1 || b.max_zoom > 22) return fail('Zoom maximal : 1 à 22.');
            const refused = tileHosts(b, setting.allowlist || []).find((h) => !h.ok);
            if (refused) {
                return fail(`L'hôte ${refused.host || b.url} n'est pas autorisé : ajoute-le à OUTBOUND_ALLOWED_HOSTS (tiles=), puis redémarre l'application.`);
            }
        }
        return null;
    }
    function validateOne(setting) {
        const value = current(setting.key);
        switch (setting.type) {
            case 'int': case 'float':
                if (typeof value !== 'number' || !Number.isFinite(value)) return { message: 'Un nombre est attendu.' };
                if (setting.type === 'int' && !Number.isInteger(value)) return { message: 'Un entier est attendu.' };
                if (value < setting.min || value > setting.max) return { message: `Valeur de ${setting.min} à ${setting.max}.` };
                return null;
            case 'basemaps': return validateBasemaps(value, setting);
            case 'polygon': {
                const message = S.polygonIssues[setting.key] || Zone.check(value);      // une seule fonction de contrôle, celle du serveur (EF-6.20)
                return message ? { message } : null;
            }
            case 'list': return (value || []).length > setting.max_items ? { message: `${setting.max_items} éléments au plus.` } : null;
            case 'enum': return setting.choices.includes(value) ? null : { message: 'Valeur non permise.' };
            case 'str': case 'email': case 'url': case 'timezone':
                return typeof value === 'string' && value.length <= (setting.max_length || 1e9) ? null : { message: `${setting.max_length} caractères au plus.` };
            default: return null;
        }
    }
    function validateTab(section) {
        let first = null;
        sectionSettings(section).filter((s) => isDirty(s.key) || (s.type === 'polygon' && S.polygonIssues[s.key])).forEach((setting) => {
            const problem = validateOne(setting);
            if (problem) { S.errors[setting.key] = problem; first = first || setting.key; }
        });
        // Fond par défaut : doit exister dans la saisie (page seulement, EF-6.12)
        const defaultKey = 'map.default_basemap';
        if (S.byKey[defaultKey] && S.byKey[defaultKey].section === section && (isDirty(defaultKey) || isDirty('map.basemaps'))) {
            const names = current('map.basemaps').map((b) => b.name);
            if (!names.includes(current(defaultKey))) {
                S.errors[defaultKey] = { message: 'Choisis un autre fond par défaut.' };
                first = first || defaultKey;
            }
        }
        return first;
    }

    // -- éditeurs ---------------------------------------------------------------------------------------------------------
    function errorLine(key, index = null) {
        const error = S.errors[key];
        const id = `err-${key}${index === null ? '' : `-${index}`}`;
        const node = el('p', { class: 'setting-error', id, role: 'alert' }, '');
        if (error && (error.index === undefined || error.index === null ? index === null : error.index === index)) node.textContent = error.message;
        node.hidden = !node.textContent;
        return node;
    }

    function switchEditor(setting) {
        const input = el('input', { type: 'checkbox', class: 'form-check-input', id: `f-${setting.key}`, checked: !!current(setting.key),
            'data-key': setting.key, onchange: () => setEdit(setting.key, input.checked) });
        return el('div', { class: 'form-check form-switch' }, input);
    }
    function selectEditor(setting) {
        const select = el('select', { class: 'form-select', id: `f-${setting.key}`, 'data-key': setting.key,
            onchange: () => setEdit(setting.key, select.value) },
        setting.choices.map((c) => el('option', { value: c, selected: current(setting.key) === c }, c)));
        return select;
    }
    function numberEditor(setting) {
        const input = el('input', { type: 'number', class: 'form-control', id: `f-${setting.key}`, 'data-key': setting.key, min: setting.min,
            max: setting.max, step: setting.type === 'int' ? 1 : 'any', value: current(setting.key),
            oninput: () => setEdit(setting.key, input.value === '' ? NaN : Number(input.value)) });
        return setting.unit ? el('div', { class: 'input-group' }, input, el('span', { class: 'input-group-text' }, setting.unit)) : input;
    }
    function textEditor(setting) {
        const input = el('input', { type: 'text', class: 'form-control', id: `f-${setting.key}`, 'data-key': setting.key,
            maxlength: setting.max_length, value: current(setting.key) ?? '', autocomplete: 'off', spellcheck: 'false',
            oninput: () => setEdit(setting.key, input.value) });
        return input;
    }

    // Fond par défaut : liste des NOMS des fonds, saisies non enregistrées comprises
    function defaultOptions(setting) {
        const names = (current('map.basemaps') || []).map((b) => b.name);
        const value = current(setting.key);
        const all = names.includes(value) ? names : [...names, value];
        return all.map((n) => el('option', { value: n, selected: n === value }, names.includes(n) ? n : `${n} (inexistant)`));
    }
    function defaultEditor(setting) {
        const select = el('select', { class: 'form-select', id: `f-${setting.key}`, 'data-key': setting.key,
            onchange: () => setEdit(setting.key, select.value) }, defaultOptions(setting));
        return select;
    }
    function refreshDefaultSelect() {
        const select = document.getElementById('f-map.default_basemap');
        if (select) select.replaceChildren(...defaultOptions(S.byKey['map.default_basemap']));
    }

    function chipsEditor(setting) {
        const holder = el('div', { class: 'chips-editor' });
        const input = el('input', { type: 'text', class: 'form-control', maxlength: setting.max_length, 'aria-label': `Ajouter à ${setting.label}` });
        const problem = el('p', { class: 'setting-error', role: 'alert', hidden: true });
        const draw = () => {
            const items = current(setting.key);
            const add = () => {
                const value = input.value.trim();
                const fail = (m) => { problem.textContent = m; problem.hidden = false; };
                if (!value) return;
                if (value.length > setting.max_length) return fail(`${setting.max_length} caractères au plus.`);
                if (items.includes(value)) return fail('Déjà dans la liste.');
                setEdit(setting.key, [...items, value]); problem.hidden = true; input.value = ''; draw();
            };
            holder.replaceChildren(el('div', { class: 'd-flex flex-wrap gap-2 mb-2' }, items.map((item) => el('span', { class: 'chip' }, item,
                el('button', { type: 'button', class: 'chip-remove', 'aria-label': `Retirer ${item}`,
                    onclick: () => { setEdit(setting.key, items.filter((i) => i !== item)); draw(); } }, '×')))),
            el('div', { class: 'input-group' }, input, el('button', { type: 'button', class: 'btn btn-secondary', disabled: items.length >= setting.max_items,
                onclick: add }, 'Ajouter')), problem);
            input.onkeydown = (event) => { if (event.key === 'Enter') { event.preventDefault(); add(); } };
        };
        draw();
        return holder;
    }

    // Fonds de carte : cartes ordonnées
    function basemapEditor(setting) {
        const holder = el('div', { class: 'basemaps-editor' });
        const draw = () => {
            const list = current(setting.key);
            const cards = list.map((b, index) => {
                const field = (label, attrs, key, parse = (v) => v) => {
                    const id = `bm-${index}-${key}`;
                    const input = el('input', { class: 'form-control', id, value: b[key] ?? '', ...attrs,
                        oninput: () => {            // copie : la valeur chargée n'est jamais modifiée en place
                            const next = current(setting.key).map((x) => ({ ...x }));
                            next[index][key] = parse(input.value);
                            setEdit(setting.key, next);
                        } });
                    return el('div', { class: 'field' }, el('label', { for: id }, label), input);
                };
                const live = () => current(setting.key);            // toujours partir des saisies en cours, pas de la liste figée au dernier dessin
                const remove = confirmInline('Supprimer', `Supprimer le fond « ${b.name} » ?`, () => { setEdit(setting.key, live().filter((_, i) => i !== index)); draw(); });
                return el('div', { class: 'basemap-card', 'data-index': index },
                    el('div', { class: 'd-flex gap-2 align-items-center mb-2' }, el('strong', {}, `Fond ${index + 1}`),
                        el('button', { type: 'button', class: 'btn btn-sm btn-outline-secondary', disabled: index === 0, 'aria-label': `Monter le fond ${index + 1}`,
                            onclick: () => { const next = live().map((x) => ({ ...x })); [next[index - 1], next[index]] = [next[index], next[index - 1]]; setEdit(setting.key, next); draw(); } }, 'Monter'),
                        el('button', { type: 'button', class: 'btn btn-sm btn-outline-secondary', disabled: index === list.length - 1, 'aria-label': `Descendre le fond ${index + 1}`,
                            onclick: () => { const next = live().map((x) => ({ ...x })); [next[index + 1], next[index]] = [next[index], next[index + 1]]; setEdit(setting.key, next); draw(); } }, 'Descendre'),
                        remove),
                    el('div', { class: 'field-grid' }, field('Nom', { maxlength: 40 }, 'name'),
                        field('Sous-domaines', { maxlength: 4 }, 'subdomains'),
                        field('Zoom maximal', { type: 'number', min: 1, max: 22, step: 1 }, 'max_zoom', (v) => (v === '' ? NaN : Number(v)))),
                    field('URL', { maxlength: 500, spellcheck: 'false' }, 'url'),
                    el('p', { class: 'hint mb-1' }, 'https://…/{z}/{x}/{y}… ; {s} = sous-domaine, {r} = suffixe rétine facultatif.'),
                    field('Attribution', { maxlength: 200 }, 'attribution'),
                    errorLine(setting.key, index));
            });
            holder.replaceChildren(...cards, el('button', { type: 'button', class: 'btn btn-secondary', disabled: list.length >= setting.max_items,
                onclick: () => { setEdit(setting.key, [...current(setting.key), { name: '', url: 'https://', subdomains: 'abc', attribution: '', max_zoom: 19 }]); draw(); } },
            'Ajouter un fond'), advancedPane(setting, draw));
        };
        draw();
        return holder;
    }

    const r4 = (v) => Math.round(v * 1e4) / 1e4;
    const clampTo = (v, lo, hi) => (hi < lo ? v : Math.min(hi, Math.max(lo, v)));

    // Polygone : carte Leaflet éditable + liste des sommets (EF-6.20, EF-6.21). Les contrôles sont ceux du serveur (Zone.check).
    const TOLERANCE_MOUSE = 10, TOLERANCE_TOUCH = 16;
    function polygonEditor(setting) {
        const key = setting.key;
        const holder = el('div', { class: 'polygon-editor' });
        const dims = el('p', { class: 'hint mb-1' });
        const problem = el('p', { class: 'setting-error poly-slot', role: 'alert', hidden: true });     // case réservée : la carte ne bouge jamais
        const notice = el('p', { class: 'hint mb-1', role: 'status' });                // « 50 sommets au plus. », « 3 sommets au moins. »
        const preview = el('div', { class: 'poly-preview', role: 'group',
            'aria-label': 'Carte de la zone couverte : déplace les sommets, ou saisis-les dans la liste' });
        const help = el('p', { class: 'hint' }, 'Clique ou glisse sur un côté pour ajouter un sommet ; glisse un sommet pour le déplacer ; clic droit pour le retirer.');
        const listPane = el('details', { class: 'advanced poly-list-pane' });
        const listBody = el('div', { class: 'poly-list' });
        let map = null, shape = null, handles = [], selected = null, hover = null, gesture = null, listCount = -1;
        const issues = new Map();                                                       // champs de la liste en erreur : index -> message

        const shown = () => (Array.isArray(current(key)) ? current(key) : []).map((v) => [...v]);
        const drawable = (vs) => Array.isArray(vs) && vs.length >= 3 && vs.every((v) => Number.isFinite(v[0]) && Number.isFinite(v[1])
            && Math.abs(v[0]) <= 180 && Math.abs(v[1]) <= MAP_LAT);
        let lastDrawn = shown();
        const commit = (vs) => {
            setEdit(key, vs);
            delete S.polygonIssues[key];
            if (issues.size) { issues.clear(); listCount = -1; }                       // les erreurs de champ disparaissent : la liste est reconstruite
            notice.textContent = '';
            refresh();
        };
        const clampVertex = (lon, lat) => [r4(clampTo(lon, -180, 180)), r4(clampTo(lat, -MAP_LAT, MAP_LAT))];
        const midpoint = (a, b) => {                                                     // milieu pris dans la projection de la carte
            const [ax, ay] = Zone.project(a[0], a[1]), [bx, by] = Zone.project(b[0], b[1]);
            return clampVertex(...Zone.unproject((ax + bx) / 2, (ay + by) / 2));
        };

        // -- actions sur les sommets (carte, clavier, liste) ------------------------------------------------------------------
        const insertAfter = (index, vertex = null) => {
            const vs = shown();
            if (vs.length >= Zone.MAX_VERTICES) { notice.textContent = '50 sommets au plus.'; return false; }
            vs.splice(index + 1, 0, vertex || midpoint(vs[index], vs[(index + 1) % vs.length]));
            commit(vs);
            focusHandle(index + 1);
            return true;
        };
        const removeAt = (index) => {
            const vs = shown();
            if (vs.length <= Zone.MIN_VERTICES) { notice.textContent = '3 sommets au moins.'; return false; }
            vs.splice(index, 1);
            if (selected === index) selected = null; else if (selected !== null && selected > index) selected -= 1;
            commit(vs);
            focusHandle(Math.min(index, vs.length - 1));
            return true;
        };
        const focusHandle = (index) => { const node = handles[index] && handles[index].getElement(); if (node) node.focus(); };

        // -- liste des sommets : alternative complète à la carte, au clavier --------------------------------------------------
        const buildList = () => {
            const vs = shown();
            listCount = vs.length;
            listPane.querySelector('summary').textContent = `Coordonnées des sommets (${vs.length})`;
            listBody.replaceChildren(...vs.map((vertex, i) => {
                const field = (axis, label, bound) => {
                    const input = el('input', { type: 'number', class: 'form-control', step: STEP, min: -bound, max: bound, value: vertex[axis],
                        'aria-label': `${label} du sommet ${i + 1}`, 'data-axis': axis,
                        oninput: () => onField(i, axis, input) });
                    return el('div', { class: 'field' }, el('label', {}, label), input);
                };
                return el('div', { class: 'poly-row', 'data-index': i },
                    el('span', { class: 'poly-num' }, String(i + 1)), field(0, 'Longitude', 180), field(1, 'Latitude', MAP_LAT),
                    el('button', { type: 'button', class: 'btn btn-sm btn-outline-secondary', onclick: () => insertAfter(i) }, 'Ajouter après'),
                    el('button', { type: 'button', class: 'btn btn-sm btn-outline-danger', onclick: () => removeAt(i) }, 'Retirer'),
                    el('p', { class: 'setting-error', role: 'alert', hidden: true }));
            }));
        };
        const syncList = () => {
            if (listCount !== shown().length) { buildList(); return; }
            shown().forEach((vertex, i) => listBody.querySelectorAll(`[data-index="${i}"] input`).forEach((input) => {
                if (document.activeElement !== input) input.value = vertex[Number(input.dataset.axis)];
            }));
        };
        const onField = (index, axis, input) => {
            const row = listBody.querySelector(`[data-index="${index}"]`);
            const error = row.querySelector('.setting-error');
            const number = input.value === '' ? NaN : Number(input.value);
            const bound = axis === 0 ? 180 : MAP_LAT;
            let message = '';
            if (!Number.isFinite(number)) message = 'Une valeur est requise.';
            else if (Math.abs(number) > bound) message = 'Zone : longitude de −180 à 180, latitude de −85,0511 à 85,0511.';
            error.textContent = message;
            error.hidden = !message;
            if (message) { issues.set(`${index}-${axis}`, message); input.setAttribute('aria-invalid', 'true'); }
            else { issues.delete(`${index}-${axis}`); input.removeAttribute('aria-invalid'); }
            S.polygonIssues[key] = [...issues.values()][0];
            if (!S.polygonIssues[key]) delete S.polygonIssues[key];
            if (message) { refresh(); return; }                                          // le polygone garde sa dernière valeur valide
            const vs = shown();
            vs[index][axis] = number;
            setEdit(key, vs);
            if (issues.size) S.polygonIssues[key] = [...issues.values()][0];
            refresh();
        };

        // -- dessin : un seul L.polygon et un marqueur par sommet, jamais recréés à la saisie ----------------------------------
        const bounds = () => shape.getBounds();
        const refresh = () => {
            const value = current(key);
            const issue = S.polygonIssues[key] || Zone.check(value);
            problem.textContent = issue || '';
            problem.hidden = !issue;
            const vs = shown();
            if (vs.length >= 3 && drawable(vs)) {
                const { width, height } = Zone.sides(vs);
                dims.textContent = `Emprise ≈ ${Math.round(width / 1000)} km × ${Math.round(height / 1000)} km · ${vs.length} sommets`;
            } else dims.textContent = `${vs.length} sommets`;
            if (drawable(vs)) lastDrawn = vs;
            if (map && shape) {
                const spots = lastDrawn.map(([lon, lat]) => [lat, lon]);
                shape.setLatLngs(spots);
                shape.setStyle(issue ? { color: '#dc3545', dashArray: '6 6' } : { color: '#0d6efd', dashArray: null });
                if (handles.length !== spots.length) buildHandles(); else handles.forEach((m, i) => { if (!m.isDragging) m.setLatLng(spots[i]); });
                handles.forEach((m, i) => {
                    const node = m.getElement();
                    if (node) { node.setAttribute('aria-label', `Sommet ${i + 1} sur ${handles.length} : flèches pour déplacer, Suppr pour retirer, Insér pour ajouter un sommet après`);
                        node.classList.toggle('is-selected', selected === i); node.title = `Sommet ${i + 1}`; }
                });
            }
            syncList();
            removeButton.disabled = selected === null;
        };
        const buildHandles = () => {
            handles.forEach((m) => map.removeLayer(m));
            handles = lastDrawn.map(([lon, lat], i) => {
                const icon = L.divIcon({ className: 'rect-handle poly-handle', html: el('span', { class: 'poly-handle-num' }, String(i + 1)), iconSize: [28, 28] });
                const marker = L.marker([lat, lon], { draggable: true, icon, keyboard: true, zIndexOffset: 100 }).addTo(map);
                marker.on('dragstart', () => { marker.isDragging = true; });
                marker.on('drag', () => {
                    const at = marker.getLatLng();
                    if (!Number.isFinite(at.lat) || !Number.isFinite(at.lng)) return;
                    const vs = shown();
                    vs[i] = clampVertex(at.lng, at.lat);
                    commit(vs);
                });
                marker.on('dragend', () => { marker.isDragging = false; refresh(); });          // se recale sur les valeurs arrondies
                marker.on('click', () => { selected = selected === i ? null : i; refresh(); });
                marker.on('contextmenu', () => removeAt(i));
                const node = marker.getElement();
                node.setAttribute('role', 'button');
                node.addEventListener('keydown', (event) => {
                    const dir = { ArrowRight: [1, 0], ArrowLeft: [-1, 0], ArrowUp: [0, 1], ArrowDown: [0, -1] }[event.key];
                    if (dir) {
                        event.preventDefault(); event.stopPropagation();
                        const step = event.shiftKey ? 0.1 : 0.01, vs = shown();
                        vs[i] = clampVertex(vs[i][0] + dir[0] * step, vs[i][1] + dir[1] * step);
                        commit(vs);
                    } else if (event.key === 'Insert' || event.key === '+') { event.preventDefault(); event.stopPropagation(); insertAfter(i); }
                    else if (event.key === 'Delete' || event.key === 'Backspace') { event.preventDefault(); event.stopPropagation(); removeAt(i); }
                });
                return marker;
            });
        };
        const fit = () => {
            if (!map || !shape || !drawable(lastDrawn)) return;
            const size = map.getSize();
            if (!size.x || !size.y) return;
            map.invalidateSize({ animate: false });
            map.fitBounds(bounds(), { padding: [14, 14], animate: false });
        };

        // -- appui sur une côté : le nouveau sommet suit le pointeur jusqu'au relâchement (EF-6.21) ---------------------------
        const nearestEdge = (point) => {
            const pts = lastDrawn.map(([lon, lat]) => map.latLngToContainerPoint([lat, lon]));
            let best = null;
            pts.forEach((a, i) => {
                const b = pts[(i + 1) % pts.length];
                const dx = b.x - a.x, dy = b.y - a.y, len2 = dx * dx + dy * dy;
                const t = len2 === 0 ? 0 : Math.max(0, Math.min(1, ((point.x - a.x) * dx + (point.y - a.y) * dy) / len2));
                const d = Math.hypot(point.x - (a.x + t * dx), point.y - (a.y + t * dy));
                if (!best || d < best.distance) best = { distance: d, edge: i, latlng: map.containerPointToLatLng([a.x + t * dx, a.y + t * dy]) };
            });
            return best;
        };
        const toleranceFor = (event) => (event.pointerType === 'mouse' || !event.pointerType ? TOLERANCE_MOUSE : TOLERANCE_TOUCH);
        const clearHover = () => {
            if (hover) { map.removeLayer(hover); hover = null; }
            preview.classList.remove('is-over-edge');
        };
        const onDown = (event) => {
            if (!map || gesture || (event.button !== undefined && event.button > 0) || !drawable(lastDrawn)) return;
            if (event.target.closest && event.target.closest('.leaflet-marker-icon, .leaflet-control')) return;      // un sommet passe avant une arête
            const hit = nearestEdge(map.mouseEventToContainerPoint(event));
            if (!hit || hit.distance > toleranceFor(event)) return;
            event.preventDefault();
            event.stopPropagation();
            if (shown().length >= Zone.MAX_VERTICES) { notice.textContent = '50 sommets au plus.'; return; }
            clearHover();
            map.dragging.disable();
            const start = shown();
            const at = map.mouseEventToLatLng(event);
            const vs = shown();
            const index = hit.edge + 1;
            vs.splice(index, 0, clampVertex(at.lng, at.lat));
            commit(vs);
            gesture = { index, start };
            const move = (e) => {
                const latlng = map.mouseEventToLatLng(e);
                if (!Number.isFinite(latlng.lat) || !Number.isFinite(latlng.lng)) return;
                const next = shown();
                next[index] = clampVertex(latlng.lng, latlng.lat);
                commit(next);
            };
            const finish = (keep) => {
                document.removeEventListener('pointermove', move);
                document.removeEventListener('pointerup', up);
                document.removeEventListener('pointercancel', cancel);
                document.removeEventListener('keydown', onKey, true);
                map.dragging.enable();
                gesture = null;
                if (!keep) commit(start);                                                 // Échap : le nouveau sommet disparaît
                else refresh();
            };
            const up = () => finish(true);
            const cancel = () => finish(false);
            const onKey = (e) => { if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); finish(false); } };
            document.addEventListener('pointermove', move);
            document.addEventListener('pointerup', up);
            document.addEventListener('pointercancel', cancel);
            document.addEventListener('keydown', onKey, true);
        };
        const onMove = (event) => {
            if (!map || gesture || event.pointerType !== 'mouse') return;
            if ((event.target.closest && event.target.closest('.leaflet-marker-icon, .leaflet-control')) || !drawable(lastDrawn)) { clearHover(); return; }
            const hit = nearestEdge(map.mouseEventToContainerPoint(event));
            if (!hit || hit.distance > TOLERANCE_MOUSE) { clearHover(); return; }
            if (!hover) {
                hover = L.circleMarker(hit.latlng, { radius: 5, color: '#fff', weight: 2, fillColor: '#0d6efd', fillOpacity: 1, interactive: false }).addTo(map);
                hover.bindTooltip('Clique ou glisse pour ajouter un sommet', { direction: 'top', offset: [0, -6] });
                hover.openTooltip();
                preview.classList.add('is-over-edge');
            }
            hover.setLatLng(hit.latlng);
        };

        const init = () => {
            if (typeof L === 'undefined' || !holder.isConnected || map || !drawable(lastDrawn)) return;
            if (!preview.clientWidth || !preview.clientHeight) {
                if (typeof ResizeObserver === 'function') {
                    const watcher = new ResizeObserver(() => { if (preview.clientWidth && preview.clientHeight) { watcher.disconnect(); init(); } });
                    watcher.observe(preview);
                }
                return;
            }
            map = L.map(preview, { zoomControl: false, attributionControl: false });
            S.previewMaps.push(map);
            const maps = S.byKey['map.basemaps'], wanted = S.byKey['map.default_basemap'];
            const base = maps && wanted && (maps.value || []).find((b) => b.name === wanted.value);
            if (base && tileHosts(base, maps.allowlist || []).every((h) => h.ok)) App.basemapLayer(base).addTo(map);
            shape = L.polygon(lastDrawn.map(([lon, lat]) => [lat, lon]), { color: '#0d6efd', weight: 2, fill: false, interactive: false }).addTo(map);
            fit();
            preview.addEventListener('pointerdown', onDown, true);                       // avant Leaflet : un appui sur un côté ne déplace pas la carte
            preview.addEventListener('pointermove', onMove);
            preview.addEventListener('pointerleave', clearHover);
            buildHandles();
            refresh();
        };

        const removeButton = el('button', { type: 'button', class: 'btn btn-sm btn-outline-danger mt-1', disabled: true,
            onclick: () => { if (selected !== null) removeAt(selected); } }, 'Retirer le sommet');
        listPane.append(el('summary', {}, 'Coordonnées des sommets'), listBody);
        holder.append(dims, problem, preview, help, notice,
            el('div', { class: 'd-flex flex-wrap gap-2' }, el('button', { type: 'button', class: 'btn btn-sm btn-outline-secondary mt-1', onclick: fit }, 'Recadrer'),
                removeButton),
            listPane, advancedPane(setting, () => { selected = null; issues.clear(); notice.textContent = ''; listCount = -1; refresh(); fit(); }));
        buildList();
        refresh();
        setTimeout(init, 0);
        return holder;
    }

    // Volet « Avancé » : JSON brut de la valeur EN COURS DE SAISIE (basemaps et polygone)
    function advancedPane(setting, redraw) {
        const area = el('textarea', { class: 'form-control mono', rows: 8, 'aria-label': `JSON de ${setting.label}` },
            JSON.stringify(current(setting.key), null, 2));
        const problem = el('p', { class: 'setting-error', role: 'alert', hidden: true });
        S.jsonViews[setting.key] = area;
        const pane = el('details', { class: 'advanced' });
        const apply = () => {
            let parsed;
            try { parsed = JSON.parse(area.value); } catch (_) { problem.textContent = 'JSON invalide.'; problem.hidden = false; return; }
            if (setting.type === 'polygon' && Array.isArray(parsed) && parsed.length >= 4 && same(parsed[0], parsed[parsed.length - 1])) {
                parsed = parsed.slice(0, -1);                    // contour fermé : le doublon final est retiré avant d'appliquer (EF-6.21)
            }
            const shape = setting.type === 'polygon'
                ? Array.isArray(parsed) && parsed.every((v) => Array.isArray(v) && v.length === 2 && v.every((n) => typeof n === 'number'))
                : Array.isArray(parsed) && parsed.every((b) => b && typeof b === 'object' && !Array.isArray(b));
            if (!shape) { problem.textContent = 'Forme inattendue.'; problem.hidden = false; return; }
            problem.hidden = true;
            setEdit(setting.key, setting.type === 'basemaps' ? parsed.map((b) => ({ subdomains: 'abc', attribution: '', max_zoom: 19, ...b })) : parsed);
            delete S.polygonIssues[setting.key];
            redraw();
            area.value = JSON.stringify(current(setting.key), null, 2);
            pane.open = false;
        };
        pane.append(el('summary', {}, 'Avancé'), area,
            el('button', { type: 'button', class: 'btn btn-sm btn-secondary mt-1', onclick: apply }, 'Appliquer'), problem);
        return pane;
    }

    // Confirmation en ligne (dans la page, jamais confirm())
    function confirmInline(label, question, action, cls = 'btn btn-sm btn-outline-danger') {
        const holder = el('span', { class: 'ms-auto' });
        const start = el('button', { type: 'button', class: cls, onclick: () => holder.replaceChildren(el('span', { class: 'me-1' }, question),
            el('button', { type: 'button', class: 'btn btn-sm btn-danger me-1', onclick: () => action() }, 'Confirmer'),
            el('button', { type: 'button', class: 'btn btn-sm btn-link', onclick: () => holder.replaceChildren(start) }, 'Annuler')) }, label);
        holder.append(start);
        return holder;
    }

    function editorFor(setting) {
        if (setting.key === 'map.default_basemap') return defaultEditor(setting);
        switch (CONTROL_FOR_TYPE[setting.type]) {
            case 'switch': return switchEditor(setting);
            case 'select': return selectEditor(setting);
            case 'number': return numberEditor(setting);
            case 'chips': return chipsEditor(setting);
            case 'basemap-cards': return basemapEditor(setting);
            case 'polygon-map': return polygonEditor(setting);
            default: return textEditor(setting);
        }
    }

    // -- ligne de réglage --------------------------------------------------------------------------------------------------
    function resetControl(setting) {
        if (setting.source !== 'base') return null;
        const toEnv = setting.fallback_source === 'env' && !setting.env_invalid;
        const target = format(setting, setting.fallback);
        const linked = LINKED_SECRETS[setting.key];
        const clears = linked && S.secrets[linked] ? ` Le secret « ${linked} » enregistré sera effacé.` : '';
        return confirmInline(`${toEnv ? "Rétablir la valeur d'environnement" : 'Rétablir la valeur par défaut'} → ${target}`,
            `Supprimer la valeur enregistrée ?${clears}`, () => resetSetting(setting), 'btn btn-sm btn-outline-secondary');
    }

    // Ligne : un en-tête (libellé, « ? », pastille, aide, description) et un éditeur (champ, rétablissement, erreurs) (EF-6.18)
    const WIDE = ['basemaps', 'polygon'];
    function row(setting) {
        const badges = [];
        if (setting.source === 'env') badges.push(el('span', { class: 'badge-soft is-warn' }, 'Environnement'));      // aucune pastille « Défaut » (EF-6.11)
        else if (setting.source === 'base') badges.push(el('span', { class: 'badge-soft is-info' }, 'Base'));
        if (setting.requires_restart) badges.push(el('span', { class: 'badge-soft is-warn' }, 'Redémarrage nécessaire'));
        if (setting.env_invalid) badges.push(el('span', { class: 'badge-soft is-bad', role: 'alert' }, `${setting.env} invalide : ignorée`));
        const helpId = `help-${setting.key}`;
        const description = setting.help ? el('p', { class: 'setting-desc', id: helpId, hidden: true }, setting.help) : null;
        const toggle = setting.help ? el('button', { type: 'button', class: 'setting-help-toggle', 'aria-expanded': 'false', 'aria-controls': helpId,
            'aria-label': `Aide : ${setting.label}`, onclick: () => {
                const open = description.hidden;
                description.hidden = !open;
                toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
            } }, '?') : null;
        const hintText = hint(setting);
        return el('div', { class: `setting-row${isDirty(setting.key) ? ' is-dirty' : ''}${WIDE.includes(setting.type) ? ' is-wide' : ''}`,
            'data-setting': setting.key, 'data-type': setting.type },
        el('div', { class: 'setting-head' },
            el('div', { class: 'setting-title' }, el('label', { class: 'setting-label', for: `f-${setting.key}` }, setting.label), toggle, ...badges),
            hintText ? el('p', { class: 'setting-hint' }, hintText) : null,
            description),
        el('div', { class: 'setting-editor' }, el('div', { class: 'setting-control' }, editorFor(setting)), resetControl(setting),
            setting.type === 'basemaps' ? null : errorLine(setting.key)));
    }

    // -- onglets, compteurs, recherche ------------------------------------------------------------------------------------
    function matches(setting) {
        if (S.onlyModified && setting.source === 'défaut') return false;
        if (!S.search) return true;
        const hay = fold(`${setting.label} ${setting.help || ''} ${setting.key} ${setting.env || ''}`);
        return hay.includes(fold(S.search));
    }
    function alertedKeys() {
        const keys = new Set(S.settings.filter((s) => s.env_invalid).map((s) => s.key));
        S.alerts.forEach((a) => {
            (ALERT_KEYS[a.service] || []).forEach((k) => keys.add(k));
            if (a.code === 'default_basemap_unavailable') keys.add('map.default_basemap');
        });
        return keys;
    }
    const plural = (n, word) => `${n} ${word}${n > 1 ? 's' : ''}`;
    function tabLabel(section) {
        const all = sectionSettings(section);
        const found = all.filter(matches).length;
        const modified = all.filter((s) => s.source !== 'défaut').length;
        const unsaved = all.some((s) => isDirty(s.key));
        const alerted = all.some((s) => alertedKeys().has(s.key));
        const searching = S.search || S.onlyModified;
        // Une seule pastille de nombre : réglages trouvés pendant une recherche, sinon réglages modifiés ; jamais de total (EF-6.9)
        const number = searching ? found : modified;
        const name = searching ? `${plural(found, 'réglage')} ${found > 1 ? 'trouvés' : 'trouvé'}` : `${plural(modified, 'réglage')} ${modified > 1 ? 'modifiés' : 'modifié'}`;
        return { found, text: [el('span', {}, section),
            number > 0 ? el('span', { class: 'tab-count', role: 'img', 'aria-label': name }, String(number)) : null,
            unsaved ? el('span', { class: 'tab-dot', role: 'img', 'aria-label': 'saisies non enregistrées' }) : null,
            alerted ? el('span', { class: 'badge-soft is-bad', role: 'img', 'aria-label': 'alerte' }, '!') : null] };
    }
    function renderTabs() {
        const bar = $('settingsTabs');
        bar.replaceChildren(...S.sections.map((section) => {
            const label = tabLabel(section);
            const selected = section === S.tab;
            return el('button', { type: 'button', role: 'tab', id: `stab-${slug(section)}`, 'aria-selected': selected ? 'true' : 'false',
                'aria-controls': 'settingsPanel', tabindex: selected ? 0 : -1, class: `settings-tab${label.found === 0 && (S.search || S.onlyModified) ? ' is-dim' : ''}`,
                'data-section': section, onclick: () => selectTab(section), onkeydown: (event) => arrow(event, section) }, ...label.text);
        }));
        const chosen = bar.querySelector('[aria-selected="true"]');          // la barre défile dans sa zone : l'onglet ouvert reste visible
        if (chosen) {
            const box = bar.getBoundingClientRect(), tab = chosen.getBoundingClientRect();
            if (tab.left < box.left) bar.scrollLeft -= box.left - tab.left;
            else if (tab.right > box.right) bar.scrollLeft += tab.right - box.right;
        }
    }
    function arrow(event, section) {
        const index = S.sections.indexOf(section);
        const move = { ArrowRight: 1, ArrowLeft: -1 }[event.key];
        if (move === undefined) return;
        event.preventDefault();
        const next = S.sections[(index + move + S.sections.length) % S.sections.length];
        selectTab(next);
        $(`stab-${slug(next)}`).focus();
    }
    function selectTab(section) {
        S.tab = section;
        S.confirm = null;
        const wanted = `#reglages/${slug(section)}`;
        if (window.location.hash !== wanted) window.history.replaceState(null, '', wanted);      // la section seulement, jamais une valeur
        renderAll();
    }

    function renderPanel() {
        S.previewMaps.forEach((m) => m.remove());
        S.previewMaps = [];
        const panel = $('settingsPanel');
        panel.setAttribute('aria-labelledby', `stab-${slug(S.tab)}`);
        const all = sectionSettings(S.tab);
        const shown = all.filter((s) => matches(s) || S.errors[s.key]);      // une ligne en erreur reste visible hors recherche/filtre (L3-1)
        const nodes = [];
        const restart = all.filter((s) => s.restart_pending);
        if (restart.length) nodes.push(el('div', { class: 'alert alert-warning', role: 'status' }, 'Valeur enregistrée, prise en compte au prochain redémarrage.'));
        if (!shown.length) {
            const elsewhere = S.sections.filter((s) => s !== S.tab && sectionSettings(s).some(matches));
            nodes.push(el('p', { class: 'hint' }, 'Aucun réglage ne correspond.'),
                ...(elsewhere.length ? [el('p', { class: 'hint' }, 'Résultats dans : ', ...elsewhere.flatMap((s) => [el('button', { type: 'button', class: 'btn btn-link p-0',
                    onclick: () => selectTab(s) }, s), ' ']))] : []));
        }
        nodes.push(...shown.map(row));
        panel.replaceChildren(...nodes);
        updateChrome();
    }

    function updateChrome() {
        renderTabs();
        const dirtyHere = sectionSettings(S.tab).filter((s) => isDirty(s.key));
        const save = $('settingsSave');
        if (save) { save.disabled = !dirtyHere.length; $('settingsCancel').disabled = !dirtyHere.length; }
        document.querySelectorAll('.setting-row').forEach((rowNode) => rowNode.classList.toggle('is-dirty', isDirty(rowNode.dataset.setting)));
        $('settingsVersion').textContent = S.version === null ? '' : `Version des réglages : ${S.version}`;
        window.onbeforeunload = Object.keys(S.edits).length ? (event) => { event.preventDefault(); return ''; } : null;
    }
    function renderAlerts() {
        $('settingsAlerts').replaceChildren(...S.alerts.map((a) => el('div', { class: 'alert alert-warning', role: 'alert' }, a.message)));
    }
    function renderAll() {
        renderAlerts();
        renderConfirm();
        renderPanel();
    }

    // -- enregistrement par onglet ----------------------------------------------------------------------------------------
    function basemapSummary(before, after) {
        const lines = [];
        const names = (list) => list.map((b) => b.name);
        names(after).filter((n) => !names(before).includes(n)).forEach((n) => lines.push(`ajouté : ${n}`));
        names(before).filter((n) => !names(after).includes(n)).forEach((n) => lines.push(`supprimé : ${n}`));
        const common = names(after).filter((n) => names(before).includes(n));
        if (!same(common, names(before).filter((n) => common.includes(n)))) lines.push('ordre modifié');
        common.forEach((n) => {
            const a = before.find((b) => b.name === n), b = after.find((x) => x.name === n);
            const fields = ['url', 'subdomains', 'attribution', 'max_zoom'].filter((f) => !same(a[f], b[f]));
            if (fields.length) lines.push(`modifié : ${n} (${fields.join(', ')})`);
        });
        return lines.join(' ; ') || 'aucun changement de fond';
    }
    function changesOfTab() {
        return sectionSettings(S.tab).filter((s) => isDirty(s.key));
    }
    function focusError(key, index) {
        const target = document.querySelector(`[data-setting="${CSS.escape(key)}"]`);
        if (!target) return;
        const field = index !== undefined && index !== null ? target.querySelector(`[data-index="${index}"] input`) : target.querySelector('input, select');
        if (field) { field.setAttribute('aria-invalid', 'true'); field.focus(); }
    }
    function openConfirm() {
        const first = validateTab(S.tab);
        if (first) {
            renderPanel();
            focusError(first, S.errors[first] && S.errors[first].index);
            return;
        }
        S.confirm = { message: null };
        renderConfirm();
    }
    function renderConfirm() {
        const box = $('settingsConfirm');
        if (!S.confirm) { box.hidden = true; box.replaceChildren(); return; }
        const items = changesOfTab().map((setting) => {
            const before = setting.type === 'basemaps' ? basemapSummary(loadedValue(setting.key), current(setting.key))
                : `${format(setting, loadedValue(setting.key))} (${originOf(setting)})`;
            const after = setting.type === 'basemaps' ? '' : format(setting, current(setting.key));
            const extras = [];
            const linked = LINKED_SECRETS[setting.key];
            if (linked && S.secrets[linked]) extras.push(`le secret « ${linked} » sera effacé`);
            if (setting.requires_restart) extras.push('pris en compte au prochain redémarrage');
            return el('li', {}, el('strong', {}, setting.label), ' : ', setting.type === 'basemaps' ? before : `${before} → ${after}`,
                extras.length ? el('span', { class: 'hint' }, ` — ${extras.join(' ; ')}`) : null);
        });
        box.hidden = false;
        box.replaceChildren(...[el('p', { class: 'mb-1' }, `Enregistrer ${items.length} réglage(s) de « ${S.tab} » ?`), el('ul', { class: 'mb-2' }, items),
            S.confirm.message ? el('p', { class: 'setting-error', role: 'alert' }, S.confirm.message) : null,
            S.confirm.conflict ? el('button', { type: 'button', class: 'btn btn-sm btn-warning me-1', onclick: reloadKeepingEdits }, 'Recharger')
                : el('button', { type: 'button', class: 'btn btn-sm btn-danger me-1', id: 'settingsConfirmYes', onclick: save }, 'Confirmer'),
            el('button', { type: 'button', class: 'btn btn-sm btn-secondary', id: 'settingsConfirmNo', onclick: () => { S.confirm = null; renderConfirm(); } }, 'Annuler')]
            .filter(Boolean));
    }

    async function save() {
        const changed = changesOfTab();
        const payload = Object.fromEntries(changed.map((s) => [s.key, current(s.key)]));
        try {
            const reply = await api('/api/admin/settings', { method: 'PATCH', body: { settings: payload, version: S.version } });
            changed.forEach((s) => delete S.edits[s.key]);
            S.confirm = null;
            await fetchSettings();                      // la version du GET fait foi : celle de la réponse ne peut que reculer (L3-n1)
            notify(`${changed.length} réglage(s) enregistré(s).`);
            renderAll();
        } catch (error) {
            const data = error.data || {};
            if (data.code === 'settings_version_conflict') {
                S.confirm = { message: "Un autre admin a modifié ces réglages : recharge la page avant d'enregistrer.", conflict: true };
            } else if (data.field && S.byKey[data.field]) {
                S.errors[data.field] = { message: error.message, index: data.index };
                S.confirm = null;
                renderAll();
                focusError(data.field, data.index);
                return;
            } else {
                S.confirm = { message: error.message };
            }
            renderConfirm();
        }
    }
    async function reloadKeepingEdits() {
        S.confirm = null;
        await fetchSettings();
        renderAll();
        notify('Valeurs rechargées ; tes saisies sont conservées.');
    }
    async function resetSetting(setting) {
        try {
            await api(`/api/admin/settings/${encodeURIComponent(setting.key)}`, { method: 'DELETE' });
            delete S.edits[setting.key];
            await fetchSettings();
            notify('Valeur enregistrée supprimée.');
            renderAll();
        } catch (error) { notify(error.message, 'danger'); }
    }
    function cancelEdits() {
        sectionSettings(S.tab).forEach((s) => { delete S.edits[s.key]; delete S.errors[s.key]; delete S.polygonIssues[s.key]; });
        S.confirm = null;
        renderAll();
    }

    // -- chargement -------------------------------------------------------------------------------------------------------
    async function fetchSettings() {
        const data = await api('/api/admin/settings');
        S.settings = data.settings.filter((s) => !s.secret);
        S.byKey = Object.fromEntries(data.settings.map((s) => [s.key, s]));
        S.sections = (data.sections || []).filter((name) => S.settings.some((s) => s.section === name));
        S.version = data.version;
        Object.keys(S.edits).forEach((key) => { if (!S.byKey[key] || same(S.edits[key], S.byKey[key].value)) delete S.edits[key]; });   // saisie redevenue égale : non modifiée (L3-n5)
        S.alerts = data.alerts || [];
        try {
            const info = await api('/api/admin/secrets');
            S.secrets = info.disabled ? {} : Object.fromEntries(info.secrets.map((x) => [x.name, !!x.configured]));
        } catch (_) { S.secrets = {}; }
        S.loaded = true;
        S.stale = false;
    }

    async function open(sub) {
        try {
            if (!S.loaded || S.stale) await fetchSettings();
        } catch (error) { notify(error.message, 'danger'); return; }
        const wanted = S.sections.find((s) => slug(s) === sub);
        S.tab = wanted || (S.sections.includes(S.tab) ? S.tab : S.sections[0]);
        renderAll();
    }
    function leave() { S.stale = true; }

    function setup() {
        $('settingsSearch').addEventListener('input', (event) => { S.search = event.target.value; renderAll(); });
        $('settingsOnlyModified').addEventListener('change', (event) => { S.onlyModified = event.target.checked; renderAll(); });
        $('settingsSave').addEventListener('click', openConfirm);
        $('settingsCancel').addEventListener('click', cancelEdits);
    }

    return { CONTROL_FOR_TYPE, open, leave, setup, hasUnsaved: () => Object.keys(S.edits).length > 0 };
})();
