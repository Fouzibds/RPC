/**
 * Sous le capot — panneau de détail d'une étape : libellé, rôle et explication du catalogue,
 * durée et taille mesurées, et surtout l'allure des données à ce point précis du voyage
 * (arguments Python → texte JSON ou octets Protobuf → trame → … → résultat).
 */

import { h, clear } from '../../core/dom.js';
import { fmtBytes, fmtPercent, fmtUs } from '../../core/format.js';
import { protocol as protocolInfo } from '../../core/protocols.js';
import { CodeBlock } from '../../components/codeblock.js';
import { HexView } from '../../components/hexview.js';
import { JsonTree } from '../../components/jsontree.js';
import { Badge, Callout, KeyValue } from '../../components/ui.js';
import { parseJson, pyCall } from './model.js';

const SIDE_LABELS = { client: 'côté client', server: 'côté serveur', network: 'réseau', resilience: 'résilience' };

/** Résumé chiffré du découpage d'une trame, d'après ses segments. */
function framingSummary(event) {
  const segments = event.detail.segments ?? [];
  const sum = (kind) => segments.filter((segment) => segment.kind === kind).reduce((total, segment) => total + segment.end - segment.start, 0);
  const fields = segments.filter((segment) => segment.kind === 'tag').length;
  const fieldBytes = sum('tag') + sum('len') + sum('value');
  const parts = [];
  if (sum('frame')) parts.push(`${fmtBytes(sum('frame'))} de tramage`);
  if (sum('header')) parts.push(`${fmtBytes(sum('header'))} d’en-têtes`);
  if (sum('body')) parts.push(`${fmtBytes(sum('body'))} de corps`);
  if (fields) parts.push(`${fields} champ${fields > 1 ? 's' : ''} Protobuf (${fmtBytes(fieldBytes)})`);
  return parts.length ? `Sur le fil : ${parts.join(' · ')}.` : 'Octets tels qu’ils circulent sur la connexion.';
}

/** Valeur finale de l'appel : résultat de l'inspection ou aperçu porté par la trace relue. */
function resultOf(model) {
  if (model.result !== undefined && model.result !== null) return { ok: true, value: model.result };
  const preview = model.byStage.get('client.return')?.detail.result_preview;
  if (preview === undefined) return { ok: false, value: undefined };
  if (typeof preview !== 'string') return { ok: true, value: preview };
  const parsed = parseJson(preview);
  return parsed.ok ? parsed : { ok: true, value: preview };
}

/**
 * Panneau de détail de l'étape active.
 * @returns {HTMLElement & {show: (step: Object, model: Object) => void, destroy: () => void}}
 */
export function StageDetail() {
  const about = h('div.xr-detail__about');
  const data = h('div.xr-detail__data');
  const el = h('div.xr-detail', { 'aria-live': 'polite' }, about, data);
  let owned = [];

  const own = (component) => {
    owned.push(component);
    return component;
  };

  const code = (text, language, title) => own(CodeBlock({ code: text, language, title, lineNumbers: false, wrap: true, maxHeight: 248 }));
  const tree = (value, rootLabel) => own(JsonTree(value, { collapsedDepth: 2, rootLabel, maxHeight: 248, maxString: 64 }));

  /** Message sérialisé (étapes de marshalling) : texte JSON tel quel, ou octets Protobuf et leur lecture. */
  function serialized(event, model) {
    const detail = event.detail;
    if (!event.size) {
      const http = detail.http;
      return {
        caption: 'Aucun corps à sérialiser : les arguments voyagent dans l’URL de la requête.',
        nodes: [http ? KeyValue({ dense: true, items: [{ label: 'Requête', value: `${http.method} ${http.path}`, mono: true }] }) : null],
      };
    }
    if (event.payload_text) {
      return {
        caption: `Du texte JSON, lisible tel quel : ${fmtBytes(event.size, { exact: true })}, noms de champs compris.`,
        nodes: [code(event.payload_text, 'json', detail.http ? `Corps de ${detail.http.method} ${detail.http.path}` : 'Message JSON-RPC 2.0')],
      };
    }
    return {
      caption: `Du binaire : ${fmtBytes(event.size, { exact: true })}. Ni noms de champs ni guillemets — des numéros de champ et des valeurs, que seul le contrat permet de relire.`,
      nodes: [
        own(HexView({ hex: event.payload_hex, title: detail.message_type ?? 'Message sérialisé', legend: false, maxBytes: 96, maxHeight: 132 })),
        detail.text ? code(detail.text, 'protobuf', `Lecture avec le contrat ${protocolInfo(model.protocol).short}`) : null,
      ],
    };
  }

  /** Trame sur le fil (étapes d'envoi et de réception). */
  function framed(event) {
    const detail = event.detail;
    const headers = detail.http2_headers ?? null;
    return {
      caption: framingSummary(event),
      nodes: [
        own(HexView({ hex: event.payload_hex, segments: detail.segments ?? [], title: 'Octets sur le fil', maxBytes: 112, maxHeight: 176 })),
        headers
          ? h('p.xr-detail__note', 'En-têtes HTTP/2, transmis à part dans une trame HEADERS : ', h('span.mono', Object.entries(headers).slice(0, 3).map(([key, value]) => `${key} ${value}`).join(' · ')))
          : null,
        detail.payload_truncated ? h('p.xr-detail__note', 'Capture tronquée : le message complet est plus long que la limite de capture.') : null,
      ],
    };
  }

  /** Message décodé (étapes de démarshalling). */
  function decoded(event, source, model) {
    if (event.detail.text) {
      return {
        caption: 'Grâce au contrat, chaque numéro de champ retrouve son nom et son type.',
        nodes: [code(event.detail.text, 'protobuf', event.detail.message_type ?? 'Message décodé')],
      };
    }
    const parsed = parseJson(source?.payload_text);
    if (parsed.ok) {
      return {
        caption: 'Le texte reçu redevient une structure en mémoire, reconstruite ici à partir des octets de la trace.',
        nodes: [tree(parsed.value, source.stage === 'client.marshal' ? 'requête' : 'réponse')],
      };
    }
    if (event.detail.message_type) {
      return { caption: `Les octets redeviennent un message ${event.detail.message_type}.`, nodes: [] };
    }
    const http = model.byStage.get('server.receive')?.detail.http;
    return {
      caption: http ? 'Rien à désérialiser dans le corps : les arguments sont extraits de l’URL.' : 'Cette étape ne publie pas le message décodé.',
      nodes: [http ? KeyValue({ dense: true, items: [{ label: 'Requête', value: `${http.method} ${http.path}`, mono: true }] }) : null],
    };
  }

  function failureCard(model, detail) {
    const error = model.error ?? {};
    const extra = { ...(error.detail ?? {}), ...(detail ?? {}) };
    delete extra.code;
    delete extra.message;
    const items = [
      { label: 'Code', value: error.code ?? detail?.code, mono: true, tone: 'danger' },
      error.type ? { label: 'Exception Python', value: error.type, mono: true } : null,
      error.retryable !== undefined ? { label: 'Rejouable', value: error.retryable ? 'oui' : 'non' } : null,
      ...Object.entries(extra).map(([key, value]) => ({ label: key, value: typeof value === 'object' ? JSON.stringify(value) : String(value), mono: true })),
    ].filter(Boolean);
    return [Callout({ tone: 'danger', title: error.message || detail?.message || 'Appel en échec' }, KeyValue({ dense: true, items }))];
  }

  function dataFor(step, model) {
    const event = step.event;
    const detail = event.detail;
    switch (step.stage) {
      case 'client.call':
        return {
          caption: 'Des objets Python en mémoire. Rien n’est encore sérialisé : pour le code appelant, c’est un appel de fonction ordinaire.',
          nodes: [
            code(pyCall(`stub.${model.method}`, detail.args ?? [], detail.kwargs ?? {}), 'python'),
            detail.rpc ? KeyValue({ dense: true, items: [{ label: 'Méthode du contrat', value: detail.rpc, mono: true }] }) : null,
          ],
        };
      case 'client.marshal':
      case 'server.marshal':
        return serialized(event, model);
      case 'client.send':
      case 'server.receive':
      case 'server.send':
      case 'client.receive':
        return framed(event);
      case 'server.unmarshal':
        return decoded(event, model.byStage.get('client.marshal'), model);
      case 'client.unmarshal':
        return decoded(event, model.byStage.get('server.marshal'), model);
      case 'server.dispatch': {
        const target = String(detail.target ?? '');
        return {
          caption: 'Le nom reçu sur le fil est relié à une vraie fonction du serveur, et les arguments décodés à ses paramètres.',
          nodes: [
            KeyValue({
              dense: true,
              items: [
                { label: 'Cible', value: target, mono: true },
                detail.route ? { label: 'Route', value: detail.route, mono: true } : null,
                detail.rpc ? { label: 'Méthode du contrat', value: detail.rpc, mono: true } : null,
              ].filter(Boolean),
            }),
            code(pyCall(target.split('.').slice(-2).join('.'), [], detail.bound_args ?? {}), 'python'),
          ],
        };
      }
      case 'server.execute':
      case 'server.error': {
        if (step.state === 'failed') {
          return {
            caption: 'La procédure lève une exception : le squelette la convertit en erreur du protocole.',
            nodes: failureCard(model, model.failure.serverDetail),
          };
        }
        const result = resultOf(model);
        const share = model.totalUs > 0 && Number.isFinite(event.duration_us) ? event.duration_us / model.totalUs : null;
        return {
          caption:
            share === null
              ? 'Le code métier s’exécute enfin : la seule étape qui existerait aussi dans un appel local.'
              : `Le code métier s’exécute enfin : ${fmtUs(event.duration_us)}, soit ${share < 0.001 ? 'moins de 0,1\u202F%' : fmtPercent(share)} de la durée totale de l’appel. C’est la seule étape qui existerait aussi dans un appel local.`,
          nodes: [result.ok ? tree(result.value, 'valeur renvoyée') : null],
        };
      }
      case 'client.return': {
        const result = resultOf(model);
        return {
          caption: 'Une valeur Python ordinaire, rendue à l’appelant comme si tout s’était passé en local.',
          nodes: [result.ok ? (typeof result.value === 'object' ? tree(result.value, 'résultat') : code(String(result.value), 'text')) : null],
        };
      }
      case 'client.error':
        return {
          caption: 'Le stub lève une exception typée : l’erreur distante devient une erreur locale que l’appelant peut intercepter.',
          nodes: failureCard(model, detail),
        };
      default:
        return { caption: '', nodes: [Object.keys(detail).length ? tree(detail, 'detail') : null] };
    }
  }

  function fact(label, value, hint) {
    return h('div.xr-fact', h('span.xr-fact__label.t-label', label), h('span.xr-fact__value.num', value), hint ? h('span.xr-fact__hint', hint) : null);
  }

  el.show = (step, model) => {
    owned.forEach((component) => component.destroy?.());
    owned = [];
    const event = step.event;
    const terminal = step.stage === 'client.return' || step.stage === 'client.error';
    const tone = step.state === 'failed' || step.stage === 'client.error' ? 'danger' : step.state === 'error' ? 'warning' : null;
    const share = !terminal && model.totalUs > 0 && event.duration_us > 0 ? event.duration_us / model.totalUs : null;

    clear(
      about,
      h(
        'div.xr-detail__head',
        h('span.xr-detail__index.num', { dataset: { tone: tone ?? 'ok' } }, String(step.index + 1).padStart(2, '0')),
        h(
          'div.xr-detail__title',
          h('h3.xr-detail__label', step.info.label),
          h('p.xr-detail__meta', [step.info.role, SIDE_LABELS[event.side] ?? event.side].filter(Boolean).join(' · ')),
        ),
        h('code.xr-detail__stage.mono', step.stage),
      ),
      tone === 'danger'
        ? h('div.xr-detail__flags', Badge({ label: step.state === 'failed' ? 'Point de rupture' : 'Erreur remontée à l’appelant', tone: 'danger', size: 'sm', icon: 'triangle-alert' }))
        : null,
      tone === 'warning' ? h('div.xr-detail__flags', Badge({ label: 'Cette étape transporte l’erreur', tone: 'warning', size: 'sm', icon: 'triangle-alert' })) : null,
      h('p.xr-detail__text', step.info.text),
      share !== null
        ? h(
            'div.xr-share',
            h('div.xr-share__track', h('span.xr-share__fill', { style: { width: `${Math.max(0.6, Math.min(100, share * 100))}%` } })),
            h('span.xr-share__label', h('span.num', share < 0.001 ? '< 0,1\u202F%' : fmtPercent(share, { decimals: share < 0.1 ? 1 : 0 })), ' de la durée totale de l’appel'),
          )
        : null,
      h(
        'div.xr-detail__facts',
        fact('Durée', Number.isFinite(event.duration_us) ? fmtUs(event.duration_us) : '—', terminal ? 'de l’appel entier' : Number.isFinite(event.duration_us) ? 'de cette étape' : 'simple jalon'),
        fact('Taille', Number.isFinite(event.size) ? fmtBytes(event.size, { exact: true }) : '—', Number.isFinite(event.size) ? 'du message' : 'aucun octet'),
        fact('Instant', `+${fmtUs(event.offset_us)}`, 'depuis l’appel'),
      ),
    );

    const view = dataFor(step, model);
    clear(
      data,
      h('div.xr-detail__data-head', h('span.t-label', 'Les données à cette étape'), h('span.xr-detail__proto', { style: { '--c': protocolInfo(model.protocol).color } }, protocolInfo(model.protocol).label)),
      view.caption ? h('p.xr-detail__caption', view.caption) : null,
      h('div.xr-detail__views', view.nodes.filter(Boolean)),
    );
  };

  el.destroy = () => {
    owned.forEach((component) => component.destroy?.());
    owned = [];
  };
  return el;
}
