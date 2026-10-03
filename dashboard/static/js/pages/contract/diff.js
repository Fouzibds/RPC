/**
 * Contrat & IDL — « service.proto, v1 face à v2 » : le diff des deux contrats, annoté ligne à
 * ligne, et la liste des changements classés. Choisir un changement fait défiler le diff jusqu'à
 * sa ligne, la met en avant et affiche son intitulé sous elle.
 */

import { h, uid } from '../../core/dom.js';
import { icon } from '../../core/icons.js';
import { DiffView } from '../../components/codeblock.js';
import { Badge, Button, Section, Segmented } from '../../components/ui.js';
import { CONTRACTS, diffAnnotations, hasLines, kindInfo, plural, severityInfo } from './model.js';

const fileName = (path) => String(path ?? '').split('/').pop();

/** Hauteur de l'en-tête collant d'un groupe de la liste : un élément amené en haut ne doit pas passer dessous. */
const GROUP_HEAD_HEIGHT = 32;

function lineReference(change) {
  const v1 = change.lines?.v1 ?? [];
  const v2 = change.lines?.v2 ?? [];
  if (v1.length && v2.length) return `l. ${v1[0]} → ${v2[0]}`;
  if (v2.length) return `v2 · l. ${v2.join(', ')}`;
  if (v1.length) return `v1 · l. ${v1.join(', ')}`;
  return '';
}

function snippet(version, code) {
  return h(
    'div.contract-snip',
    { dataset: { version } },
    h('span.contract-tag', { dataset: { version } }, version),
    code ? h('pre.contract-snip__code', code) : h('span.contract-snip__none', version === 'v2' ? 'supprimé du contrat' : 'absent du contrat'),
  );
}

/**
 * Section du diff des contrats.
 * @param {Object} options
 * @param {Object} options.overview Réponse de `GET /api/contract`.
 * @param {(scenarioId: string) => void} options.onScenario Appelé quand on demande à voir un scénario lié.
 * @returns {{el: HTMLElement, focusChange: (id: string, options?: {page?: boolean}) => void, destroy: () => void}}
 *   `focusChange` sélectionne un changement ; `page: true` amène d'abord la section à l'écran.
 */
export function diffSection({ overview, onScenario }) {
  const changes = overview.changes ?? [];
  const byId = new Map(changes.map((change) => [change.id, change]));
  const scenarioTitles = new Map((overview.scenarios ?? []).map((scenario) => [scenario.id, scenario.title]));
  const state = { selected: null, filter: 'all' };
  const items = new Map();

  const diff = DiffView({
    left: overview.proto_v1?.source ?? '',
    right: overview.proto_v2?.source ?? '',
    language: 'protobuf',
    leftTitle: `${fileName(overview.proto_v1?.path)} · v1`,
    rightTitle: `${fileName(overview.proto_v2?.path)} · v2`,
    annotations: diffAnnotations(changes, null),
    context: 3,
    maxHeight: 'var(--contract-pane-h)',
    onModeChange: () => scrollDiffTo(markRows(), 'auto'),
  });
  // Le DiffView reconstruit ses lignes (changement de mode, repli, largeur) : la mise en avant suit.
  const rowsObserver = new MutationObserver(() => markRows());
  rowsObserver.observe(diff.querySelector('tbody'), { childList: true });

  /** Met en avant les lignes du changement sélectionné ; renvoie la première (côté v2 de préférence). */
  function markRows() {
    const change = byId.get(state.selected);
    const wanted = { left: new Set(change?.lines?.v1 ?? []), right: new Set(change?.lines?.v2 ?? []) };
    let first = null;
    let firstRight = null;
    for (const row of diff.querySelectorAll('tr.diff__row')) {
      const [left, right] = row.querySelectorAll('td.diff__ln');
      const onLeft = Boolean(left) && wanted.left.has(Number(left.textContent));
      const onRight = Boolean(right) && wanted.right.has(Number(right.textContent));
      row.classList.toggle('is-focus-left', onLeft);
      row.classList.toggle('is-focus-right', onRight);
      if (onLeft || onRight) first ??= row;
      if (onRight) firstRight ??= row;
    }
    return firstRight ?? first;
  }

  function scrollDiffTo(row, behavior = 'smooth') {
    const scroller = diff.querySelector('.diff__scroll');
    if (!row || !scroller) return;
    const top = row.getBoundingClientRect().top - scroller.getBoundingClientRect().top + scroller.scrollTop;
    scroller.scrollTo({ top: Math.max(0, top - scroller.clientHeight * 0.32), behavior });
  }

  function changeItem(change) {
    const kind = kindInfo(change.kind);
    const severity = severityInfo(change.severity);
    const bodyId = uid('change');
    const reference = lineReference(change);
    const head = h(
      'button.contract-change__head',
      { type: 'button', 'aria-expanded': 'false', 'aria-controls': bodyId, onClick: () => select(state.selected === change.id ? null : change.id) },
      h('span.contract-change__title', change.title),
      h(
        'span.contract-change__meta',
        Badge({ label: kind.label, tone: kind.tone, size: 'sm' }),
        h('span.contract-change__severity', { dataset: { tone: severity.tone } }, severity.label),
        reference ? h('span.contract-change__lines.num', reference) : null,
      ),
      h('span.contract-change__chevron', { 'aria-hidden': 'true' }, icon('chevron-down', { size: 14 })),
    );
    const linked = (change.scenarios ?? []).filter((id) => scenarioTitles.has(id));
    const body = h(
      'div.contract-change__body',
      { id: bodyId, hidden: true },
      h('p.contract-change__element.mono', change.element),
      h('div.contract-snips', snippet('v1', change.v1), snippet('v2', change.v2)),
      h('p.contract-change__label.t-label', 'Effet sur le fil'),
      h('p.contract-change__text', change.wire_effect),
      h('p.contract-change__label.t-label', 'Pourquoi'),
      h('p.contract-change__text', change.explanation),
      linked.length
        ? h(
            'div.contract-change__links',
            linked.map((id) => Button({ label: scenarioTitles.get(id), size: 'sm', icon: 'flask-conical', title: 'Voir le scénario qui rejoue ce changement', onClick: () => onScenario(id) })),
          )
        : null,
    );
    const el = h('li.contract-change', { dataset: { tone: kind.tone, kind: change.kind } }, head, body);
    items.set(change.id, { el, head, body });
    return el;
  }

  const counts = { all: changes.length, breaking: changes.filter((change) => change.kind === 'breaking').length };
  const filter = Segmented({
    size: 'sm',
    block: true,
    value: state.filter,
    ariaLabel: 'Filtrer les changements',
    options: [
      { value: 'all', label: `Tous · ${counts.all}` },
      { value: 'breaking', label: `Cassants · ${counts.breaking}` },
      { value: 'compatible', label: `Compatibles · ${counts.all - counts.breaking}` },
    ],
    onChange: (value) => {
      state.filter = value;
      applyFilter();
    },
  });

  const groups = Object.entries(CONTRACTS)
    .map(([id, contract]) => ({ id, contract, changes: changes.filter((change) => change.contract === id) }))
    .concat([{ id: 'other', contract: { label: 'Autres', detail: '' }, changes: changes.filter((change) => !(change.contract in CONTRACTS)) }])
    .filter((group) => group.changes.length);

  const groupNodes = groups.map((group) => {
    const count = h('span.contract-changes__count.num');
    const list = h('ol.contract-changes__list', group.changes.map(changeItem));
    const el = h(
      'section.contract-changes__group',
      h(
        'header.contract-changes__group-head',
        h('span.contract-changes__dot', { style: group.contract.protocol ? { '--c': `var(--proto-${group.contract.protocol})` } : null, 'aria-hidden': 'true' }),
        h('span.contract-changes__group-title', group.contract.label),
        h('span.contract-changes__group-detail.truncate', group.contract.detail),
        count,
      ),
      list,
    );
    return { el, count, group };
  });

  const none = h('p.contract-changes__none', 'Aucun changement dans cette catégorie.');
  const scroller = h('div.contract-changes__scroll.scroll-y', groupNodes.map((node) => node.el), none);

  function applyFilter() {
    let visible = 0;
    for (const { el, count, group } of groupNodes) {
      const shown = group.changes.filter((change) => state.filter === 'all' || change.kind === state.filter);
      for (const change of group.changes) items.get(change.id).el.hidden = !shown.includes(change);
      el.hidden = shown.length === 0;
      count.textContent = String(shown.length);
      visible += shown.length;
    }
    none.hidden = visible > 0;
  }

  function select(id, { page = false } = {}) {
    state.selected = id && byId.has(id) ? id : null;
    const change = byId.get(state.selected);
    if (change && state.filter !== 'all' && change.kind !== state.filter) {
      state.filter = 'all';
      filter.setValue('all');
      applyFilter();
    }
    for (const [itemId, item] of items) {
      const open = itemId === state.selected;
      item.el.classList.toggle('is-selected', open);
      item.head.setAttribute('aria-expanded', String(open));
      item.body.hidden = !open;
    }
    diff.update({ annotations: diffAnnotations(changes, state.selected) });
    const row = markRows();
    if (page) section.scrollIntoView({ behavior: 'smooth', block: 'start' });
    if (!change) return;
    const item = items.get(change.id);
    const top = item.el.getBoundingClientRect().top - scroller.getBoundingClientRect().top + scroller.scrollTop;
    scroller.scrollTo({ top: Math.max(0, top - GROUP_HEAD_HEIGHT), behavior: 'smooth' });
    if (hasLines(change)) scrollDiffTo(row);
  }

  const stats = overview.stats ?? {};
  const panel = h(
    'aside.contract-changes',
    { 'aria-label': 'Changements entre les deux contrats' },
    h('header.contract-changes__bar', filter),
    scroller,
  );

  const section = Section(
    {
      id: 'contract-diff',
      title: `${fileName(overview.proto_v1?.path) || 'service.proto'} — v1 face à v2`,
      description: [
        `${plural(stats.breaking ?? counts.breaking, 'changement cassant', 'changements cassants')} et ${plural(stats.compatible ?? counts.all - counts.breaking, 'compatible')} séparent les deux contrats. `,
        'Cliquez sur un changement pour le retrouver dans le diff.',
      ],
    },
    h('div.contract-diff', h('div.contract-diff__main', diff), panel),
  );
  applyFilter();

  return {
    el: section,
    focusChange: (id, options) => select(id, options),
    destroy: () => {
      rowsObserver.disconnect();
      diff.destroy();
    },
  };
}
