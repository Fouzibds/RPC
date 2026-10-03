/**
 * Guide de style — couches flottantes : info-bulles, notifications, fenêtres modales,
 * tiroirs, palette de commandes et tiroir « Sous le capot ».
 */

import { h } from '../../core/dom.js';
import { confirm, openDrawer, openModal } from '../../components/modal.js';
import { openPalette } from '../../components/palette.js';
import { toast } from '../../components/toast.js';
import { wiretap } from '../../components/wiretap.js';
import { Button, Callout, Field, Grid, Input, KeyValue, ProtocolChip, Section, Select, Slider, Toggle, Tooltip } from '../../components/ui.js';
import { fmtMs } from '../../core/format.js';
import { DemoCard, Specimen, Usage } from './demo.js';
import { sampleTraceEvents } from './samples.js';

function tipTarget(label, placement, kbd) {
  const el = h('button.btn.btn--secondary.btn--sm', { type: 'button', dataset: { tip: placement } }, label);
  Tooltip(el, `Info-bulle placée « ${placement} »`, { placement, kbd });
  return el;
}

function policyModal() {
  openModal({
    title: 'Politique du client',
    description: 'Appliquée aux appels lancés depuis la console.',
    icon: 'shield-check',
    body: h(
      'div.kit-form',
      Field({ label: 'Délai maximal par appel', control: Slider({ value: 2000, min: 100, max: 10000, step: 100, format: (v) => fmtMs(v) }) }),
      Field({
        label: 'Nouvelles tentatives',
        control: Select({ value: 3, block: true, options: [1, 2, 3, 5].map((n) => ({ value: n, label: n === 1 ? 'Aucune (1 essai)' : `${n} essais au plus` })) }),
        hint: 'Recul exponentiel : 100 ms, 200 ms, 400 ms…',
      }),
      Field({ label: 'Clé d’idempotence', control: Input({ value: 'kit-7f3a9c', mono: true }), aside: 'facultatif' }),
      Field({ inline: true, label: 'Disjoncteur', hint: 'S’ouvre après 3 échecs consécutifs.', control: Toggle({ checked: true, ariaLabel: 'Disjoncteur' }) }),
    ),
    footer: (close) => [
      Button({ label: 'Annuler', variant: 'ghost', onClick: close }),
      Button({
        label: 'Appliquer',
        variant: 'primary',
        onClick: () => {
          close();
          toast.success('Politique appliquée', { description: '3 essais · délai de 2,00 s · disjoncteur actif' });
        },
      }),
    ],
  });
}

function traceDrawer() {
  openDrawer({
    title: 'Trace grpc-000128',
    subtitle: 'update_stock · 0,41 ms',
    icon: 'timer',
    width: 400,
    body: [
      KeyValue({
        items: [
          { label: 'Protocole', value: ProtocolChip('grpc', { size: 'sm' }) },
          { label: 'Identifiant', value: 'grpc-000128', mono: true, copy: true },
          { label: 'Requête', value: '15 o', mono: true },
          { label: 'Réponse', value: '43 o', mono: true },
          { label: 'Exécution serveur', value: '12,4 µs', mono: true },
        ],
      }),
      h('div.kit-gap'),
      Callout({ tone: 'info', text: 'Un tiroir modal piège le focus et se ferme avec Échap ou d’un clic sur le voile.' }),
    ],
    footer: (close) => [h('span.fg-2.t-small', 'openDrawer({ side, width, title, body })'), Button({ label: 'Fermer', size: 'sm', onClick: close })],
  });
}

async function askReset() {
  const ok = await confirm({
    title: 'Réinitialiser le laboratoire ?',
    text: 'Les stocks, les conditions réseau et les traces reviennent à leur état initial.',
    confirmLabel: 'Réinitialiser',
    tone: 'danger',
    icon: 'rotate-ccw',
  });
  if (ok) toast.info('Démonstration', { description: 'Dans le guide de style, rien n’est réellement réinitialisé.' });
}

/**
 * Sections « couches flottantes » du guide de style.
 * @returns {HTMLElement[]}
 */
export function layers() {
  return [
    Section(
      { title: 'Couches flottantes', description: 'Info-bulles, notifications, fenêtres modales, tiroirs, palette de commandes.' },
      Grid(
        DemoCard(
          { title: 'Tooltip et toast', span: 6 },
          Specimen('Tooltip', tipTarget('Haut', 'top'), tipTarget('Bas', 'bottom'), tipTarget('Gauche', 'left'), tipTarget('Droite', 'right'), tipTarget('Avec raccourci', 'top', 'g b')),
          Specimen(
            'Toast',
            Button({ label: 'Succès', size: 'sm', id: 'kit-toast-success', onClick: () => toast.success('Réseau : Internet (WAN)', { description: 'Latence continentale et un peu de gigue' }) }),
            Button({ label: 'Info', size: 'sm', id: 'kit-toast-info', onClick: () => toast.info('Benchmark terminé', { description: '4 protocoles · 1 000 appels chacun', action: { label: 'Voir', onClick: () => {} } }) }),
            Button({ label: 'Avertissement', size: 'sm', id: 'kit-toast-warn', onClick: () => toast.warn('Disjoncteur ouvert', { description: 'gRPC : 3 échecs consécutifs, appels suspendus 2 s' }) }),
            Button({ label: 'Erreur', size: 'sm', id: 'kit-toast-error', onClick: () => toast.error('Appel échoué', { description: 'TIMEOUT : aucune réponse du serveur en 5,00 s', action: { label: 'Réessayer', onClick: () => {} } }) }),
          ),
          Usage("toast.success(titre, { description, duration, action: { label, onClick } })\nTooltip(élément, 'texte', { placement: 'top', kbd: 'g b' })"),
        ),
        DemoCard(
          { title: 'Modal, drawer, palette, écoute du fil', span: 6 },
          Specimen(
            'Fenêtres',
            Button({ label: 'Fenêtre modale', icon: 'maximize-2', id: 'kit-open-modal', onClick: policyModal }),
            Button({ label: 'Confirmation', icon: 'circle-help', id: 'kit-open-confirm', onClick: askReset }),
            Button({ label: 'Tiroir', icon: 'panel-right', id: 'kit-open-drawer', onClick: traceDrawer }),
          ),
          Specimen(
            'Globales',
            Button({ label: 'Palette de commandes', icon: 'command', kbd: 'mod+k', id: 'kit-open-palette', onClick: () => openPalette() }),
            Button({
              label: 'Messages d’exemple',
              icon: 'radio',
              id: 'kit-wire-sample',
              onClick: () => {
                wiretap.open();
                sampleTraceEvents().forEach((event) => wiretap.push(event));
              },
            }),
          ),
          Usage('openModal({ title, description, icon, body, footer: (close) => […], size })\nconfirm({ title, text, confirmLabel, tone }) → Promise<boolean>\nopenDrawer({ side, width, title, subtitle, body, footer, modal })'),
        ),
      ),
    ),
  ];
}
