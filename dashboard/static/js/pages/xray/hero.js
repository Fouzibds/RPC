/**
 * Sous le capot — carte principale : onglets de protocole (une trace par protocole), appel
 * disséqué, schéma animé, explication d'un éventuel échec et panneau de détail de l'étape active.
 */

import { h, clear } from '../../core/dom.js';
import { fmtBytes, fmtUs } from '../../core/format.js';
import { icon } from '../../core/icons.js';
import { protocol } from '../../core/protocols.js';
import { Badge, Callout, Card, CopyButton } from '../../components/ui.js';
import { Journey } from './journey.js';
import { pyCall } from './model.js';
import { StageDetail } from './stagedetail.js';

/** Explication d'un échec : où le pipeline s'arrête, et par quel chemin l'erreur revient. */
function failureNote(model) {
  const failure = model.failure;
  const at = model.steps[failure.at];
  const where = at ? `à l’étape ${at.index + 1} (${at.info.label})` : 'avant la première étape';
  const skipped = failure.skipped.filter((index) => index > failure.at);
  const range = skipped.length > 1 ? `Les étapes ${skipped[0] + 1} à ${skipped[skipped.length - 1] + 1} n’ont pas eu lieu` : skipped.length === 1 ? `L’étape ${skipped[0] + 1} n’a pas eu lieu` : '';
  const trailers = failure.serverDetail?.http2_trailers;

  let text;
  if (failure.kind === 'server') {
    if (skipped.length && trailers) {
      const shown = Object.entries(trailers).map(([key, value]) => `${key}: ${value}`).join(' · ');
      text = `La procédure a levé une erreur ${where}. ${range} : gRPC ne sérialise aucun message de réponse, l’erreur revient dans les trailers HTTP/2 (${shown}).`;
    } else if (skipped.length) {
      text = `La procédure a levé une erreur ${where}. ${range} : l’erreur est remontée directement au stub.`;
    } else {
      text = `La procédure a levé une erreur ${where}. Le retour suit pourtant le chemin habituel : l’erreur est sérialisée, tramée et renvoyée comme une réponse, puis le stub la transforme en exception.`;
    }
  } else if (failure.kind === 'transport') {
    text = `Le pipeline s’arrête ${where} : aucune réponse exploitable n’est revenue. ${range ? `${range}. ` : ''}Le stub ne peut que signaler l’échec à l’appelant.`;
  } else {
    text = `L’appel est refusé ${where}, côté client, avant tout envoi sur le réseau. ${range ? `${range}.` : ''}`;
  }
  return Callout({ tone: 'danger', title: `${failure.code || 'Erreur'} — ${failure.message || 'appel en échec'}`, text });
}

/**
 * Carte « Le voyage d'un appel ».
 * @param {Object} props
 * @param {(protocolId: string) => void} props.onProtocol Onglet de protocole choisi.
 * @param {(step: Object, model: Object, playing: boolean) => void} props.onStep Étape active.
 * @returns {HTMLElement & {update: (models: Object[], active: string, options?: Object) => void,
 *   journey: ReturnType<typeof Journey>, destroy: () => void}}
 */
export function Hero({ onProtocol, onStep }) {
  let playing = false;
  let current = null;
  const tabs = h('div.xr-ptabs', { role: 'tablist', 'aria-label': 'Protocole disséqué' });
  const call = h('div.xr-hero__call');
  const failureSlot = h('div.xr-hero__failure');
  const detail = StageDetail();
  const journey = Journey({
    onStep: (step, model) => {
      current = { step, model };
      detail.show(step, model);
      onStep(step, model, playing);
    },
    onPlaying: (on) => {
      playing = on;
      if (current) onStep(current.step, current.model, playing);
    },
  });

  const el = Card({ padding: 'none', class: 'xr-hero' }, h('div.xr-hero__top', tabs, call), journey, failureSlot, detail);

  function tab(model, active) {
    const info = protocol(model.protocol);
    const bytes = (model.request?.size ?? 0) + (model.response?.size ?? 0);
    return h(
      'button.xr-ptab',
      {
        type: 'button',
        role: 'tab',
        'aria-selected': String(active),
        tabIndex: active ? 0 : -1,
        style: { '--c': info.color, '--c-soft': info.soft, '--c-fg': info.fg },
        dataset: { ok: model.ok },
        onClick: () => onProtocol(model.protocol),
      },
      h('span.xr-ptab__dot', { 'aria-hidden': 'true' }),
      h('span.xr-ptab__name', info.label),
      h(
        'span.xr-ptab__meta.num',
        model.ok ? null : icon('triangle-alert', { size: 12, label: 'en échec' }),
        `${fmtUs(model.totalUs)} · ${fmtBytes(bytes, { exact: true })}`,
      ),
    );
  }

  tabs.addEventListener('keydown', (event) => {
    if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
    const buttons = Array.from(tabs.children);
    const index = buttons.indexOf(document.activeElement);
    if (index < 0) return;
    event.preventDefault();
    event.stopPropagation();
    const next = buttons[(index + (event.key === 'ArrowRight' ? 1 : buttons.length - 1)) % buttons.length];
    next.focus();
    next.click();
  });

  el.update = (models, active, { autoplay = false, keepStage = false, refocus = false } = {}) => {
    const model = models.find((item) => item.protocol === active) ?? models[0];
    const info = protocol(model.protocol);
    el.style.setProperty('--xr-proto', info.color);
    el.style.setProperty('--xr-proto-soft', info.soft);
    el.style.setProperty('--xr-proto-fg', info.fg);
    clear(tabs, models.map((item) => tab(item, item === model)));
    if (refocus) tabs.querySelector('[aria-selected="true"]')?.focus();

    const first = model.byStage.get('client.call');
    const signature = pyCall(model.method, first?.detail.args ?? [], first?.detail.kwargs ?? {}).replace(/\s*\n\s*/g, ' ');
    clear(
      call,
      h('code.xr-hero__signature.mono.truncate', { title: signature }, signature),
      model.viaProxy ? Badge({ label: 'via le proxy', tone: 'warning', size: 'sm', icon: 'shuffle' }) : null,
      model.callId ? h('span.xr-hero__id.mono', model.callId) : null,
      model.callId ? CopyButton({ text: model.callId, label: 'Copier l’identifiant d’appel' }) : null,
    );
    clear(failureSlot, model.failure ? failureNote(model) : null);
    failureSlot.hidden = !model.failure;
    journey.update(model, { autoplay, keepStage });
  };

  el.journey = journey;
  el.destroy = () => {
    journey.destroy();
    detail.destroy();
  };
  return el;
}
