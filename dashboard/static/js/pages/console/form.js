/**
 * Console RPC — volet de configuration : protocole, procédure, paramètres générés, mode
 * d'exécution, options réseau et politique de résilience, puis le bouton « Exécuter ».
 */

import { h, clear } from '../../core/dom.js';
import { icon } from '../../core/icons.js';
import { fmtNumber } from '../../core/format.js';
import { describeNetwork } from '../../core/network.js';
import { PROTOCOL_IDS, protocol } from '../../core/protocols.js';
import { Button, Card, Field, NumberInput, Segmented, Toggle } from '../../components/ui.js';
import { COUNT_MIN, KINDS, MODES, allowedModes, methodSpec, normalize, unavailableReason, validate } from './model.js';
import { ParamForm } from './params.js';
import { IdempotenceBadge, KindBadge, ProcedurePicker } from './picker.js';

function label(text, aside) {
  return h('div.console-label', h('span.t-label', text), aside ? h('span.console-label__aside', aside) : null);
}

/**
 * Adresse visée par un protocole : serveur en direct, ou proxy de chaos.
 * @param {Object|null} status Réponse de `GET /api/status`.
 * @param {string} protocolId
 * @param {boolean} viaProxy
 * @returns {{host: string, port: number|null}}
 */
export function endpointOf(status, protocolId, viaProxy) {
  const server = status?.servers?.find((entry) => entry.id === protocolId);
  if (!server) return { host: '127.0.0.1', port: protocol(protocolId).port };
  return { host: server.host, port: viaProxy && server.proxy_port ? server.proxy_port : server.port };
}

/**
 * Volet de configuration de la console.
 * @param {Object} props
 * @param {import('./model.js').ConsoleConfig} props.config Configuration (modifiée en place).
 * @param {Object} props.catalog Réponse de `GET /api/catalog`.
 * @param {() => {status: Object|null, network: Object|null}} props.getLab État courant du laboratoire.
 * @param {() => void} props.onChange Après chaque modification de la configuration.
 * @param {() => void} props.onRun Clic sur « Exécuter ».
 * @returns {HTMLElement & {sync: () => void, refreshLab: () => void, setBusy: (label: string|null) => void, setProducts: (products: Object[]) => void, isValid: () => boolean, destroy: () => void}}
 */
export function ConfigForm({ config, catalog, getLab, onChange, onRun }) {
  const limits = catalog.limits ?? {};
  let paramForm = null;
  let errors = {};

  /* --- Protocole --------------------------------------------------------------------- */
  const protocolControl = Segmented({
    value: config.protocol,
    block: true,
    ariaLabel: 'Protocole',
    options: PROTOCOL_IDS.map((id) => ({ value: id, label: protocol(id).short, color: protocol(id).color, title: protocol(id).label })),
    onChange: (id) => {
      config.protocol = id;
      if (unavailableReason(methodSpec(catalog, config.method), id)) {
        config.method = methodSpec(catalog, 'update_stock')?.name ?? catalog.methods.find((spec) => spec.protocols.includes(id))?.name ?? config.method;
      }
      changed({ rebuild: true });
    },
  });
  const transport = h('p.console-transport');

  /* --- Procédure ----------------------------------------------------------------------- */
  const picker = ProcedurePicker({
    methods: catalog.methods,
    value: config.method,
    protocol: config.protocol,
    onChange: (name) => {
      config.method = name;
      changed({ rebuild: true });
    },
  });
  const about = h('div.console-about');
  const paramsSlot = h('div.console-slot');

  /* --- Mode ---------------------------------------------------------------------------- */
  const modeSlot = h('div.console-mode');
  const countInput = NumberInput({
    value: config.count,
    min: COUNT_MIN,
    max: limits.async_calls_max ?? 200,
    step: 1,
    width: 112,
    ariaLabel: 'Nombre d’appels lancés ensemble',
    onChange: (value) => {
      config.count = value;
      changed();
    },
  });
  const countField = h('div.console-count', h('span.console-count__label', 'N ='), countInput, h('span.console-count__hint', `appels lancés ensemble (${COUNT_MIN} à ${fmtNumber(limits.async_calls_max ?? 200)})`));
  const modeHint = h('p.console-hint');

  /* --- Options --------------------------------------------------------------------------- */
  const proxyToggle = Toggle({
    checked: config.viaProxy,
    ariaLabel: 'Via le proxy de chaos',
    onChange: (on) => {
      config.viaProxy = on;
      changed();
    },
  });
  const proxyField = Field({ inline: true, label: 'Via le proxy de chaos', control: proxyToggle, hint: ' ' });
  const timeoutInput = NumberInput({
    value: config.timeoutMs,
    min: 50,
    max: limits.timeout_ms_max ?? 60000,
    step: 250,
    suffix: 'ms',
    width: 148,
    ariaLabel: 'Échéance de chaque appel',
    onChange: (value) => {
      config.timeoutMs = value;
      changed();
    },
  });
  const timeoutField = Field({ inline: true, label: 'Échéance', control: timeoutInput, hint: 'Au-delà, le client abandonne : erreur TIMEOUT.' });

  /* --- Politique de résilience ------------------------------------------------------------- */
  const policyToggle = Toggle({
    checked: config.policy.enabled,
    ariaLabel: 'Politique de résilience',
    onChange: (on) => {
      config.policy.enabled = on;
      changed();
    },
  });
  const policySummary = h('span.console-policy__summary');
  const attemptsInput = NumberInput({ value: config.policy.max_attempts, min: 1, max: 10, width: 112, ariaLabel: 'Tentatives au total', onChange: (value) => setPolicy('max_attempts', value) });
  const backoffInput = NumberInput({ value: config.policy.base_delay_ms, min: 0, max: 10000, step: 50, suffix: 'ms', width: 148, ariaLabel: 'Attente de base', onChange: (value) => setPolicy('base_delay_ms', value) });
  const breakerToggle = Toggle({ checked: config.policy.breaker, ariaLabel: 'Disjoncteur', onChange: (on) => setPolicy('breaker', on) });
  const keyToggle = Toggle({ checked: config.policy.auto_idempotency_key, ariaLabel: 'Clé d’idempotence automatique', onChange: (on) => setPolicy('auto_idempotency_key', on) });
  const policyBody = h(
    'div.console-policy__body',
    Field({ inline: true, label: 'Tentatives au total', control: attemptsInput, hint: 'La première comprise ; seuls TIMEOUT et UNAVAILABLE sont rejoués.' }),
    Field({ inline: true, label: 'Attente de base', control: backoffInput, hint: 'Doublée à chaque échec (recul exponentiel).' }),
    Field({ inline: true, label: 'Disjoncteur', control: breakerToggle, hint: 'Après 3 échecs de suite, refuse les appels sans toucher au réseau.' }),
    Field({ inline: true, label: 'Clé d’idempotence automatique', control: keyToggle, hint: 'Un update_stock rejoué n’applique pas son effet deux fois.' }),
  );
  const policyButton = h(
    'button.console-policy__head',
    { type: 'button', 'aria-expanded': 'false', onClick: () => togglePolicy() },
    h('span.console-policy__chevron', { 'aria-hidden': 'true' }, icon('chevron-right', { size: 14 })),
    h('span.console-policy__title', 'Politique de résilience'),
    policySummary,
  );
  const policy = h('div.console-policy', h('div.console-policy__bar', policyButton, policyToggle), policyBody);
  let policyOpen = config.policy.enabled;

  function setPolicy(key, value) {
    config.policy[key] = value;
    changed();
  }

  function togglePolicy(open = !policyOpen) {
    policyOpen = open;
    policy.dataset.open = String(policyOpen);
    policyButton.setAttribute('aria-expanded', String(policyOpen));
    policyBody.hidden = !policyOpen;
  }

  /* --- Exécution --------------------------------------------------------------------------- */
  const runButton = Button({ label: 'Exécuter', variant: 'primary', size: 'lg', icon: 'play', kbd: 'mod+enter', block: true, onClick: () => onRun() });
  const runNote = h('p.console-run__note', { 'aria-live': 'polite', dataset: { tone: 'danger' } });
  const footer = h('div.console-run', runButton, runNote);

  const el = Card(
    { title: 'Requête', subtitle: 'Quoi appeler, par quel middleware, et comment', icon: 'sliders-horizontal', accent: protocol(config.protocol).color, footer, class: 'console-form' },
    h('div.console-group', label('Protocole'), protocolControl, transport),
    h('div.console-group', label('Procédure', `${catalog.methods.length} au catalogue`), picker, about),
    h('div.console-group', label('Paramètres'), paramsSlot),
    h('div.console-group', label('Mode d’exécution'), modeSlot, countField, modeHint),
    h('div.console-group', label('Options'), proxyField, timeoutField, policy),
  );

  /* --- Rendu ------------------------------------------------------------------------------- */
  function renderAbout(spec) {
    clear(
      about,
      h('div.console-about__badges', KindBadge(spec.kind), IdempotenceBadge(spec.idempotent)),
      h('p.console-about__text', spec.description),
      h(
        'dl.console-about__routes',
        h('dt', 'gRPC'),
        h('dd.mono', `InventoryService/${spec.grpc_method}`),
        h('dt', 'REST'),
        h('dd.mono', spec.rest_route && spec.rest_route !== '—' ? spec.rest_route : 'aucune route'),
      ),
    );
  }

  function renderMode(spec) {
    const allowed = allowedModes(spec);
    const reason = spec.kind === 'unary' ? 'Réservé aux procédures en flux.' : `Un ${KINDS[spec.kind]?.label ?? 'flux'} ne se lance qu’en mode flux.`;
    clear(
      modeSlot,
      Segmented({
        value: config.mode,
        block: true,
        ariaLabel: 'Mode d’exécution',
        options: Object.entries(MODES).map(([id, mode]) => ({ value: id, label: mode.label, icon: mode.icon, disabled: !allowed.includes(id), title: allowed.includes(id) ? undefined : reason })),
        onChange: (mode) => {
          config.mode = mode;
          changed();
        },
      }),
    );
  }

  function renderParams(spec) {
    paramForm = ParamForm({ spec, values: config.values[spec.name], catalog, onChange: () => changed() });
    clear(paramsSlot, paramForm);
  }

  function refresh() {
    const spec = methodSpec(catalog, config.method);
    const info = protocol(config.protocol);
    const { status, network } = getLab();
    const endpoint = endpointOf(status, config.protocol, config.viaProxy);

    el.style.setProperty('--edge', info.color);
    clear(transport, h('span.console-transport__text', info.transport), endpoint.port ? h('span.console-transport__addr.mono', `${endpoint.host}:${endpoint.port}`) : h('span.console-transport__addr.mono', 'en mémoire'));

    countField.hidden = config.mode !== 'async';
    clear(
      modeHint,
      {
        sync: 'Un appel bloquant : le client attend la réponse avant de continuer.',
        async: `Les ${fmtNumber(config.count)} appels partent ensemble : on compare le temps réel à la somme des durées.`,
        stream: `${KINDS[spec.kind]?.text ?? ''} Les éléments s’affichent à mesure qu’ils arrivent.`,
      }[config.mode],
    );

    const isLocal = config.protocol === 'local';
    proxyToggle.setChecked(config.viaProxy && !isLocal);
    proxyToggle.setDisabled(isLocal);
    const conditions = describeNetwork(network);
    proxyField.setHint(
      isLocal
        ? 'Sans objet : un appel local ne traverse aucun réseau.'
        : h('span', config.viaProxy ? 'Réseau simulé traversé : ' : 'Appel direct. Réseau simulé actuel : ', h('a.console-link', { href: '#/chaos' }, conditions.label), conditions.detail ? ` — ${conditions.detail}` : ''),
    );

    const active = config.policy.enabled;
    policy.dataset.enabled = String(active);
    for (const control of [attemptsInput, backoffInput, breakerToggle, keyToggle]) control.setDisabled(!active);
    clear(
      policySummary,
      active
        ? [`${config.policy.max_attempts} tentative${config.policy.max_attempts > 1 ? 's' : ''}`, `${fmtNumber(config.policy.base_delay_ms)}\u202Fms`, config.policy.breaker ? 'disjoncteur' : null, config.policy.auto_idempotency_key ? 'clé auto' : null].filter(Boolean).join(' · ')
        : 'désactivée : une seule tentative',
    );

    errors = validate(spec, config.values[spec.name]);
    paramForm?.setErrors(errors);
    const count = Object.keys(errors).length;
    runButton.setDisabled(count > 0 || busy !== null);
    clear(runNote, count ? `${count} paramètre${count > 1 ? 's' : ''} à corriger avant l’envoi.` : '');
    runNote.hidden = count === 0;
  }

  let busy = null;

  function changed({ rebuild = false } = {}) {
    if (config.policy.enabled && !policyOpen) togglePolicy(true);
    normalize(config, catalog);
    if (rebuild) rebuildAll();
    else refresh();
    onChange();
  }

  function rebuildAll() {
    const spec = methodSpec(catalog, config.method);
    protocolControl.setValue(config.protocol);
    picker.set({ value: config.method, protocol: config.protocol });
    renderAbout(spec);
    renderParams(spec);
    renderMode(spec);
    countInput.setValue(config.count);
    timeoutInput.setValue(config.timeoutMs);
    policyToggle.setChecked(config.policy.enabled);
    attemptsInput.setValue(config.policy.max_attempts);
    backoffInput.setValue(config.policy.base_delay_ms);
    breakerToggle.setChecked(config.policy.breaker);
    keyToggle.setChecked(config.policy.auto_idempotency_key);
    refresh();
  }

  /** Recharge tous les contrôles depuis la configuration (après un rechargement depuis l'historique). */
  el.sync = () => {
    normalize(config, catalog);
    togglePolicy(config.policy.enabled);
    rebuildAll();
  };
  /** Rafraîchit ce qui dépend de l'état du laboratoire (adresses, réseau simulé). */
  el.refreshLab = () => refresh();
  el.setBusy = (text) => {
    busy = text;
    runButton.setLoading(text !== null);
    runButton.setLabel(text ?? 'Exécuter');
    refresh();
  };
  el.setProducts = (products) => {
    catalog.products = products;
    paramForm?.setProducts(products);
  };
  el.isValid = () => Object.keys(errors).length === 0;
  el.destroy = () => picker.close();

  togglePolicy(policyOpen);
  rebuildAll();
  return el;
}
