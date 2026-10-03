/**
 * Formatage à la française des grandeurs du laboratoire : octets, durées, débits, dates.
 * Séparateur de milliers et séparateur d'unité = espace fine insécable (U+202F), décimale =
 * virgule. Toute valeur absente ou non finie donne « — ».
 */

const NBSP = '\u202F';
const DASH = '—';

const formatters = new Map();

function numberFormat(min, max) {
  const key = `${min}:${max}`;
  let formatter = formatters.get(key);
  if (!formatter) {
    formatter = new Intl.NumberFormat('fr-FR', { minimumFractionDigits: min, maximumFractionDigits: max });
    formatters.set(key, formatter);
  }
  return formatter;
}

function isMissing(value) {
  return value === null || value === undefined || value === '' || !Number.isFinite(Number(value));
}

/** Nombre de décimales utiles pour afficher ~3 chiffres significatifs. */
function autoDecimals(value) {
  const abs = Math.abs(value);
  if (abs === 0 || abs >= 100) return 0;
  if (abs >= 10) return 1;
  if (abs >= 0.1) return 2;
  return 3;
}

/**
 * Nombre avec groupement français : `12480` → « 12 480 », `3.14159` → « 3,14 ».
 * @param {number} value
 * @param {{decimals?: number, maxDecimals?: number, compact?: boolean}} [options]
 *   `decimals` fixe le nombre de décimales ; `maxDecimals` (2 par défaut) le plafonne ;
 *   `compact` abrège : « 12,4 k », « 3,2 M ».
 * @returns {string}
 */
export function fmtNumber(value, { decimals, maxDecimals = 2, compact = false } = {}) {
  if (isMissing(value)) return DASH;
  const number = Number(value);
  if (compact && Math.abs(number) >= 1000) {
    const units = [
      [1e9, 'Md'],
      [1e6, 'M'],
      [1e3, 'k'],
    ];
    const [divisor, suffix] = units.find(([limit]) => Math.abs(number) >= limit);
    const scaled = number / divisor;
    return `${numberFormat(0, Math.abs(scaled) >= 100 ? 0 : 1).format(scaled)}${NBSP}${suffix}`;
  }
  if (decimals !== undefined) return numberFormat(decimals, decimals).format(number);
  return numberFormat(0, maxDecimals).format(number);
}

/**
 * Taille en octets : « 84 o », « 1,5 ko », « 3,2 Mo » (base 1024).
 * @param {number} bytes
 * @param {{exact?: boolean}} [options] `exact: true` garde l'unité octet : « 12 480 o ».
 * @returns {string}
 */
export function fmtBytes(bytes, { exact = false } = {}) {
  if (isMissing(bytes)) return DASH;
  const number = Number(bytes);
  if (exact || Math.abs(number) < 1024) return `${numberFormat(0, 0).format(number)}${NBSP}o`;
  const units = ['ko', 'Mo', 'Go', 'To'];
  let scaled = number / 1024;
  let index = 0;
  while (Math.abs(scaled) >= 1024 && index < units.length - 1) {
    scaled /= 1024;
    index += 1;
  }
  const decimals = Math.abs(scaled) >= 100 ? 0 : Math.abs(scaled) >= 10 ? 1 : 2;
  return `${numberFormat(0, decimals).format(scaled)}${NBSP}${units[index]}`;
}

/**
 * Durée exprimée en millisecondes : « 0,42 ms », « 12,3 ms », « 1,25 s ».
 * @param {number} ms
 * @param {{decimals?: number}} [options] Force le nombre de décimales.
 * @returns {string}
 */
export function fmtMs(ms, { decimals } = {}) {
  if (isMissing(ms)) return DASH;
  const number = Number(ms);
  if (Math.abs(number) >= 60000) return fmtDuration(number / 1000);
  if (Math.abs(number) >= 1000) {
    const seconds = number / 1000;
    const d = decimals ?? (Math.abs(seconds) >= 10 ? 1 : 2);
    return `${numberFormat(d, d).format(seconds)}${NBSP}s`;
  }
  const d = decimals ?? autoDecimals(number);
  return `${numberFormat(d, d).format(number)}${NBSP}ms`;
}

/**
 * Durée exprimée en microsecondes : « 850 µs », « 1,24 ms », « 2,50 s ».
 * @param {number} us
 * @returns {string}
 */
export function fmtUs(us) {
  if (isMissing(us)) return DASH;
  const number = Number(us);
  if (Math.abs(number) >= 1000) return fmtMs(number / 1000);
  const d = Math.abs(number) >= 100 || number === 0 ? 0 : 1;
  return `${numberFormat(d, d).format(number)}${NBSP}µs`;
}

/**
 * Durée longue en secondes : « 45 s », « 3 min 12 s », « 2 h 05 min », « 3 j 4 h ».
 * @param {number} seconds
 * @returns {string}
 */
export function fmtDuration(seconds) {
  if (isMissing(seconds)) return DASH;
  const total = Math.max(0, Math.round(Number(seconds)));
  if (Number(seconds) > 0 && Number(seconds) < 1) return fmtMs(Number(seconds) * 1000);
  const days = Math.floor(total / 86400);
  const hours = Math.floor((total % 86400) / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  const pad = (n) => String(n).padStart(2, '0');
  if (days > 0) return `${days}${NBSP}j ${hours}${NBSP}h`;
  if (hours > 0) return `${hours}${NBSP}h ${pad(minutes)}${NBSP}min`;
  if (minutes > 0) return `${minutes}${NBSP}min ${pad(secs)}${NBSP}s`;
  return `${secs}${NBSP}s`;
}

/**
 * Pourcentage à partir d'un ratio : `0.125` → « 12,5 % ».
 * @param {number} ratio Valeur entre 0 et 1 (ou au-delà).
 * @param {{decimals?: number, signed?: boolean}} [options] `signed` préfixe « + » aux valeurs positives.
 * @returns {string}
 */
export function fmtPercent(ratio, { decimals = 1, signed = false } = {}) {
  if (isMissing(ratio)) return DASH;
  const percent = Number(ratio) * 100;
  const text = numberFormat(0, decimals).format(Math.abs(percent));
  const sign = percent < 0 ? '−' : signed && percent > 0 ? '+' : '';
  return `${sign}${text}${NBSP}%`;
}

/**
 * Débit : « 1 250 req/s », « 8,4 req/s ».
 * @param {number} perSecond
 * @param {string} [unit='req/s']
 * @returns {string}
 */
export function fmtRate(perSecond, unit = 'req/s') {
  if (isMissing(perSecond)) return DASH;
  const number = Number(perSecond);
  const d = Math.abs(number) >= 100 || number === 0 ? 0 : 1;
  return `${numberFormat(0, d).format(number)}${NBSP}${unit}`;
}

function toDate(timestamp) {
  if (timestamp instanceof Date) return timestamp;
  const number = Number(timestamp);
  if (!Number.isFinite(number)) return null;
  return new Date(number < 1e12 ? number * 1000 : number);
}

/**
 * Heure locale d'un horodatage : « 14:32:07 » (ou « 14:32:07.123 » avec `ms: true`).
 * @param {number|Date} timestamp Epoch en secondes ou millisecondes, ou `Date`.
 * @param {{ms?: boolean}} [options]
 * @returns {string}
 */
export function fmtTime(timestamp, { ms = false } = {}) {
  const date = toDate(timestamp);
  if (!date) return DASH;
  const pad = (n, width = 2) => String(n).padStart(width, '0');
  const base = `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
  return ms ? `${base}.${pad(date.getMilliseconds(), 3)}` : base;
}

/**
 * Temps écoulé depuis un horodatage : « à l'instant », « il y a 12 s », « il y a 3 min », « il y a 2 h ».
 * @param {number|Date} timestamp Epoch en secondes ou millisecondes, ou `Date`.
 * @param {number} [now=Date.now()] Référence, en millisecondes.
 * @returns {string}
 */
export function fmtRelative(timestamp, now = Date.now()) {
  const date = toDate(timestamp);
  if (!date) return DASH;
  const delta = (now - date.getTime()) / 1000;
  const abs = Math.abs(delta);
  if (abs < 5) return 'à l’instant';
  const steps = [
    [60, 1, 's'],
    [3600, 60, 'min'],
    [86400, 3600, 'h'],
    [Infinity, 86400, 'j'],
  ];
  const [, divisor, unit] = steps.find(([limit]) => abs < limit);
  const amount = `${Math.floor(abs / divisor)}${NBSP}${unit}`;
  return delta >= 0 ? `il y a ${amount}` : `dans ${amount}`;
}

/**
 * Typographie française : espace insécable avant « : » et à l'intérieur des guillemets, espace
 * fine insécable avant « ; », « ? », « ! » et « % ». La longueur du texte est conservée.
 * `h()` l'applique déjà à tout texte hors code ; à appeler soi-même avant un `textContent = …`.
 * @param {*} text Les valeurs qui ne sont pas des chaînes sont renvoyées telles quelles.
 * @returns {*}
 */
export function typo(text) {
  if (typeof text !== 'string') return text;
  return text
    .replace(/ :(?=\s|$)/g, '\u00A0:')
    .replace(/ ([;?!%])(?=\s|$|[)»,.])/g, '\u202F$1')
    .replace(/« /g, '«\u00A0')
    .replace(/ »/g, '\u00A0»');
}

/**
 * Sépare une valeur formatée de son unité : « 12,3 ms » → `{ value: '12,3', unit: 'ms' }`.
 * Utile pour afficher le nombre en grand et l'unité en petit.
 * @param {string} text
 * @returns {{value: string, unit: string}}
 */
export function splitUnit(text) {
  const index = String(text).lastIndexOf(NBSP);
  if (index === -1) return { value: String(text), unit: '' };
  const unit = text.slice(index + 1);
  if (/^[\d\s,.−+-]+$/.test(unit)) return { value: String(text), unit: '' };
  return { value: text.slice(0, index), unit };
}
