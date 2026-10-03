/**
 * Console RPC — formulaire généré à partir des `ParamSpec` du catalogue : sélecteur de produit
 * (nom et stock), catégorie, entiers bornés, chaînes et petit éditeur JSON pour les listes.
 */

import { h } from '../../core/dom.js';
import { fmtNumber } from '../../core/format.js';
import { EmptyState, Field, IconButton, Input, NumberInput, Select, Textarea } from '../../components/ui.js';

/** Référence volontairement absente du catalogue : elle montre l'erreur `NOT_FOUND` de chaque protocole. */
const UNKNOWN_PRODUCT = 'SKU-9999';

const TYPE_LABELS = { int: 'entier', str: 'chaîne', product_id: 'référence', category: 'catégorie', updates: 'liste JSON', product_ids: 'liste JSON' };

/** Bornes d'un entier, en abrégé (« 0 à 5 k ») : le libellé partage une demi-largeur avec le nom. */
function bounds(param) {
  if (param.minimum === null || param.maximum === null) return '';
  return `${fmtNumber(param.minimum, { compact: true })} à ${fmtNumber(param.maximum, { compact: true })}`;
}

function productOptions(products, current) {
  const options = (products ?? []).map((product) => ({
    value: product.id,
    label: `${product.id} · ${product.name}`,
    description: product.category,
    hint: `${fmtNumber(product.stock)} en stock`,
  }));
  options.push({ value: UNKNOWN_PRODUCT, label: `${UNKNOWN_PRODUCT} · référence inconnue`, description: 'Pour observer l’erreur renvoyée', hint: 'NOT_FOUND', icon: 'bug' });
  if (current && !options.some((option) => option.value === current)) options.push({ value: current, label: String(current), description: 'Valeur hors catalogue' });
  return options;
}

function formatJson(value) {
  if (!Array.isArray(value)) return JSON.stringify(value ?? [], null, 2);
  const simple = value.every((item) => typeof item !== 'object' || item === null);
  if (simple) return JSON.stringify(value);
  return `[\n${value.map((item) => `  ${JSON.stringify(item)}`).join(',\n')}\n]`;
}

/**
 * Construit les champs d'une procédure.
 * @param {Object} props
 * @param {Object} props.spec `MethodSpec` du catalogue.
 * @param {Record<string, *>} props.values Valeurs courantes (modifiées en place).
 * @param {Object} props.catalog Catalogue (`products`, `categories`).
 * @param {() => void} props.onChange Appelé après chaque modification.
 * @returns {HTMLElement & {setErrors: (errors: Record<string, string>) => void, setProducts: (products: Object[]) => void}}
 *   `setErrors` affiche les messages sous les champs ; `setProducts` rafraîchit les stocks affichés.
 */
export function ParamForm({ spec, values, catalog, onChange }) {
  const fields = new Map();
  const productSelects = [];
  const required = new Set(spec.required ?? []);

  function set(name, value) {
    values[name] = value;
    onChange();
  }

  function control(param) {
    const label = `Paramètre ${param.name}`;
    if (param.type === 'int') {
      return NumberInput({
        value: values[param.name],
        min: param.minimum ?? -Infinity,
        max: param.maximum ?? Infinity,
        step: 1,
        suffix: param.name.endsWith('_ms') ? 'ms' : '',
        width: '100%',
        ariaLabel: label,
        onChange: (value) => set(param.name, value),
      });
    }
    if (param.type === 'product_id') {
      const select = Select({
        value: values[param.name],
        options: productOptions(catalog.products, values[param.name]),
        block: true,
        ariaLabel: label,
        onChange: (value) => set(param.name, value),
      });
      productSelects.push({ select, name: param.name });
      return select;
    }
    if (param.type === 'category') {
      return Select({
        value: values[param.name],
        options: [{ value: '', label: 'Toutes les catégories' }, ...(catalog.categories ?? []).map((category) => ({ value: category, label: category }))],
        block: true,
        ariaLabel: label,
        onChange: (value) => set(param.name, value),
      });
    }
    if (param.type === 'updates' || param.type === 'product_ids') {
      const area = Textarea({
        value: formatJson(values[param.name]),
        rows: param.type === 'updates' ? 5 : 2,
        mono: true,
        ariaLabel: label,
        onInput: (text) => {
          let parsed;
          try {
            parsed = JSON.parse(text);
          } catch (error) {
            parsed = error instanceof SyntaxError ? error : new SyntaxError(String(error));
          }
          set(param.name, parsed);
        },
      });
      return area;
    }
    const input = Input({
      value: values[param.name] ?? '',
      placeholder: required.has(param.name) ? '' : 'vide = aucune',
      mono: true,
      ariaLabel: label,
      onInput: (value) => set(param.name, value),
      suffix:
        param.name === 'idempotency_key'
          ? IconButton({
              icon: 'fingerprint',
              label: 'Générer une clé unique',
              size: 'sm',
              onClick: () => {
                input.value = `console-${Math.random().toString(16).slice(2, 10)}`;
                set(param.name, input.value);
              },
            })
          : null,
    });
    return input;
  }

  const nodes = spec.params.map((param) => {
    const node = control(param);
    const wide = param.type !== 'int' && param.type !== 'str';
    const aside = bounds(param) || (required.has(param.name) ? (TYPE_LABELS[param.type] ?? param.type) : 'facultatif');
    const field = Field({ label: param.name, control: node, hint: param.description, aside });
    field.classList.add('console-param');
    field.dataset.wide = String(wide);
    field.dataset.type = param.type;
    fields.set(param.name, { field, node });
    return field;
  });

  const el = h(
    'div.console-params',
    nodes.length ? nodes : EmptyState({ icon: 'circle-slash', size: 'sm', title: 'Aucun paramètre', text: 'Cette procédure s’appelle sans argument.' }),
  );

  el.setErrors = (errors) => {
    for (const [name, { field, node }] of fields) {
      field.setError(errors[name] ?? '');
      node.setInvalid?.(Boolean(errors[name]));
    }
  };
  el.setProducts = (products) => {
    for (const { select, name } of productSelects) select.setOptions(productOptions(products, values[name]));
  };
  return el;
}
