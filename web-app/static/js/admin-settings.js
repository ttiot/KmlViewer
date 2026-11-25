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
        timezone: 'text', list: 'chips', basemaps: 'basemap-cards', rectangle: 'rectangle-fields' };
    const MAX_RECTANGLE_SIDE_M = 2_000_000;
    const MARKUP = /[<>&"']/;
    const LINKED_SECRETS = { 'smtp.host': 'smtp.password', 'smtp.port': 'smtp.password', 'routing.base_url': 'routing.api_key' };
    const ALERT_KEYS = { smtp: ['smtp.host', 'smtp.port'], routing: ['routing.base_url'], dem: ['DEM_URL'], tiles: ['map.basemaps'] };

    const S = {
        settings: [], byKey: {}, sections: [], version: null, alerts: [], secrets: {}, edits: {}, errors: {}, tab: null, search: '',
        jsonViews: {}, onlyModified: false, loaded: false, stale: true, confirm: null, previewMap: null, registerUnload: false,
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
            case 'rectangle': return Array.isArray(value) ? `lon ${value[0]} → ${value[2]}, lat ${value[1]} → ${value[3]}` : '—';
            case 'list': return (value || []).join(', ') || '—';
            default: return value === '' || value === null || value === undefined ? '(vide)' : String(value);
        }
    }
    const originOf = (setting) => (setting.source === 'env' ? `Environnement · ${setting.env}` : setting.source === 'base' ? 'Base' : 'Défaut');

    function hint(setting) {
        const parts = [];
        if (setting.type === 'int' || setting.type === 'float') parts.push(`${setting.min} à ${setting.max}${setting.unit ? ` ${setting.unit}` : ''}`);
        else if (['str', 'email', 'url', 'timezone'].includes(setting.type) && setting.max_length) parts.push(`${setting.max_length} caractères au plus`);
        else if (setting.type === 'basemaps') parts.push(`${setting.max_items} fonds au plus`);
        else if (setting.type === 'list') parts.push(`${setting.max_items} éléments au plus`);
        if (setting.env) parts.push(`variable ${setting.env}`);
        return parts.join(' · ');
    }

    // -- géométrie (même calcul que le serveur : haversine, largeur au parallèle le plus proche de l'équateur) -----------------
    function haversine(lat1, lon1, lat2, lon2) {
        const rad = Math.PI / 180, dLat = (lat2 - lat1) * rad, dLon = (lon2 - lon1) * rad;
        const a = Math.sin(dLat / 2) ** 2 + Math.cos(lat1 * rad) * Math.cos(lat2 * rad) * Math.sin(dLon / 2) ** 2;
        return 2 * 6371008.8 * Math.asin(Math.min(1, Math.sqrt(a)));
    }
    function rectangleSides(box) {
        const [lonMin, latMin, lonMax, latMax] = box;
        const parallel = latMin <= 0 && latMax >= 0 ? 0 : Math.min(Math.abs(latMin), Math.abs(latMax));
        return { width: haversine(parallel, lonMin, parallel, lonMax), height: haversine(latMin, lonMin, latMax, lonMin) };
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
    function validateRectangle(box) {
        if (!Array.isArray(box) || box.length !== 4 || box.some((v) => typeof v !== 'number' || !Number.isFinite(v))) return { message: 'Quatre nombres sont attendus.' };
        const [lonMin, latMin, lonMax, latMax] = box;
        if (!(lonMin >= -180 && lonMax <= 180 && latMin >= -90 && latMax <= 90)) return { message: 'Longitudes dans [-180, 180], latitudes dans [-90, 90].' };
        if (!(lonMin < lonMax && latMin < latMax)) return { message: 'Le minimum doit être inférieur au maximum.' };
        const { width, height } = rectangleSides(box);
        if (Math.max(width, height) > MAX_RECTANGLE_SIDE_M) return { message: 'Côtés de 2 000 km au plus.' };
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
            case 'rectangle': return validateRectangle(value);
            case 'list': return (value || []).length > setting.max_items ? { message: `${setting.max_items} éléments au plus.` } : null;
            case 'enum': return setting.choices.includes(value) ? null : { message: 'Valeur non permise.' };
            case 'str': case 'email': case 'url': case 'timezone':
                return typeof value === 'string' && value.length <= (setting.max_length || 1e9) ? null : { message: `${setting.max_length} caractères au plus.` };
            default: return null;
        }
    }
    function validateTab(section) {
        let first = null;
        sectionSettings(section).filter((s) => isDirty(s.key)).forEach((setting) => {
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

    function rectangleEditor(setting) {
        const holder = el('div', { class: 'rectangle-editor' });
        const labels = ['Longitude min', 'Latitude min', 'Longitude max', 'Latitude max'];
        const dims = el('p', { class: 'hint mb-1' });
        const preview = el('div', { class: 'rect-preview', 'aria-label': 'Aperçu du rectangle' });
        const problem = el('p', { class: 'setting-error', role: 'alert', hidden: true });
        const bounds = [[-180, 180], [-90, 90], [-180, 180], [-90, 90]];
        const inputs = [];
        const refresh = () => {
            const box = current(setting.key);
            const issue = validateRectangle(box);
            problem.textContent = issue ? issue.message : '';
            problem.hidden = !issue;
            if (!issue) {
                const { width, height } = rectangleSides(box);
                dims.textContent = `≈ ${Math.round(width / 1000)} km × ${Math.round(height / 1000)} km`;
                drawPreview(preview, box);
            } else dims.textContent = '';
        };
        const fields = labels.map((label, i) => {
            const input = el('input', { type: 'number', class: 'form-control', id: `rect-${i}`, min: bounds[i][0], max: bounds[i][1], step: 0.0001,
                value: current(setting.key)[i], 'aria-label': label,
                oninput: () => { const box = current(setting.key).slice(); box[i] = input.value === '' ? NaN : Number(input.value); setEdit(setting.key, box); refresh(); } });
            inputs.push(input);
            return el('div', { class: 'field' }, el('label', { for: `rect-${i}` }, label), input);
        });
        holder.append(el('div', { class: 'field-grid' }, fields), dims, problem, preview,
            advancedPane(setting, () => { inputs.forEach((input, i) => { input.value = current(setting.key)[i]; }); refresh(); }));
        setTimeout(refresh, 0);
        return holder;
    }

    function drawPreview(container, box) {
        if (typeof L === 'undefined') return;
        if (S.previewMap) { S.previewMap.remove(); S.previewMap = null; }
        container.replaceChildren();
        const map = L.map(container, { zoomControl: false, attributionControl: false });
        S.previewMap = map;
        const maps = S.byKey['map.basemaps'];
        const wanted = S.byKey['map.default_basemap'];
        const base = maps && wanted && (maps.value || []).find((b) => b.name === wanted.value);
        if (base && tileHosts(base, maps.allowlist || []).every((h) => h.ok)) {      // fond par défaut seulement s'il est autorisé
            App.basemapLayer(base).addTo(map);
        }
        const rectangle = L.rectangle([[box[1], box[0]], [box[3], box[2]]], { color: '#0d6efd', weight: 2, fill: false }).addTo(map);
        map.fitBounds(rectangle.getBounds(), { padding: [10, 10] });
    }

    // Volet « Avancé » : JSON brut de la valeur EN COURS DE SAISIE (basemaps et rectangle seulement)
    function advancedPane(setting, redraw) {
        const area = el('textarea', { class: 'form-control mono', rows: setting.type === 'rectangle' ? 2 : 8, 'aria-label': `JSON de ${setting.label}` },
            JSON.stringify(current(setting.key), null, 2));
        const problem = el('p', { class: 'setting-error', role: 'alert', hidden: true });
        S.jsonViews[setting.key] = area;
        const pane = el('details', { class: 'advanced' });
        const apply = () => {
            let parsed;
            try { parsed = JSON.parse(area.value); } catch (_) { problem.textContent = 'JSON invalide.'; problem.hidden = false; return; }
            const shape = setting.type === 'rectangle'
                ? Array.isArray(parsed) && parsed.length === 4 && parsed.every((v) => typeof v === 'number')
                : Array.isArray(parsed) && parsed.every((b) => b && typeof b === 'object' && !Array.isArray(b));
            if (!shape) { problem.textContent = 'Forme inattendue.'; problem.hidden = false; return; }
            problem.hidden = true;
            setEdit(setting.key, setting.type === 'basemaps' ? parsed.map((b) => ({ subdomains: 'abc', attribution: '', max_zoom: 19, ...b })) : parsed);
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
            case 'rectangle-fields': return rectangleEditor(setting);
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

    function row(setting) {
        const origin = originOf(setting);
        const badges = [el('span', { class: `badge-soft ${setting.source === 'base' ? 'is-info' : setting.source === 'env' ? 'is-warn' : ''}` }, origin)];
        if (setting.env_overridden) badges.push(el('span', { class: 'hint' }, `remplace ${setting.env}`));
        if (setting.requires_restart) badges.push(el('span', { class: 'badge-soft is-warn' }, 'Redémarrage nécessaire'));
        if (setting.env_invalid) badges.push(el('span', { class: 'badge-soft is-bad', role: 'alert' }, `${setting.env} invalide : ignorée`));
        const help = setting.help ? el('details', { class: 'setting-help' }, el('summary', { 'aria-label': `Aide : ${setting.label}` }, '?'), el('p', { class: 'hint mb-0' }, setting.help)) : null;
        return el('div', { class: `setting-row${isDirty(setting.key) ? ' is-dirty' : ''}`, 'data-setting': setting.key },
            el('div', { class: 'setting-head' }, el('label', { class: 'setting-label', for: `f-${setting.key}` }, setting.label), ...badges, help, resetControl(setting)),
            editorFor(setting), el('p', { class: 'hint setting-hint' }, hint(setting)), setting.type === 'basemaps' ? null : errorLine(setting.key));
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
    function tabLabel(section) {
        const all = sectionSettings(section);
        const found = all.filter(matches).length;
        const modified = all.filter((s) => s.source !== 'défaut').length;
        const unsaved = all.some((s) => isDirty(s.key));
        const alerted = all.some((s) => alertedKeys().has(s.key));
        const searching = S.search || S.onlyModified;
        const count = `${searching ? `${found} / ` : ''}${all.length}`;
        return { found, text: [el('span', {}, section), el('span', { class: 'tab-count' }, count),
            modified ? el('span', { class: 'tab-count' }, `${modified} modifié${modified > 1 ? 's' : ''}`) : null,
            unsaved ? el('span', { class: 'badge-soft is-warn' }, 'non enregistré') : null,
            alerted ? el('span', { class: 'badge-soft is-bad', title: 'Alerte sur un réglage' }, '!') : null] };
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
        if (S.previewMap) { S.previewMap.remove(); S.previewMap = null; }
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
        sectionSettings(S.tab).forEach((s) => { delete S.edits[s.key]; delete S.errors[s.key]; });
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
