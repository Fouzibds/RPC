"""Bilan « avantages / inconvénients du RPC », chiffré par les mesures du laboratoire.

Chaque argument est écrit une fois pour toutes ; son chiffre et sa preuve, eux, viennent
de ce que le laboratoire a réellement mesuré : dernier rapport du banc d'essai, derniers
scénarios de pannes et de contrat joués, dernier lot d'appels asynchrones. Tant qu'une
expérience n'a pas été lancée, ``metric`` vaut ``None`` et ``evidence`` dit laquelle lancer
— aucun nombre n'est inventé. Les deux seuls chiffres disponibles d'emblée sont lus dans le
code du dépôt lui-même : lignes des quatre écritures de ``transparency_demo`` et diff des
deux contrats ``.proto``.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Mapping

from benchmark_lab import failure_simulation
from benchmark_lab.contract_evolution import contract_overview
from benchmark_lab.report import fr_bytes, fr_compact, fr_duration, fr_number, fr_ratio
from benchmark_lab.transparency_demo import code_comparison
from common.config import PROTOCOL_LABELS, PROTOCOLS

Report = Mapping[str, Any]
Results = Mapping[str, Mapping[str, Any]]

_RUN_BENCHMARK = "Pas encore mesuré : lancez le banc d’essai (page « Benchmark »)."
_RUN_FAILURE = "Pas encore observé : jouez le scénario « {title} » (page « Chaos réseau »)."
_RUN_CONTRACT = "Pas encore observé : jouez les scénarios de rupture (page « Contrat & IDL »)."
_RUN_BATCH = "Pas encore mesuré : lancez un lot d’appels asynchrones (page « Console RPC »)."
_REFERENCE_METHOD = "get_product_details"
_FAILURE_TITLES = {scenario["id"]: scenario["title"] for scenario in failure_simulation.SCENARIOS}

# Matrice qualitative : ce qui distingue les quatre façons d'appeler la même procédure.
_MATRIX: tuple[tuple[str, str, str, str, str], ...] = (
    ("Contrat", "Signature de la fonction", "Implicite : découvert à l’exécution (rpc.discover)",
     "IDL Protobuf, stub et squelette générés", "Convention d’URL et de verbes"),
    ("Format des messages", "Aucun : objets en mémoire", "JSON (texte) précédé de sa longueur",
     "Protobuf (binaire) précédé de 5 octets", "JSON (texte) sous des en-têtes HTTP"),
    ("Transport", "Aucun", "TCP, connexion persistante", "HTTP/2, connexion persistante",
     "HTTP/1.1, connexion persistante"),
    ("Appels simultanés", "Threads", "Multiplexés sur une connexion (id de requête)",
     "Multiplexés sur une connexion (flux HTTP/2)", "Une connexion par appel en cours"),
    ("Flux (streaming)", "Générateur Python", "Flux serveur (notifications rpc.stream.item)",
     "Flux serveur, client et bidirectionnel", "Flux serveur (NDJSON par morceaux)"),
    ("Typage", "Celui du langage", "Contrôlé à l’exécution, par le serveur",
     "Contrôlé avant l’envoi, par le code généré", "Perdu dans l’URL, retrouvé par le serveur"),
    ("Erreurs", "Exceptions", "Codes JSON-RPC (−32601, −32602…)", "Statuts gRPC (NOT_FOUND, UNAVAILABLE…)",
     "Statuts HTTP (404, 409, 503…)"),
    ("Échéance (timeout)", "Sans objet", "Locale au client", "Propagée au serveur (grpc-timeout)",
     "Locale au client"),
    ("Lisibilité des messages", "Sans objet", "Lisible tel quel", "Illisible sans le contrat",
     "Lisible tel quel (curl, navigateur)"),
    ("Depuis un navigateur", "Sans objet", "Non : socket TCP brute", "Par gRPC-Web et un proxy",
     "Natif (fetch)"),
    ("Outillage", "Débogueur du langage", "À écrire soi-même", "protoc, greffons, grpcurl",
     "Universel : curl, proxys, caches HTTP"),
    ("Couplage au contrat", "Fort : même programme", "Fort, et sans garde-fou : la rupture se voit à l’exécution",
     "Fort : numéros de champs et types figés", "Plus lâche : les champs inconnus sont ignorés"),
)


@lru_cache(maxsize=1)
def _code_facts() -> dict[str, Any]:
    """Lignes de code des quatre écritures du même ``update_stock`` (lues dans leur source réelle)."""
    snippets = {snippet["id"]: snippet for snippet in code_comparison()["snippets"]}
    return {
        "stub_lines": snippets["custom"]["lines"],
        "grpc_lines": snippets["grpc"]["lines"],
        "rest_lines": snippets["rest"]["lines"],
        "rest_concerns": len(snippets["rest"]["concerns"]),
    }


@lru_cache(maxsize=1)
def _contract_facts() -> dict[str, int]:
    """Changements entre les contrats v1 et v2, comptés par le diff des deux ``.proto``."""
    return dict(contract_overview()["stats"])


def _item(
    identifier: str,
    title: str,
    text: str,
    evidence: str,
    metric: dict[str, Any] | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    """Un argument du bilan ; ``source`` nomme l'expérience d'où viennent son chiffre et sa preuve."""
    return {"id": identifier, "title": title, "text": text, "metric": metric, "evidence": evidence, "source": source}


def _metric(label: str, value: Any, unit: str) -> dict[str, Any]:
    return {"label": label, "value": value, "unit": unit}


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _counted(count: int, singular: str, plural: str) -> str:
    return f"{count} {singular if count < 2 else plural}"


# --- Lecture du rapport de banc d'essai ----------------------------------------------

def _latency_results(report: Report | None, *, remote_only: bool = False) -> list[Mapping[str, Any]]:
    """Protocoles mesurés avec succès, du plus rapide au plus lent."""
    results = [
        result for result in ((report or {}).get("latency") or {}).get("results") or ()
        if _number(result.get("mean_ms")) and not (remote_only and result["protocol"] == "local")
    ]
    return sorted(results, key=lambda result: result["mean_ms"])


def _response_row(report: Report | None) -> Mapping[str, Any] | None:
    for row in ((report or {}).get("payload") or {}).get("rows") or ():
        if row["method"] == _REFERENCE_METHOD and row["direction"] == "response":
            if _number(row.get("json_bytes")) and _number(row.get("protobuf_bytes")):
                return row
    return None


def _wire_rows(report: Report | None) -> list[Mapping[str, Any]]:
    """Octets réels par appel, du protocole le plus léger au plus lourd."""
    rows = [
        row for row in ((report or {}).get("payload") or {}).get("wire") or ()
        if _number(row.get("total_per_call"))
    ]
    return sorted(rows, key=lambda row: row["total_per_call"])


def _conditions(latency: Mapping[str, Any]) -> str:
    route = "à travers le proxy de chaos" if latency.get("via_proxy") else "en boucle locale"
    return f"{latency.get('method', '')}, {fr_number(latency.get('iterations'))} appels par protocole, {route}"


# --- Avantages -----------------------------------------------------------------------

def _transparency() -> dict[str, Any]:
    code = _code_facts()
    return _item(
        "location_transparency",
        "Transparence de localisation",
        "Un appel distant s’écrit comme un appel local : le stub se charge de la sérialisation, du transport et "
        "de la traduction des erreurs. Le code métier ne change pas quand la procédure change de machine.",
        f"Le même update_stock tient en {code['stub_lines']} lignes avec le stub maison et {code['grpc_lines']} "
        f"avec le stub gRPC, contre {code['rest_lines']} lignes en REST écrit à la main, où "
        f"{code['rest_concerns']} préoccupations reviennent à l’appelant (page « Transparence »).",
        _metric("Lignes de code de l’appel avec un stub", code["stub_lines"], "lignes"),
        "code",
    )


def _compact_messages(report: Report | None) -> dict[str, Any]:
    title = "Messages compacts"
    text = (
        "Protobuf remplace les noms de champs par des numéros et encode les entiers sur une longueur variable : "
        "à contenu égal, le message binaire est plus court que son équivalent JSON."
    )
    row = _response_row(report)
    if row is None:
        return _item("compact_messages", title, text, _RUN_BENCHMARK)
    evidence = (
        f"Réponse de {_REFERENCE_METHOD} : {fr_bytes(row['protobuf_bytes'])} en Protobuf contre "
        f"{fr_bytes(row['json_bytes'])} en JSON."
    )
    scaling = [
        point for point in report["payload"].get("scaling") or ()
        if _number(point.get("json_bytes")) and _number(point.get("protobuf_bytes"))
    ]
    if scaling:
        largest = max(scaling, key=lambda point: point["items"])
        evidence += (
            f" Sur une liste de {fr_number(largest['items'])} produits : {fr_bytes(largest['protobuf_bytes'])} "
            f"contre {fr_bytes(largest['json_bytes'])}."
        )
    metric = _metric("Taille d’une fiche produit en Protobuf, par rapport à JSON", row["protobuf_vs_json_pct"], "%")
    return _item("compact_messages", title, text, evidence, metric, "benchmark")


def _light_framing(report: Report | None) -> dict[str, Any]:
    title = "Peu d’octets autour du message"
    text = (
        "Le tramage RPC est minimal — 4 octets pour le JSON-RPC maison, 5 pour gRPC, dont HTTP/2 compresse les "
        "en-têtes — là où HTTP/1.1 répète ligne de départ et en-têtes en clair à chaque appel."
    )
    rows = _wire_rows(report)
    if len(rows) < 2:
        return _item("light_framing", title, text, _RUN_BENCHMARK)
    lightest = rows[0]
    listing = ", ".join(f"{row['label']} {fr_number(row['total_per_call'])}" for row in rows)
    return _item(
        "light_framing",
        title,
        text,
        f"Octets réellement échangés sur TCP par appel {lightest.get('method', _REFERENCE_METHOD)}, aller et "
        f"retour (compteurs des proxys) : {listing}.",
        _metric(f"Octets par appel — {lightest['label']}", lightest["total_per_call"], "octets"),
        "benchmark",
    )


def _speed(report: Report | None) -> dict[str, Any]:
    title = "Des appels rapides sur un réseau sain"
    text = (
        "Connexion persistante, tramage léger, sérialisation efficace : sur un réseau proche et sain, un appel "
        "RPC reste assez rapide pour relier les services d’une même application."
    )
    remote = _latency_results(report, remote_only=True)
    if not remote:
        return _item("speed", title, text, _RUN_BENCHMARK)
    fastest = remote[0]
    listing = ", ".join(f"{result['label']} {fr_duration(result['mean_ms'])}" for result in remote)
    return _item(
        "speed",
        title,
        text,
        f"Temps moyen par appel ({_conditions(report['latency'])}) : {listing}.",
        _metric(f"Temps moyen d’un appel — {fastest['label']}", fastest["mean_ms"], "ms"),
        "benchmark",
    )


def _multiplexing(batches: Results) -> dict[str, Any]:
    title = "Multiplexage et flux"
    text = (
        "Une seule connexion transporte plusieurs appels simultanés, et des flux dans les deux sens avec gRPC : "
        "pas de nouvelle connexion par requête, pas d’interrogation répétée pour suivre un indicateur."
    )
    measured = [
        batch for protocol, batch in batches.items()
        if protocol in ("custom", "grpc") and batch.get("ok") and _number(batch.get("speedup"))
    ]
    if not measured:
        return _item("multiplexing", title, text, _RUN_BATCH)
    best = max(measured, key=lambda batch: batch["speedup"])
    label = PROTOCOL_LABELS[best["protocol"]]
    return _item(
        "multiplexing",
        title,
        text,
        f"Dernier lot asynchrone via {label} : {fr_number(best['count'])} appels {best['method']} terminés en "
        f"{fr_duration(best['wall_ms'])} sur une seule connexion, pour {fr_duration(best['sum_ms'])} de durées "
        "cumulées.",
        _metric(f"Gain d’un lot de {fr_number(best['count'])} appels simultanés — {label}", best["speedup"], "×"),
        "calls",
    )


def _typed_contract(contract: Results) -> dict[str, Any]:
    title = "Contrat typé, code généré"
    text = (
        "L’IDL décrit procédures et messages une fois pour toutes : protoc en tire le stub et le squelette, une "
        "valeur hors contrat est refusée avant tout envoi, et un champ ajouté selon les règles reste lisible par "
        "les anciens clients."
    )
    compatible = [result for result in contract.values() if result.get("outcome") == "compatible"]
    if not compatible:
        return _item("typed_contract", title, text, _RUN_CONTRACT)
    titles = " ; ".join(f"« {result['title']} »" for result in compatible)
    return _item(
        "typed_contract",
        title,
        text,
        f"Évolution compatible observée : {titles}. Le client v1 obtient le même résultat face au serveur v2.",
        _metric("Évolutions de contrat absorbées sans erreur", len(compatible), "scénarios"),
        "contract",
    )


# --- Inconvénients -------------------------------------------------------------------

def _remote_cost(report: Report | None) -> dict[str, Any]:
    title = "Un appel distant n’est pas un appel local"
    text = (
        "Sérialisation, appels système, traversée de la pile réseau : même sans latence, l’appel distant coûte "
        "bien plus cher que l’appel de fonction qu’il imite — et rien, dans le code appelant, ne le signale."
    )
    local = next((result for result in _latency_results(report) if result["protocol"] == "local"), None)
    remote = _latency_results(report, remote_only=True)
    if local is None or not remote or not local["mean_ms"]:
        return _item("remote_cost", title, text, _RUN_BENCHMARK)
    fastest = remote[0]
    factor = fastest["mean_ms"] / local["mean_ms"]
    return _item(
        "remote_cost",
        title,
        text,
        f"{report['latency'].get('method', '')} : {fr_duration(local['mean_ms'])} en appel local, "
        f"{fr_duration(fastest['mean_ms'])} avec {fastest['label']}, le plus rapide des protocoles distants "
        f"mesurés, soit {fr_ratio(factor)}.",
        _metric(f"Surcoût de {fastest['label']} par rapport à l’appel local", round(factor, 1), "×"),
        "benchmark",
    )


def _latency_trap(report: Report | None, failures: Results) -> dict[str, Any]:
    identifier, title = "latency_trap", "La latence se paie à chaque appel"
    text = (
        "Une boucle anodine en local devient N allers-retours sur le réseau. Une interface distante se conçoit "
        "à gros grain : un appel qui rapporte tout, plutôt que N appels bavards."
    )
    scenario = failures.get("latency_trap")
    if scenario is not None:
        slowdown = scenario["metrics"].get("slowdown_x")
        metric = _metric("Ralentissement de la boucle distante", slowdown, "×") if _number(slowdown) else None
        return _item(identifier, title, text, scenario["verdict"], metric, "failures")
    # À défaut du scénario, le balayage réseau du banc d'essai montre la même chose, appel par appel.
    network = (report or {}).get("network") or {}
    points = [point for point in network.get("points") or () if point["latency_ms"] > 0]
    slowest = max(points, key=lambda point: point["latency_ms"], default=None)
    remote = [] if slowest is None else [
        mean for protocol, mean in slowest["results"].items() if protocol != "local" and _number(mean)
    ]
    if not remote:
        return _item(identifier, title, text, _RUN_FAILURE.format(title=_FAILURE_TITLES["latency_trap"]))
    mean = math.fsum(remote) / len(remote)
    added = fr_compact(slowest["latency_ms"])
    return _item(
        identifier,
        title,
        text,
        f"Avec {added} ms de latence ajoutée, un appel {network.get('method', '')} dure en moyenne "
        f"{fr_duration(mean)} à distance (moyenne des protocoles mesurés par le banc d’essai).",
        _metric(f"Appel distant avec {added} ms de latence", round(mean, 3), "ms"),
        "benchmark",
    )


@dataclass(frozen=True)
class _ScenarioArgument:
    """Inconvénient démontré par un scénario de panne : son verdict fait preuve, l'une de ses mesures le chiffre."""

    identifier: str
    scenario: str           # identifiant du scénario (``failure_simulation.SCENARIOS``)
    title: str
    text: str
    metric: str             # mesure du scénario mise en avant
    metric_label: str
    unit: str

    def build(self, failures: Results) -> dict[str, Any]:
        result = failures.get(self.scenario)
        if result is None:
            hint = _RUN_FAILURE.format(title=_FAILURE_TITLES[self.scenario])
            return _item(self.identifier, self.title, self.text, hint)
        value = result["metrics"].get(self.metric)
        metric = _metric(self.metric_label, value, self.unit) if _number(value) else None
        return _item(self.identifier, self.title, self.text, result["verdict"], metric, "failures")


_SCENARIO_ARGUMENTS: tuple[_ScenarioArgument, ...] = (
    _ScenarioArgument(
        "unknown_outcome", "timeout_spike",
        "Après un timeout, l’issue est inconnue",
        "Une échéance libère l’appelant, mais ne dit pas si le serveur a exécuté l’appel. Cet état « ni réussi "
        "ni échoué » n’existe pas pour un appel local.",
        "with_deadline_ms", "Attente de l’appelant avec une échéance", "ms",
    ),
    _ScenarioArgument(
        "partial_failure", "connection_cut",
        "Des pannes partielles",
        "Le réseau peut échouer alors que le client et le serveur sont corrects : chaque appel distant doit "
        "prévoir l’erreur, l’attente et la nouvelle tentative.",
        "resilient_attempts", "Tentatives nécessaires pour aboutir", "tentatives",
    ),
    _ScenarioArgument(
        "outage", "server_outage",
        "Un serveur en panne retient ses appelants",
        "Sans garde-fou, chaque appelant attend l’épuisement de ses tentatives. Il faut un disjoncteur pour "
        "échouer vite — une machinerie de plus à écrire, régler et surveiller.",
        "slow_fail_ms", "Échec après épuisement des tentatives", "ms",
    ),
    _ScenarioArgument(
        "duplicate_execution", "duplicate_execution",
        "Réessayer peut exécuter deux fois",
        "Après une erreur réseau, l’appelant ignore si l’opération a eu lieu. Rejouer une écriture non "
        "idempotente l’applique deux fois, sauf à concevoir le serveur pour reconnaître les rejeux.",
        "naive_executions", "Exécutions d’un seul update_stock rejoué sans clé", "exécutions",
    ),
)


def _contract_coupling(contract: Results) -> dict[str, Any]:
    title = "Couplage fort au contrat"
    text = (
        "Client et serveur partagent noms de méthodes, numéros de champs et types. Une évolution non coordonnée "
        "casse les clients déjà déployés — au mieux par un rejet explicite, au pire par une corruption "
        "silencieuse des données."
    )
    broken = [result for result in contract.values() if result.get("outcome") != "compatible"]
    if broken:
        silent = [result for result in broken if result.get("outcome") == "silent_corruption"]
        evidence = (
            f"{_counted(len(broken), 'rupture observée', 'ruptures observées')} sur "
            f"{_counted(len(contract), 'scénario joué', 'scénarios joués')}"
        )
        if silent:
            evidence += (
                f", dont {_counted(len(silent), 'corruption silencieuse', 'corruptions silencieuses')} "
                f"(« {silent[0]['title']} ») : l’appel réussit, les données sont fausses"
            )
        metric = _metric("Scénarios de rupture observés", len(broken), f"sur {len(contract)}")
        return _item("contract_coupling", title, text, evidence + ".", metric, "contract")
    facts = _contract_facts()
    return _item(
        "contract_coupling",
        title,
        text,
        f"Le diff des contrats v1 et v2 de ce laboratoire compte {facts['breaking']} changements incompatibles "
        f"pour {facts['compatible']} compatibles. " + _RUN_CONTRACT,
        _metric("Changements incompatibles entre les contrats v1 et v2", facts["breaking"], "changements"),
        "code",
    )


def _opaque_messages() -> dict[str, Any]:
    return _item(
        "opaque_messages",
        "Messages opaques, outillage spécifique",
        "Un message Protobuf ne se lit pas sans son contrat : ni curl ni la console d’un navigateur ne "
        "suffisent, il faut des outils dédiés — et gRPC-Web plus un proxy pour atteindre un navigateur.",
        "Page « Sous le capot » : les octets d’un appel gRPC ne deviennent lisibles qu’une fois décodés champ "
        "par champ à l’aide du contrat, alors que le JSON des deux autres protocoles se lit tel quel.",
    )


# --- Matrice comparative -------------------------------------------------------------

def _comparison(report: Report | None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = [
        {"criterion": criterion, **dict(zip(PROTOCOLS, cells)), "measured": False} for criterion, *cells in _MATRIX
    ]
    missing = dict.fromkeys(PROTOCOLS, "—")
    row = _response_row(report)
    if row is not None:
        rows.append({
            "criterion": "Réponse d’une fiche produit (mesurée)",
            **missing,
            "custom": fr_bytes(row.get("json_rpc_bytes")),
            "grpc": fr_bytes(row.get("grpc_bytes")),
            "rest": fr_bytes(row.get("rest_bytes")),
            "measured": True,
        })
    wire = {entry["protocol"]: entry for entry in _wire_rows(report)}
    if wire:
        rows.append({
            "criterion": "Octets réels sur TCP par appel (mesurés)",
            **missing,
            **{protocol: fr_bytes(entry["total_per_call"]) for protocol, entry in wire.items()},
            "measured": True,
        })
    latency = {result["protocol"]: result for result in _latency_results(report)}
    if latency:
        rows.append({
            "criterion": "Temps moyen par appel (mesuré)",
            **missing,
            **{protocol: fr_duration(result["mean_ms"]) for protocol, result in latency.items()},
            "measured": True,
        })
    return rows


# --- Bilan ---------------------------------------------------------------------------

def build_summary(
    report: Report | None, failures: Results, contract: Results, batches: Results | None = None
) -> dict[str, Any]:
    """Bilan complet (``GET /api/summary``).

    ``report`` est le dernier rapport de banc d'essai (ou ``None``) ; ``failures`` et
    ``contract`` les derniers résultats de scénarios, par identifiant ; ``batches`` le
    dernier lot d'appels asynchrones de chaque protocole. ``measured`` indique qu'au moins
    une de ces expériences alimente le bilan.
    """
    batches = batches or {}
    advantages = [
        _transparency(),
        _compact_messages(report),
        _light_framing(report),
        _speed(report),
        _multiplexing(batches),
        _typed_contract(contract),
    ]
    drawbacks = [
        _remote_cost(report),
        _latency_trap(report, failures),
        *(argument.build(failures) for argument in _SCENARIO_ARGUMENTS),
        _contract_coupling(contract),
        _opaque_messages(),
    ]
    return {
        "measured": bool(report or failures or contract or batches),
        "advantages": advantages,
        "drawbacks": drawbacks,
        "comparison": _comparison(report),
        "protocols": [{"id": protocol, "label": PROTOCOL_LABELS[protocol]} for protocol in PROTOCOLS],
        "sources": {
            "benchmark_id": report.get("id") if report else None,
            "benchmark_created_at": report.get("created_at") if report else None,
            "failure_scenarios": list(failures),
            "contract_scenarios": list(contract),
            "async_batches": list(batches),
        },
    }
