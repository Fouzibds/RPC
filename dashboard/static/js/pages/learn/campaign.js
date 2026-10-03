/**
 * Bilan — campagne de mesure : les quatre expériences qui chiffrent le bilan, lancées depuis
 * cette page. Le banc d'essai et les scénarios de pannes sont des tâches de fond suivies par
 * les messages `job` du WebSocket (et par `GET /api/jobs/{id}` en secours) ; les scénarios de
 * contrat et les lots d'appels simultanés répondent directement.
 */

/** Expériences, dans l'ordre de la campagne. */
export const EXPERIMENTS = Object.freeze(['benchmark', 'failures', 'contract', 'batches']);

/** Nom de chaque expérience, tel qu'il est affiché et annoncé. */
export const EXPERIMENT_NAMES = Object.freeze({ benchmark: 'Banc d’essai', failures: 'Pannes réseau', contract: 'Contrat', batches: 'Multiplexage' });

/** Protocoles dont un lot simultané prouve le multiplexage (`dashboard/summary.py`). */
const BATCH_PROTOCOLS = Object.freeze(['custom', 'grpc']);
const BATCH_SIZE = 20;
const BATCH_METHOD = 'get_product_details';
const POLL_MS = 1000;
const MAX_SILENT_POLLS = 5;

/**
 * @typedef {Object} CampaignEnv
 * @property {{get: Function, post: Function}} api
 * @property {{on: Function}} ws
 * @property {AbortSignal} signal Interrompt le suivi quand la page est quittée.
 * @property {Object[]|null} failures Scénarios de pannes connus (`GET /api/failures/scenarios`).
 * @property {Object|null} report Dernier rapport de banc d'essai.
 * @property {Object|null} catalog Catalogue des procédures (`appStore.catalog`).
 */

/**
 * Attend la fin d'une tâche de fond.
 * @param {string} jobId
 * @param {CampaignEnv} env
 * @param {(progress: number, message: string) => void} onProgress
 * @returns {Promise<void>} Rejette si la tâche échoue, disparaît ou si le laboratoire ne répond plus.
 */
function waitJob(jobId, env, onProgress) {
  return new Promise((resolve, reject) => {
    let settled = false;
    let timer = 0;
    let silent = 0;

    const settle = (action, value) => {
      if (settled) return;
      settled = true;
      window.clearTimeout(timer);
      off();
      env.signal.removeEventListener('abort', onAbort);
      action(value);
    };
    const apply = (job) => {
      if (job.state === 'done') settle(resolve);
      else if (job.state === 'error') settle(reject, new Error(job.error?.message ?? job.message ?? 'La tâche a échoué.'));
      else onProgress(Number(job.progress) || 0, job.message ?? '');
    };
    const onAbort = () => settle(reject, new DOMException('Suivi interrompu.', 'AbortError'));
    const off = env.ws.on('job', (message) => {
      if ((message.job_id ?? message.id) === jobId) apply(message);
    });
    const poll = async () => {
      try {
        apply(await env.api.get(`/api/jobs/${encodeURIComponent(jobId)}`, { signal: env.signal, timeoutMs: 8000 }));
        silent = 0;
      } catch (error) {
        silent += 1;
        if (!error.offline || silent >= MAX_SILENT_POLLS) settle(reject, error);
      }
      if (!settled) timer = window.setTimeout(poll, POLL_MS);
    };

    env.signal.addEventListener('abort', onAbort, { once: true });
    poll();
  });
}

async function runBenchmark(env, onProgress) {
  onProgress(0, 'Démarrage du banc d’essai…');
  const { job_id: jobId } = await env.api.post('/api/benchmark/run', { quick: true }, { signal: env.signal });
  await waitJob(jobId, env, onProgress);
}

async function runFailures(env, onProgress) {
  const scenarios = env.failures ?? (await env.api.get('/api/failures/scenarios', { signal: env.signal })).scenarios ?? [];
  if (!scenarios.length) throw new Error('Aucun scénario de panne n’est proposé par le laboratoire.');
  for (const [index, scenario] of scenarios.entries()) {
    const report = (progress, message) => onProgress((index + Math.min(1, progress)) / scenarios.length, `${scenario.title} — ${message || 'en cours…'}`);
    report(0, '');
    const { job_id: jobId } = await env.api.post('/api/failures/run', { scenario: scenario.id, protocol: 'custom' }, { signal: env.signal });
    await waitJob(jobId, env, report);
  }
}

async function runContract(env, onProgress) {
  onProgress(null, 'Un client v1 face aux serveurs « contrat v2 »…');
  await env.api.post('/api/contract/run', { scenario: 'all' }, { signal: env.signal, timeoutMs: 60000 });
}

/** L'appel du lot : celui du dernier banc d'essai, sinon la procédure de référence et ses valeurs par défaut. */
function batchCall(env) {
  const config = env.report?.config;
  if (config?.method && config.params) return { method: config.method, params: config.params };
  const spec = (env.catalog?.methods ?? []).find((method) => method.name === BATCH_METHOD);
  if (!spec) throw new Error('Catalogue des procédures indisponible : impossible de composer le lot d’appels.');
  const defaults = (spec.params ?? []).filter((param) => param.default !== null && param.default !== undefined);
  return { method: spec.name, params: Object.fromEntries(defaults.map((param) => [param.name, param.default])) };
}

async function runBatches(env, onProgress) {
  const call = batchCall(env);
  for (const [index, protocol] of BATCH_PROTOCOLS.entries()) {
    onProgress(index / BATCH_PROTOCOLS.length, `${BATCH_SIZE} appels ${call.method} simultanés…`);
    const batch = await env.api.post('/api/call', { ...call, protocol, mode: 'async', count: BATCH_SIZE }, { signal: env.signal, timeoutMs: 60000 });
    if (!batch?.ok) throw new Error(batch?.error?.message ?? `Le lot d’appels simultanés a échoué (${batch?.errors ?? '?'} erreurs).`);
  }
}

const RUNNERS = Object.freeze({ benchmark: runBenchmark, failures: runFailures, contract: runContract, batches: runBatches });

/**
 * Lance une expérience et attend son issue.
 * @param {'benchmark'|'failures'|'contract'|'batches'} id
 * @param {CampaignEnv} env
 * @param {(progress: number|null, message: string) => void} onProgress
 *   Avancement entre 0 et 1 (`null` : durée inconnue) et message de l'étape en cours.
 * @returns {Promise<void>} Rejette avec une erreur dont `message` est lisible (français).
 */
export function runExperiment(id, env, onProgress) {
  return RUNNERS[id](env, onProgress);
}
