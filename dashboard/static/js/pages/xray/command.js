/**
 * Sous le capot — barre de commande : procédure, paramètres générés depuis le catalogue,
 * protocoles à comparer, passage par le proxy de chaos et bouton « Inspecter ».
 */

import { h, clear } from '../../core/dom.js';
import { describeNetwork } from '../../core/network.js';
import { REMOTE_PROTOCOL_IDS, protocol } from '../../core/protocols.js';
import { Badge, Button, Card, Chip, Field, Input, NumberInput, Select, Toggle, Tooltip } from '../../components/ui.js';

/** Appel proposé au premier affichage : une écriture, pour que requête et réponse aient du contenu. */
const DEFAULT_METHOD = 'update_stock';
const DRAW = 3;
const DEFAULT_OVERRIDES = { delta: -DRAW };
const RESTOCK = 100;

/** Référence absente de l'inventaire : une « SKU-xxxx » que le catalogue ne contient pas. */
function unknownProduct(products) {
  const known = new Set(products.map((product) => product.id));
  for (let n = 9999; n > 0; n -= 1) if (!known.has(`SKU-${n}`)) return `SKU-${n}`;
  return 'SKU-INCONNU';
}

/**
 * Barre de commande de l'inspection.
 * @param {Object} props
 * @param {Object} props.catalog Réponse de `GET /api/catalog`.
 * @param {{get: Function, subscribe: Function}} props.store Magasin de l'application (conditions réseau).
 * @param {(request: {method: string, params: Object, protocols: string[], via_proxy: boolean}) => void} props.onRun
 * @param {(message: string) => void} [props.onHint] Remarque à afficher (dernier protocole décoché…).
 * @returns {HTMLElement & {value: () => Object, setBusy: (on: boolean) => void, setRequest: (method: string, params?: Object) => boolean,
 *   includeProtocol: (id: string) => boolean, canRun: () => boolean, destroy: () => void}}
 *   `includeProtocol` coche un protocole (faux si la procédure ne le propose pas).
 */
export function CommandBar({ catalog, store, onRun, onHint }) {
  const methods = catalog.methods.filter((method) => method.kind === 'unary' && method.protocols.some((id) => REMOTE_PROTOCOL_IDS.includes(id)));
  const products = catalog.products ?? [];
  const missing = unknownProduct(products);
  /** Stocks à jour : le magasin recharge le catalogue après chaque écriture. */
  const inventory = () => store.get().catalog?.products ?? products;
  /** Produit capable d'absorber une sortie de `needed` unités : celui proposé par le catalogue, sinon le mieux fourni. */
  const stocked = (preferred, needed) => {
    const list = inventory();
    const wanted = list.find((product) => product.id === preferred);
    if (wanted && wanted.stock >= needed) return wanted.id;
    return [...list].sort((a, b) => b.stock - a.stock)[0]?.id ?? preferred;
  };
  const scarce = () => [...inventory()].sort((a, b) => a.stock - b.stock)[0]?.id;
  const byName = new Map(methods.map((method) => [method.name, method]));

  let method = byName.get(DEFAULT_METHOD) ?? methods[0];
  let controls = new Map();
  const chips = new Map();
  const selected = new Set(REMOTE_PROTOCOL_IDS);
  let busy = false;

  const paramSlot = h('div.xr-command__params');
  const chipSlot = h('div.xr-command__chips', { role: 'group', 'aria-label': 'Protocoles à inspecter' });

  const methodSelect = Select({
    options: methods.map((item) => ({ value: item.name, label: item.name, description: item.title })),
    value: method?.name,
    width: 214,
    ariaLabel: 'Procédure à appeler',
    onChange: (name) => {
      method = byName.get(name);
      renderParams({});
      renderChips();
    },
  });

  const runButton = Button({ label: 'Inspecter', variant: 'primary', icon: 'scan-search', kbd: 'mod+enter', type: 'submit' });
  const network = h('span.xr-command__net');
  const proxy = Toggle({
    label: 'Via le proxy de chaos',
    size: 'sm',
    onChange: () => renderNetwork(),
  });
  Tooltip(proxy, 'L’appel traverse le proxy : latence, pertes et coupures réglées dans « Chaos réseau » s’appliquent.');

  function controlFor(param, value) {
    if (param.type === 'int') {
      return NumberInput({ value: Number(value), min: param.minimum ?? -Infinity, max: param.maximum ?? Infinity, width: 112, ariaLabel: param.description });
    }
    if (param.type === 'product_id') {
      const options = products.map((product) => ({ value: product.id, label: product.id, description: product.name }));
      options.push({ value: missing, label: missing, description: 'Référence inconnue : provoque une erreur' });
      if (!options.some((option) => option.value === value)) options.push({ value, label: String(value) });
      return Select({ options, value, width: 150, ariaLabel: param.description });
    }
    if (param.type === 'category') {
      const options = [{ value: '', label: 'Toutes' }, ...(catalog.categories ?? []).map((name) => ({ value: name, label: name }))];
      return Select({ options, value, width: 150, ariaLabel: param.description });
    }
    return Input({ value: String(value ?? ''), mono: true, width: 150, placeholder: 'facultatif', ariaLabel: param.description, onEnter: () => submit() });
  }

  function renderParams(values) {
    controls = new Map();
    const fields = (method?.params ?? []).map((param) => {
      let initial = values[param.name] ?? (method.name === DEFAULT_METHOD ? DEFAULT_OVERRIDES[param.name] : undefined) ?? param.default;
      // Une écriture par protocole : le produit proposé doit avoir de quoi les absorber toutes.
      if (param.type === 'product_id' && values[param.name] === undefined && method.idempotent === false) {
        initial = stocked(initial, DRAW * REMOTE_PROTOCOL_IDS.length);
      }
      const control = controlFor(param, initial);
      controls.set(param.name, control);
      const field = Field({ label: param.name, control });
      Tooltip(field.querySelector('.field__label'), param.description);
      return field;
    });
    clear(paramSlot, fields.length ? fields : h('span.xr-command__noparam', 'Aucun paramètre'));
  }

  function renderChips() {
    chips.clear();
    const offered = REMOTE_PROTOCOL_IDS.filter((id) => method?.protocols.includes(id));
    for (const id of Array.from(selected)) if (!offered.includes(id)) selected.delete(id);
    if (!selected.size) offered.forEach((id) => selected.add(id));
    clear(
      chipSlot,
      offered.map((id) => {
        const chip = Chip({
          label: protocol(id).label,
          color: protocol(id).color,
          selected: selected.has(id),
          onToggle: (on) => {
            if (on) selected.add(id);
            else if (selected.size > 1) selected.delete(id);
            else {
              chip.setSelected(true);
              onHint?.('Gardez au moins un protocole : il faut un appel à observer.');
            }
          },
        });
        chips.set(id, chip);
        return chip;
      }),
    );
  }

  function renderNetwork() {
    const conditions = store.get().network;
    if (!proxy.checked || !conditions) {
      clear(network);
      return;
    }
    const state = describeNetwork(conditions);
    clear(network, Badge({ label: state.label, tone: state.tone === 'muted' ? 'neutral' : state.tone, size: 'sm', dot: true, title: state.detail }));
  }

  function value() {
    const params = {};
    for (const param of method?.params ?? []) {
      const current = controls.get(param.name).value;
      const optional = !(method.required ?? []).includes(param.name);
      if (optional && (current === '' || current === null || current === undefined)) continue;
      params[param.name] = param.type === 'int' ? Number(current) : current;
    }
    return { method: method?.name, params, protocols: REMOTE_PROTOCOL_IDS.filter((id) => selected.has(id)), via_proxy: proxy.checked };
  }

  function submit() {
    if (!busy && method) onRun(value());
  }

  /** Exemple en un clic ; `params` est évalué au clic pour lire les stocks du moment. */
  function preset(label, icon, name, params, tone) {
    if (!byName.has(name)) return null;
    return Chip({
      label,
      icon,
      tone,
      onClick: () => {
        if (busy) return;
        el.setRequest(name, params());
        submit();
      },
    });
  }

  const form = h(
    'form.xr-command__form',
    {
      novalidate: true,
      onSubmit: (event) => {
        event.preventDefault();
        submit();
      },
    },
    h('div.xr-command__fields', Field({ label: 'Procédure', control: methodSelect }), paramSlot),
    h(
      'div.xr-command__options',
      h('div.xr-command__protocols', h('span.field__label', 'Protocoles'), chipSlot),
      h('div.xr-command__proxy', proxy, network),
      runButton,
    ),
  );

  const presets = h(
    'div.xr-command__presets',
    h('span.t-label', 'Essayer'),
    preset('Débiter un stock', 'package', 'update_stock', () => ({ product_id: stocked(products[0]?.id, DRAW * REMOTE_PROTOCOL_IDS.length), delta: -DRAW })),
    preset(`Réapprovisionner +${RESTOCK}`, 'plus', 'update_stock', () => ({ product_id: scarce(), delta: RESTOCK })),
    preset('Lire une fiche produit', 'file-json', 'get_product_details', () => ({ product_id: products[0]?.id })),
    preset('Lister 20 produits', 'list', 'list_products', () => ({ limit: 20 })),
    preset('Calculer 20!', 'calculator', 'calculate_factorial', () => ({ n: 20 })),
    preset('Produit inconnu → erreur', 'triangle-alert', 'update_stock', () => ({ product_id: missing, delta: -DRAW }), 'danger'),
  );

  const el = Card({ padding: 'sm', class: 'xr-command' }, form, presets);
  const unsubscribe = store.subscribe('network', renderNetwork);

  el.value = value;
  el.canRun = () => Boolean(method) && !busy;
  el.submit = submit;
  el.setBusy = (on) => {
    busy = Boolean(on);
    runButton.setLoading(busy);
    el.dataset.busy = String(busy);
  };
  el.setRequest = (name, params = {}) => {
    if (!byName.has(name)) return false;
    method = byName.get(name);
    methodSelect.setValue(name);
    renderParams(params);
    renderChips();
    return true;
  };
  el.includeProtocol = (id) => {
    const chip = chips.get(id);
    if (!chip) return false;
    selected.add(id);
    chip.setSelected(true);
    return true;
  };
  el.destroy = () => unsubscribe();

  renderParams({});
  renderChips();
  return el;
}
