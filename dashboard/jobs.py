"""Tâches de fond du dashboard : banc d'essai et scénarios de pannes.

Ces deux expériences durent de quelques secondes à quelques minutes : elles s'exécutent
dans un thread, publient leur progression sur le WebSocket (messages ``job``) et restent
consultables par ``GET /api/jobs/{id}``.

Une seule tâche à la fois. Le banc d'essai coupe le bus de traces, compte les octets des
proxys et règle la latence du réseau simulé ; un scénario de panne dérègle ce même réseau.
Deux tâches simultanées — ou un réglage manuel du réseau pendant une tâche — fausseraient
les mesures de l'une et de l'autre : le laboratoire est alors « occupé » (``LabBusy``).
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from benchmark_lab import failure_simulation
from benchmark_lab.benchmark_perf import SUITE_LABELS, run_full_benchmark
from benchmark_lab.report import DEFAULT_KIND, list_reports, load_report, save_report
from common.errors import RpcError

from .hub import Message

if TYPE_CHECKING:
    from lab import LabRuntime

BENCHMARK = "benchmark"
FAILURE = "failure"
RUNNING, DONE, ERROR = "running", "done", "error"
HISTORY_LIMIT = 40              # tâches terminées conservées en mémoire
_LIVE_PROGRESS_CEILING = 0.95   # la durée d'un scénario n'est qu'indicative : 100 % est réservé à la fin réelle

_OCCUPANTS = {BENCHMARK: "le banc d'essai", FAILURE: "le scénario de panne « {title} »"}
_SCENARIOS = {scenario["id"]: scenario for scenario in failure_simulation.SCENARIOS}

_log = logging.getLogger(__name__)

Publish = Callable[[str, Message], None]


@dataclass
class Job:
    """Une tâche de fond et son avancement, tel que le renvoie ``GET /api/jobs/{id}``."""

    id: str
    kind: str
    title: str
    params: dict[str, Any]
    state: str = RUNNING
    progress: float = 0.0
    phase: str = ""
    message: str = ""
    result: Any = None
    error: dict[str, Any] | None = None
    partial: dict[str, Any] = field(default_factory=dict)       # banc d'essai : sections déjà mesurées
    steps: list[dict[str, Any]] = field(default_factory=list)   # scénario de panne : chronologie en cours
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None

    def summary(self) -> dict[str, Any]:
        """L'essentiel, sans résultat ni données partielles (messages ``job``, ``hello``, ``stats``)."""
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "state": self.state,
            "progress": self.progress,
            "phase": self.phase,
            "message": self.message,
        }

    def snapshot(self) -> dict[str, Any]:
        return {
            **self.summary(),
            "result": self.result,
            "error": self.error,
            "params": self.params,
            "partial": dict(self.partial),
            "steps": list(self.steps),
            "created_at": self.created_at,
            "finished_at": self.finished_at,
        }


class LabBusy(Exception):
    """Une tâche de fond est en cours : l'action demandée fausserait ses mesures (HTTP 409)."""

    def __init__(self, job: Job, action: str) -> None:
        self.job = job
        self.message = (
            f"Le laboratoire est occupé par {_OCCUPANTS[job.kind].format(title=job.title)} : impossible de "
            f"{action} avant sa fin, ses mesures en seraient faussées."
        )
        super().__init__(self.message)


class JobManager:
    """Lance les tâches de fond, une à la fois, et garde leurs derniers résultats."""

    def __init__(self, runtime: "LabRuntime", publish: Publish) -> None:
        self._runtime = runtime
        self._publish = publish
        self._lock = threading.Lock()
        self._jobs: OrderedDict[str, Job] = OrderedDict()
        self._active: Job | None = None
        self._latest_benchmark: dict[str, Any] | None = None
        self.failure_results: dict[str, dict[str, Any]] = {}    # dernier résultat de chaque scénario joué

    # -- consultation ---------------------------------------------------------------

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def recent(self) -> list[Job]:
        """Tâches connues, de la plus récente à la plus ancienne."""
        with self._lock:
            return list(reversed(self._jobs.values()))

    def active(self) -> dict[str, Any] | None:
        """Résumé de la tâche en cours, ou ``None`` si le laboratoire est disponible."""
        job = self._active
        return None if job is None else job.summary()

    def require_idle(self, action: str) -> None:
        """Lève ``LabBusy`` si une tâche est en cours ; ``action`` complète « impossible de … »."""
        job = self._active
        if job is not None:
            raise LabBusy(job, action)

    def latest_benchmark(self) -> dict[str, Any] | None:
        """Dernier rapport de banc d'essai : celui de cette session, sinon le plus récent sur disque."""
        cached = self._latest_benchmark
        for entry in list_reports():    # du plus récent au plus ancien
            if entry["kind"] != DEFAULT_KIND:
                continue
            # Les noms portent la date : l'ordre alphabétique est l'ordre chronologique.
            if cached is not None and entry["name"] <= f"{cached.get('id')}.json":
                break
            try:
                loaded = load_report(entry["name"])
            except (OSError, ValueError):
                continue    # fichier illisible : on se rabat sur le rapport précédent
            self._latest_benchmark = cached = loaded
            break
        return cached

    def forget_results(self) -> None:
        """Oublie les résultats de scénarios (remise à zéro du laboratoire) ; les rapports restent sur disque."""
        self.failure_results.clear()

    # -- lancement ------------------------------------------------------------------

    def start_benchmark(self, config: dict[str, Any]) -> Job:
        """Lance le banc d'essai avec une configuration déjà validée (``resolve_config``)."""
        return self._launch(BENCHMARK, "Banc d'essai", config, lambda job: self._run_benchmark(job, config))

    def start_failure(self, scenario_id: str, protocol: str, options: dict[str, Any] | None) -> Job:
        """Lance un scénario de panne ; ``scenario_id`` est un identifiant de ``SCENARIOS``."""
        params = {"scenario": scenario_id, "protocol": protocol, "options": options or {}}
        return self._launch(
            FAILURE,
            _SCENARIOS[scenario_id]["title"],
            params,
            lambda job: self._run_failure(job, scenario_id, protocol, options),
        )

    def _launch(self, kind: str, title: str, params: dict[str, Any], work: Callable[[Job], Any]) -> Job:
        with self._lock:
            if self._active is not None:
                raise LabBusy(self._active, "lancer une autre tâche")
            job = Job(id=f"{kind}-{uuid.uuid4().hex[:8]}", kind=kind, title=title, params=params)
            self._jobs[job.id] = job
            self._active = job
            while len(self._jobs) > HISTORY_LIMIT:
                self._jobs.popitem(last=False)
        threading.Thread(target=self._run, args=(job, work), name=f"dashboard-{job.id}", daemon=True).start()
        return job

    def _run(self, job: Job, work: Callable[[Job], Any]) -> None:
        try:
            job.result = work(job)
        except Exception as exc:    # la tâche tourne dans son propre thread : toute erreur doit finir dans le job
            if not isinstance(exc, (RpcError, ValueError, RuntimeError)):
                _log.exception("Tâche %s interrompue par une erreur inattendue", job.id)
            job.error = _describe_error(exc)
            job.state, job.message = ERROR, job.error["message"]
        else:
            job.state, job.progress, job.phase = DONE, 1.0, DONE
            job.partial = {}    # le résultat complet remplace les sections partielles : inutile de les servir deux fois
        job.finished_at = time.time()
        with self._lock:
            self._active = None
        # Dernier message de la tâche : il porte le résultat complet (ou l'erreur).
        self._publish("job", {
            "type": "job", "job_id": job.id, **job.summary(), "result": job.result, "error": job.error,
        })

    # -- les deux expériences ---------------------------------------------------------

    def _run_benchmark(self, job: Job, config: dict[str, Any]) -> dict[str, Any]:
        def progress(phase: str, fraction: float, message: str, partial: dict[str, Any] | None) -> None:
            job.phase, job.progress, job.message = phase, fraction, message
            if partial:
                job.partial.update(partial)
            self._publish("job", {"type": "job", "job_id": job.id, **job.summary(), "partial": partial})

        report = run_full_benchmark(self._runtime, config, progress)
        self._latest_benchmark = report
        try:
            path = save_report(report)
        except OSError as exc:
            # Les mesures sont acquises : un disque plein ne doit pas les faire passer pour un échec.
            job.message = f"{SUITE_LABELS['done']} — rapport non enregistré ({exc})"
        else:
            job.message = f"{SUITE_LABELS['done']} — rapport enregistré : {path.name}"
        return report

    def _run_failure(self, job: Job, scenario_id: str, protocol: str, options: dict[str, Any] | None) -> dict[str, Any]:
        hint_ms = _SCENARIOS[scenario_id]["duration_hint_s"] * 1000
        job.phase = scenario_id

        def on_step(step: dict[str, Any]) -> None:
            job.steps.append(step)
            job.message = step["label"]
            job.progress = max(job.progress, min(step["t_ms"] / hint_ms, _LIVE_PROGRESS_CEILING))
            self._publish("job", {"type": "job", "job_id": job.id, **job.summary(), "step": step})

        result = failure_simulation.run_scenario(
            self._runtime, scenario_id, protocol=protocol, on_step=on_step, options=options
        )
        self.failure_results[scenario_id] = result
        job.message = result["verdict"]
        return result


def _describe_error(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, RpcError):
        return {"code": exc.code, "message": exc.message}
    if isinstance(exc, ValueError):
        return {"code": "INVALID_ARGUMENT", "message": str(exc)}
    if isinstance(exc, RuntimeError):
        return {"code": "FAILED_PRECONDITION", "message": str(exc)}
    return {"code": "INTERNAL", "message": f"Erreur inattendue ({type(exc).__name__}) : {exc}"}
