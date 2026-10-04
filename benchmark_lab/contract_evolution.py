"""Évolution de contrat : que se passe-t-il quand le serveur change sans prévenir ses clients ?

    python -m benchmark_lab.contract_evolution [--scenario ID] [--diff]

Le couplage fort au contrat est le second inconvénient du RPC que le laboratoire
démontre, après le piège de la transparence réseau. Un client RESTÉ au contrat
v1 appelle les serveurs « contrat v2 », et l'on observe les quatre issues
possibles, de la plus saine à la plus dangereuse :

* ``compatible``         l'évolution était sûre : l'ancien client ne remarque rien ;
* ``rejected``           le serveur refuse l'appel : la panne est visible ;
* ``crash``              l'appel réussit, c'est le code du client qui casse ensuite ;
* ``silent_corruption``  tout « réussit », mais les données sont fausses.

Le module a trois parties :

1. ``diff_contracts`` compare deux contrats Protobuf compilés (leurs
   *descriptors*) et classe chaque différence ; ``contract_overview`` y ajoute
   les sources, leur diff ligne à ligne, les ruptures du serveur JSON-RPC et
   les règles d'or de l'évolution d'un contrat ;
2. ``run_contract_scenario`` joue un scénario sur un ``LabRuntime`` et confronte
   trois choses : ce qu'un serveur v1 aurait fait (le même appel rejoué sur un
   inventaire de référence), ce que le client v1 a obtenu, et la vérité lue dans
   l'inventaire du serveur v2. L'issue est DÉDUITE de cette confrontation, elle
   n'est jamais déclarée d'avance ;
3. ``main`` affiche le tout dans le terminal.
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import shutil
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

from google.protobuf import message_factory
from google.protobuf.descriptor import (
    Descriptor,
    FieldDescriptor,
    FileDescriptor,
    MethodDescriptor,
    ServiceDescriptor,
)
from google.protobuf.descriptor_pb2 import FieldDescriptorProto
from rich import box
from rich.console import Console, Group
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from common.config import APP_NAME, PROTOCOL_LABELS, ROOT_DIR
from common.errors import OK, RpcError, RpcRemoteError
from common.inventory import InventoryService
from common.telemetry import EventBus
from lab import LabRuntime
from rpc_custom import protocol as jsonrpc
from rpc_grpc import converters as conv
from rpc_grpc.generated import service_pb2 as pb_v1
from rpc_grpc.generated import service_v2_pb2 as pb_v2
from rpc_grpc.interceptors import message_preview
from rpc_grpc.wire_inspector import (
    FRAME_HEADER_SIZE,
    I32,
    I64,
    LEN,
    VARINT,
    WIRE_TYPE_NAMES,
    decode_wire,
    segments_for,
)

PROTO_DIR = ROOT_DIR / "rpc_grpc" / "protos"
PROTO_V1 = PROTO_DIR / "service.proto"
PROTO_V2 = PROTO_DIR / "service_v2.proto"

BREAKING, COMPATIBLE = "breaking", "compatible"
CRITICAL, MAJOR, MINOR, INFO = "critical", "major", "minor", "info"

# Les quatre issues possibles d'un appel v1 → v2 ; « danger » croît avec la gravité.
OUTCOME_INFO: dict[str, dict[str, Any]] = {
    "compatible": {
        "label": "Compatible",
        "danger": 0,
        "text": "L’ancien client fonctionne sans rien remarquer.",
    },
    "rejected": {
        "label": "Rejet explicite",
        "danger": 1,
        "text": "Le serveur refuse l’appel : la panne est visible, rien n’est corrompu.",
    },
    "crash": {
        "label": "Plantage du client",
        "danger": 2,
        "text": "L’appel réussit, puis le code du client échoue en lisant le résultat.",
    },
    "silent_corruption": {
        "label": "Corruption silencieuse",
        "danger": 3,
        "text": "Tout semble réussir, mais les données sont fausses : le pire des cas.",
    },
}
OUTCOMES: tuple[str, ...] = tuple(OUTCOME_INFO)


# =============================================================================
# 1. Comparaison des contrats
# =============================================================================

# --- Lecture d'un descriptor ---------------------------------------------------

_VARINT_TYPES = frozenset({"int32", "int64", "uint32", "uint64", "sint32", "sint64", "bool", "enum"})
_I64_TYPES = frozenset({"double", "fixed64", "sfixed64"})
_I32_TYPES = frozenset({"float", "fixed32", "sfixed32"})
# Types que Protobuf garantit relisibles l'un par l'autre : même type de fil ET même encodage.
_INTERCHANGEABLE: tuple[frozenset[str], ...] = (
    frozenset({"int32", "int64", "uint32", "uint64", "bool"}),
    frozenset({"sint32", "sint64"}),
    frozenset({"string", "bytes"}),
    frozenset({"fixed32", "sfixed32"}),
    frozenset({"fixed64", "sfixed64"}),
)


def _scalar(descriptor: FieldDescriptor) -> str:
    """Type de base du champ, écrit comme dans un ``.proto`` (« sint32 », « string », « message »…)."""
    return FieldDescriptorProto.Type.Name(descriptor.type).removeprefix("TYPE_").lower()


def _type_name(descriptor: FieldDescriptor) -> str:
    referenced = descriptor.message_type or descriptor.enum_type
    return referenced.name if referenced is not None else _scalar(descriptor)


def _wire_type(descriptor: FieldDescriptor) -> int:
    """Type de fil du champ : la seule information de type qui voyage réellement sur le réseau."""
    scalar = _scalar(descriptor)
    if descriptor.is_repeated:
        return LEN   # proto3 : un champ répété voyage en blocs LEN (éléments « packed » ou un bloc par élément)
    if scalar in _VARINT_TYPES:
        return VARINT
    if scalar in _I64_TYPES:
        return I64
    return I32 if scalar in _I32_TYPES else LEN


def _tag(descriptor: FieldDescriptor) -> str:
    """Tag du champ tel qu'il apparaît sur le fil : varint de ``(numéro << 3) | type de fil``."""
    value, encoded = (descriptor.number << 3) | _wire_type(descriptor), bytearray()
    while True:
        value, low = value >> 7, value & 0x7F
        encoded.append(low | (0x80 if value else 0))
        if not value:
            return "0x" + encoded.hex()


def _wire_name(descriptor: FieldDescriptor) -> str:
    return WIRE_TYPE_NAMES[_wire_type(descriptor)]


def _default(descriptor: FieldDescriptor) -> str:
    """Valeur qu'un lecteur proto3 obtient quand le champ est absent du message."""
    if descriptor.is_repeated:
        return "liste vide"
    if descriptor.message_type is not None:
        return "message absent"
    scalar = _scalar(descriptor)
    if scalar in ("string", "bytes"):
        return "chaîne vide"
    if scalar == "bool":
        return "false"
    return "0.0" if scalar in ("double", "float") else "0"


def _declaration(descriptor: FieldDescriptor) -> str:
    repeated = "repeated " if descriptor.is_repeated else ""
    return f"{repeated}{_type_name(descriptor)} {descriptor.name} = {descriptor.number};"


def _rpc_declaration(method: MethodDescriptor) -> str:
    request = ("stream " if method.client_streaming else "") + method.input_type.name
    reply = ("stream " if method.server_streaming else "") + method.output_type.name
    return f"rpc {method.name} ({request}) returns ({reply});"


def _rpc_signature(method: MethodDescriptor) -> tuple[str, str, bool, bool]:
    return method.input_type.name, method.output_type.name, method.client_streaming, method.server_streaming


def _rpc_path(method: MethodDescriptor) -> str:
    """Chemin HTTP/2 de la méthode : c'est lui, et lui seul, qui désigne la procédure à gRPC."""
    return f"/{method.containing_service.full_name}/{method.name}"


def _snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def _typed(descriptor: FieldDescriptor) -> str:
    return f"« {descriptor.name} » ({_type_name(descriptor)})"


def _sentence(text: str) -> str:
    return text[:1].upper() + text[1:]


# --- Ce que chaque règle enseigne ------------------------------------------------

_LESSONS: dict[str, str] = {
    "package_renamed": (
        "Changer de package est la façon propre de publier une version incompatible : les deux services ont "
        "des chemins distincts et peuvent être servis côte à côte pendant que les clients migrent. Le serveur "
        "v2 du laboratoire fait l’inverse — il publie les messages v2 sous le nom de service v1 — et c’est ce "
        "qui rend chaque rupture invisible jusqu’à l’appel."
    ),
    "rpc_renamed": (
        "Protobuf ignore les noms de champs, mais pour gRPC le nom d’une RPC est son adresse. La renommer "
        "revient à la supprimer pour tous les clients déjà déployés. On ajoute la nouvelle RPC, on garde "
        "l’ancienne (marquée « deprecated ») et on ne la retire qu’une fois tous les clients migrés."
    ),
    "rpcs_removed": (
        "Supprimer une RPC est toujours une rupture pour qui l’appelle encore. On la déprécie d’abord, on "
        "mesure son usage, on la retire en dernier — quand plus personne ne l’appelle."
    ),
    "rpcs_added": (
        "Ajouter une RPC ne change rien pour les clients existants : ils n’appellent pas ce chemin. C’est "
        "l’outil normal d’une évolution compatible."
    ),
    "rpc_retyped": (
        "Le chemin de la méthode n’a pas changé, donc rien ne signale la rupture : le serveur décode avec "
        "son nouveau message ce que le client a sérialisé avec l’ancien. Changer la signature d’une RPC "
        "revient à en créer une autre : il lui faut un autre nom."
    ),
    "number_reused": (
        "Sur le fil, un champ n’est identifié que par son numéro. Le réattribuer fait lire à l’un ce que "
        "l’autre n’a pas écrit, sans aucune erreur de décodage : la donnée de l’ancien champ est perdue ou "
        "prise pour une autre. Un numéro retiré doit être déclaré « reserved » pour que protoc refuse de le "
        "réutiliser."
    ),
    "type_changed": (
        "Le type d’un champ fixe la façon de trouver et de décoder sa valeur. Pour changer de représentation "
        "(ici des euros en virgule flottante vers des centimes entiers), on ajoute un nouveau champ sous un "
        "nouveau numéro et l’on continue de remplir l’ancien pendant la migration."
    ),
    "field_renamed": (
        "Les noms ne voyagent pas : deux versions qui ne diffèrent que par un nom de champ échangent les "
        "mêmes octets. Seuls le code généré et la représentation JSON du message changent — c’est une "
        "rupture de code source, pas une rupture de fil."
    ),
    "field_moved": (
        "Le nom d’un champ n’existe que dans le code généré ; son identité sur le fil est son numéro. "
        "Déplacer un champ, c’est le supprimer et en créer un autre : les deux versions ne se comprennent "
        "plus sur cette donnée, et rien ne le signale."
    ),
    "field_added": (
        "C’est la seule évolution réellement sûre : un nouveau champ, sous un numéro jamais utilisé. Les "
        "anciens lecteurs sautent ce qu’ils ne connaissent pas, les nouveaux lisent une valeur par défaut "
        "quand le champ est absent — à condition que cette valeur par défaut ait un sens métier."
    ),
    "field_removed": (
        "Retirer un champ ne provoque aucune erreur de décodage, mais la donnée disparaît en silence pour "
        "ceux qui l’utilisent encore. Le numéro et le nom retirés doivent être déclarés « reserved »."
    ),
    "messages_removed": (
        "Ces messages n’étaient utilisés que par des RPC supprimées : leur disparition n’a pas d’effet "
        "propre sur le fil, elle n’est que la conséquence de ces suppressions."
    ),
    "messages_added": (
        "Un nouveau type de message n’affecte aucun échange existant tant qu’aucun champ ni aucune RPC "
        "déjà publiés ne s’en servent."
    ),
}


def _change(
    rule: str,
    suffix: str,
    *,
    kind: str,
    severity: str,
    scope: str,
    title: str,
    element: str,
    v1: str | None,
    v2: str | None,
    wire_effect: str,
) -> dict[str, Any]:
    """Un changement de contrat. ``scope`` est le bloc du ``.proto`` qui le contient (« » : le fichier)."""
    return {
        "id": f"{rule}_{suffix}" if suffix else rule,
        "contract": "protobuf",
        "rule": rule,
        "kind": kind,
        "severity": severity,
        "scope": scope,
        "title": title,
        "element": element,
        "v1": v1,
        "v2": v2,
        "wire_effect": wire_effect,
        "explanation": _LESSONS[rule],
    }


# --- Services ---------------------------------------------------------------------

def _service_changes(old: ServiceDescriptor | None, new: ServiceDescriptor | None) -> Iterator[dict[str, Any]]:
    """Différences entre deux versions d'un service ; ``None`` : le service n'existe pas de ce côté."""
    before = list(old.methods) if old is not None else []
    kept = new.methods_by_name if new is not None else {}
    known = {method.name for method in before}
    fresh = [method for method in (new.methods if new is not None else ()) if method.name not in known]
    removed: list[MethodDescriptor] = []
    for method in before:
        successor = kept.get(method.name)
        if successor is not None:
            if _rpc_signature(method) != _rpc_signature(successor):
                yield _change(
                    "rpc_retyped", _snake(method.name),
                    kind=BREAKING, severity=CRITICAL, scope=old.name,
                    title=f"Signature modifiée : {method.name}",
                    element=f"service {old.name} · {method.name}",
                    v1=_rpc_declaration(method), v2=_rpc_declaration(successor),
                    wire_effect=(
                        f"Le chemin {_rpc_path(method)} existe toujours, mais le serveur v2 y attend "
                        f"{successor.input_type.name} et y répond {successor.output_type.name} : chaque côté "
                        "décode avec son propre contrat des octets écrits avec l’autre."
                    ),
                )
            continue
        # Même signature sous un autre nom : c'est un renommage, pas une suppression suivie d'un ajout.
        twin = next((candidate for candidate in fresh if _rpc_signature(candidate) == _rpc_signature(method)), None)
        if twin is None:
            removed.append(method)
            continue
        fresh.remove(twin)
        yield _change(
            "rpc_renamed", _snake(method.name),
            kind=BREAKING, severity=MAJOR, scope=old.name,
            title=f"RPC renommée : {method.name} → {twin.name}",
            element=f"service {old.name} · {method.name}",
            v1=_rpc_declaration(method), v2=_rpc_declaration(twin),
            wire_effect=(
                f"Le nom de la méthode voyage dans le chemin HTTP/2 de l’appel (:path = {_rpc_path(method)}). "
                "Le serveur v2 ne publie plus ce chemin : il répond UNIMPLEMENTED sans rien exécuter."
            ),
        )
    if removed:
        names = ", ".join(method.name for method in removed)
        yield _change(
            "rpcs_removed", _snake(old.name),
            kind=BREAKING, severity=MAJOR, scope=old.name,
            title=f"{_count(len(removed), 'RPC supprimée', 'RPC supprimées')} : {names}",
            element=f"service {old.name}",
            v1="\n".join(_rpc_declaration(method) for method in removed), v2=None,
            wire_effect=(
                "Le serveur v2 ne publie plus ces chemins : tout appel reçoit le statut UNIMPLEMENTED, "
                "sans qu’aucune procédure ne soit exécutée."
            ),
        )
    if fresh:
        names = ", ".join(method.name for method in fresh)
        yield _change(
            "rpcs_added", _snake(new.name),
            kind=COMPATIBLE, severity=INFO, scope=new.name,
            title=f"{_count(len(fresh), 'RPC ajoutée', 'RPC ajoutées')} : {names}",
            element=f"service {new.name}",
            v1=None, v2="\n".join(_rpc_declaration(method) for method in fresh),
            wire_effect="Aucun client v1 n’appelle ces chemins : rien ne change pour eux.",
        )


def _count(count: int, singular: str, plural: str) -> str:
    return singular if count == 1 else f"{count} {plural}"


# --- Champs -----------------------------------------------------------------------

@dataclass(frozen=True)
class _Flow:
    """Sens de circulation d'un message : qui l'écrit, qui le lit — et donc quel contrat sert à quoi."""

    client_writes: bool

    @property
    def writer(self) -> str:
        return "un client v1" if self.client_writes else "le serveur v2"

    @property
    def reader(self) -> str:
        return "le serveur v2" if self.client_writes else "un client v1"

    def orient(self, old: FieldDescriptor, new: FieldDescriptor) -> tuple[FieldDescriptor, FieldDescriptor]:
        """(champ tel qu'il est écrit, champ tel qu'il est lu) : requête v1 → v2, réponse v2 → v1."""
        return (old, new) if self.client_writes else (new, old)


def _client_written(contract: FileDescriptor) -> set[str]:
    """Noms des messages qu'un client de ce contrat écrit : les requêtes et tout ce qu'elles imbriquent."""
    written: set[str] = set()

    def visit(message: Descriptor) -> None:
        if message.name not in written:
            written.add(message.name)
            for descriptor in message.fields:
                if descriptor.message_type is not None:
                    visit(descriptor.message_type)

    for service in contract.services_by_name.values():
        for method in service.methods:
            visit(method.input_type)
    return written


def _mismatch_effect(flow: _Flow, written: FieldDescriptor, read: FieldDescriptor) -> tuple[str, bool]:
    """Ce qui arrive à un champ écrit sous un contrat et lu sous l'autre ; le booléen dit si c'est sans danger."""
    number = written.number
    if _wire_type(written) != _wire_type(read):
        return (
            f"{_sentence(flow.writer)} écrit le tag {_tag(written)} (n°{number}, {_wire_name(written)}) ; "
            f"{flow.reader} attend {_tag(read)} (n°{number}, {_wire_name(read)}). Le type de fil ne correspond "
            f"pas : le champ est rangé parmi les inconnus et « {read.name} » garde sa valeur par défaut "
            f"({_default(read)}).",
            False,
        )
    if _type_name(written) == _type_name(read):
        return (
            f"Même numéro, même type de fil ({_wire_name(read)}, tag {_tag(read)}) : aucun décodeur ne peut "
            f"voir la différence. {_sentence(flow.reader)} lit dans « {read.name} » la valeur que "
            f"{flow.writer} a écrite pour « {written.name} ».",
            False,
        )
    if any({_scalar(written), _scalar(read)} <= family for family in _INTERCHANGEABLE):
        return (
            f"Même tag {_tag(read)} et même encodage : {_type_name(written)} et {_type_name(read)} sont "
            "interchangeables sur le fil. La valeur est relue correctement tant qu’elle tient dans le type "
            "le plus étroit.",
            True,
        )
    return (
        f"Même tag {_tag(read)} des deux côtés, mais {flow.writer} encode un {_type_name(written)} que "
        f"{flow.reader} décode comme un {_type_name(read)} : la valeur est lue sans erreur — et fausse.",
        False,
    )


def _field_changes(old: Descriptor, new: Descriptor, flow: _Flow) -> Iterator[dict[str, Any]]:
    """Différences entre deux versions d'un message, numéro par numéro — comme les voit le fil."""
    message, slug = old.name, _snake(old.name)
    for number in sorted({*old.fields_by_number, *new.fields_by_number}):
        before, after = old.fields_by_number.get(number), new.fields_by_number.get(number)
        element = f"{message} · champ n°{number}"
        if before is not None and after is not None:
            same_type = (_type_name(before), before.is_repeated) == (_type_name(after), after.is_repeated)
            if same_type and before.name == after.name:
                continue
            # L'ancien champ existe toujours ailleurs : son numéro a été repris par une AUTRE donnée.
            reused = before.name != after.name and before.name in new.fields_by_name
            if same_type and not reused:
                yield _change(
                    "field_renamed", f"{slug}_{number}",
                    kind=COMPATIBLE, severity=MINOR, scope=message,
                    title=f"Champ renommé au n°{number} : « {before.name} » devient « {after.name} »",
                    element=element, v1=_declaration(before), v2=_declaration(after),
                    wire_effect=(
                        f"Même tag {_tag(after)}, même type : les octets échangés sont identiques. Seul le nom "
                        "de l’accesseur change dans le code généré."
                    ),
                )
                continue
            effect, harmless = _mismatch_effect(flow, *flow.orient(before, after))
            if reused:
                title = f"N°{number} réutilisé : {_typed(before)} devient {_typed(after)}"
            elif before.name == after.name:
                title = f"Type modifié : « {before.name} » passe de {_type_name(before)} à {_type_name(after)}"
            else:
                title = f"Type modifié au n°{number} : {_typed(before)} devient {_typed(after)}"
            safe = harmless and not reused
            yield _change(
                "number_reused" if reused else "type_changed", f"{slug}_{number}",
                kind=COMPATIBLE if safe else BREAKING, severity=MINOR if safe else CRITICAL, scope=message,
                title=title, element=element, v1=_declaration(before), v2=_declaration(after),
                wire_effect=effect,
            )
        elif after is not None:
            origin = old.fields_by_name.get(after.name)
            if origin is None:
                yield _change(
                    "field_added", f"{slug}_{number}",
                    kind=COMPATIBLE, severity=INFO, scope=message,
                    title=f"Champ ajouté : {_typed(after)} au n°{number}",
                    element=element, v1=None, v2=_declaration(after),
                    wire_effect=(
                        f"Un client v1 n’envoie pas ce champ : le serveur v2 lit sa valeur par défaut "
                        f"({_default(after)})."
                        if flow.client_writes else
                        f"Le serveur v2 écrit le tag {_tag(after)} (n°{number}, {_wire_name(after)}) ; un client "
                        "v1 ne connaît pas ce numéro : le type de fil lui indique combien d’octets sauter, et "
                        "il ignore le champ."
                    ),
                )
                continue
            yield _change(
                "field_moved", f"{slug}_{after.name}",
                kind=BREAKING, severity=CRITICAL, scope=message,
                title=f"« {after.name} » déplacé du n°{origin.number} au n°{number}",
                element=f"{message} · champ « {after.name} »",
                v1=_declaration(origin), v2=_declaration(after),
                wire_effect=(
                    f"Un client v1 écrit « {after.name} » au n°{origin.number} ; le serveur v2 le cherche au "
                    f"n°{number} (tag {_tag(after)}), ne l’y trouve pas et retient la valeur par défaut "
                    f"({_default(after)})."
                    if flow.client_writes else
                    f"Le serveur v2 écrit « {after.name} » au n°{number} (tag {_tag(after)}) ; un client v1 ne "
                    f"connaît pas ce numéro et ignore le champ. Il continue de lire le n°{origin.number}."
                ),
            )
        elif before.name not in new.fields_by_name:   # sinon le champ a été déplacé : signalé à son nouveau numéro
            yield _change(
                "field_removed", f"{slug}_{number}",
                kind=BREAKING, severity=MAJOR, scope=message,
                title=f"Champ supprimé : {_typed(before)} au n°{number}",
                element=element, v1=_declaration(before), v2=None,
                wire_effect=(
                    f"Un client v1 envoie encore le tag {_tag(before)} (n°{number}) ; le serveur v2 le range "
                    "parmi les champs inconnus : la donnée est perdue sans erreur."
                    if flow.client_writes else
                    f"Le serveur v2 n’écrit plus ce champ ; un client v1 lit sa valeur par défaut "
                    f"({_default(before)}) sans pouvoir distinguer « absent » de « {_default(before)} »."
                ),
            )


def diff_contracts(old: FileDescriptor, new: FileDescriptor) -> list[dict[str, Any]]:
    """Compare deux contrats Protobuf compilés et classe chaque différence.

    La comparaison se fait comme sur le fil : les RPC par leur nom (c'est leur
    chemin HTTP/2), les champs par leur NUMÉRO. Un même nom à deux numéros
    différents n'est donc pas « le même champ », et c'est précisément ce qui
    distingue une évolution compatible d'une rupture.

    Chaque changement vaut ``{id, contract, rule, kind, severity, scope, title,
    element, v1, v2, wire_effect, explanation}`` ; ``v1`` / ``v2`` sont les
    déclarations telles qu'on les lit dans les ``.proto`` (``None`` si absentes).
    """
    changes: list[dict[str, Any]] = []
    if old.package != new.package:
        changes.append(_change(
            "package_renamed", "",
            kind=COMPATIBLE, severity=INFO, scope="",
            title=f"Package renommé : {old.package} → {new.package}",
            element="package",
            v1=f"package {old.package};", v2=f"package {new.package};",
            wire_effect=(
                f"Le package fait partie du chemin de chaque méthode (/{new.package}.Service/Méthode). Servi "
                "sous son nouveau nom, le service v2 serait simplement introuvable pour un client v1 "
                "(UNIMPLEMENTED sur tous les appels) — et pourrait cohabiter avec le service v1."
            ),
        ))
    for name in dict.fromkeys((*old.services_by_name, *new.services_by_name)):
        changes.extend(_service_changes(old.services_by_name.get(name), new.services_by_name.get(name)))

    # Le client étant resté à l'ancien contrat, c'est lui qui dit quels messages sont des requêtes.
    written = _client_written(old)
    for name, message in old.message_types_by_name.items():
        successor = new.message_types_by_name.get(name)
        if successor is not None:
            changes.extend(_field_changes(message, successor, _Flow(client_writes=name in written)))
    for rule, source, other, title, effect in (
        ("messages_removed", old, new, ("Message supprimé", "messages supprimés"),
         "Aucun : le contrat v2 n’a plus de RPC qui échange ces messages."),
        ("messages_added", new, old, ("Message ajouté", "messages ajoutés"),
         "Aucun : les clients v1 n’appellent aucune RPC qui échange ces messages."),
    ):
        names = [name for name in source.message_types_by_name if name not in other.message_types_by_name]
        if names:
            declarations = "\n".join(f"message {name} {{ … }}" for name in names)
            changes.append(_change(
                rule, "",
                kind=COMPATIBLE, severity=INFO, scope="",
                title=f"{_count(len(names), *title)} : {', '.join(names)}",
                element="messages",
                v1=declarations if source is old else None, v2=declarations if source is new else None,
                wire_effect=effect,
            ))
    return changes


# --- Le contrat JSON-RPC : aucun fichier à comparer ------------------------------------
# Le RPC maison n'a pas d'IDL. Ses ruptures ne figurent dans aucun fichier que l'on pourrait
# comparer : elles sont relevées ici à la main (et vérifiées par les tests contre les deux
# serveurs réels). C'est tout le problème d'un contrat implicite.

_JSONRPC_CHANGES: tuple[dict[str, Any], ...] = (
    {
        "id": "jsonrpc_method_renamed",
        "contract": "jsonrpc",
        "rule": "method_renamed",
        "kind": BREAKING,
        "severity": MAJOR,
        "scope": "get_product_details",
        "title": "Procédure renommée : get_product_details → get_product",
        "element": "procédure get_product_details",
        "v1": "get_product_details(product_id)",
        "v2": "get_product(product_id)",
        "wire_effect": (
            'Le nom voyage en clair dans le message ({"method":"get_product_details"}). Le dispatcher v2 ne '
            "trouve aucune procédure sous ce nom et répond par l’erreur -32601 (Method not found)."
        ),
        "explanation": (
            "Sans IDL, rien ne signale le renommage avant l’exécution : ni compilation, ni stub à régénérer. "
            "Le rejet est au moins franc — l’appelant sait que rien n’a été fait."
        ),
    },
    {
        "id": "jsonrpc_param_required",
        "contract": "jsonrpc",
        "rule": "param_required",
        "kind": BREAKING,
        "severity": MAJOR,
        "scope": "update_stock",
        "title": "Paramètre obligatoire ajouté : « warehouse »",
        "element": "procédure update_stock · paramètres",
        "v1": 'update_stock(product_id, delta, idempotency_key="")',
        "v2": 'update_stock(product_id, delta, warehouse, idempotency_key="")',
        "wire_effect": (
            "Les paramètres nommés d’un client v1 ne se lient plus à la signature v2 : le squelette répond "
            "-32602 (Invalid params) sans appeler la procédure."
        ),
        "explanation": (
            "Ajouter un paramètre obligatoire casse tous les appelants existants. Doté d’une valeur par "
            "défaut, le même paramètre aurait été une évolution compatible."
        ),
    },
    {
        "id": "jsonrpc_result_keys_renamed",
        "contract": "jsonrpc",
        "rule": "result_keys_renamed",
        "kind": BREAKING,
        "severity": MAJOR,
        "scope": "list_products",
        "title": "Clés du résultat renommées : price → price_cents, stock → quantity",
        "element": "procédure list_products · résultat",
        "v1": '{"id": …, "name": …, "price": 129.9, "stock": 84, …}',
        "v2": '{"id": …, "name": …, "price_cents": 12990, "quantity": 84, …}',
        "wire_effect": (
            "L’appel réussit et le JSON est parfaitement valide : le middleware n’a rien à redire. C’est le "
            "code applicatif du client qui échoue ensuite, en lisant une clé qui n’existe plus (KeyError)."
        ),
        "explanation": (
            "JSON-RPC ne décrit pas la forme des résultats : ni le stub ni le squelette ne peuvent vérifier "
            "ce contrat-là. La rupture se manifeste loin de l’appel, par un plantage du client — ou pire, "
            "par une valeur par défaut silencieuse si son code utilise .get()."
        ),
    },
    {
        "id": "jsonrpc_result_key_added",
        "contract": "jsonrpc",
        "rule": "result_key_added",
        "kind": COMPATIBLE,
        "severity": INFO,
        "scope": "calculate_factorial",
        "title": "Clé ajoutée au résultat : « algorithm »",
        "element": "procédure calculate_factorial · résultat",
        "v1": '{"n", "result", "digits", "compute_us"}',
        "v2": '{"n", "result", "digits", "compute_us", "algorithm"}',
        "wire_effect": (
            "La clé supplémentaire voyage dans le JSON ; un client v1, qui ne lit que les clés qu’il "
            "connaît, ne la remarque pas."
        ),
        "explanation": (
            "Comme pour Protobuf, ajouter est compatible tant que le lecteur tolère l’inconnu. Un client qui "
            "validerait strictement la forme du résultat casserait ici."
        ),
    },
)

GOLDEN_RULES: tuple[dict[str, Any], ...] = (
    {
        "title": "Ne jamais réutiliser un numéro de champ",
        "good": False,
        "text": (
            "Le numéro EST l’identité du champ sur le fil. Le réattribuer fait lire à un ancien client une "
            "donnée qu’il prend pour une autre : ici « reserved » lu comme un stock, ou un « delta » ignoré."
        ),
    },
    {
        "title": "Ne jamais changer le type d’un champ existant",
        "good": False,
        "text": (
            "Un autre type, c’est le plus souvent un autre type de fil : l’ancien lecteur ignore le champ et "
            "retombe sur la valeur par défaut (un prix à 0.0). À type de fil égal, c’est pire : la valeur est "
            "décodée sans erreur, et fausse."
        ),
    },
    {
        "title": "Ne jamais renommer ni supprimer une RPC d’un seul coup",
        "good": False,
        "text": (
            "Le nom d’une RPC est son adresse. On ajoute la nouvelle, on déprécie l’ancienne "
            "(option deprecated = true), on la retire quand plus aucun client ne l’appelle."
        ),
    },
    {
        "title": "Ne jamais exiger ce que les anciens clients n’envoient pas",
        "good": False,
        "text": (
            "Un champ ou un paramètre devenu obligatoire rejette tous les appelants existants "
            "(INVALID_ARGUMENT, -32602). Toute nouveauté doit avoir une valeur par défaut acceptable."
        ),
    },
    {
        "title": "Ajouter les nouveaux champs sous de nouveaux numéros",
        "good": True,
        "text": (
            "Les anciens lecteurs sautent les champs inconnus, les nouveaux lisent la valeur par défaut des "
            "champs absents : la compatibilité tient dans les deux sens."
        ),
    },
    {
        "title": "Réserver les numéros et les noms retirés",
        "good": True,
        "text": (
            "« reserved 5; reserved \"stock\"; » : protoc refuse alors toute réutilisation. C’est le "
            "garde-fou qui manquait au contrat v2."
        ),
    },
    {
        "title": "Publier une rupture comme une nouvelle version, servie à côté de l’ancienne",
        "good": True,
        "text": (
            "Un nouveau package (rpcexplorer.v2) donne de nouveaux chemins : v1 et v2 cohabitent sur le même "
            "serveur et chaque client migre à son rythme."
        ),
    },
    {
        "title": "Valider les entrées côté serveur",
        "good": True,
        "text": (
            "Entre une corruption silencieuse et un rejet explicite, le rejet est toujours préférable : il "
            "transforme une donnée fausse en panne visible, donc corrigeable."
        ),
    },
    {
        "title": "Vérifier la compatibilité avant de déployer",
        "good": True,
        "text": (
            "Avec un IDL, un outil compare les deux contrats à la revue de code (c’est ce que fait "
            "diff_contracts ici, ou « buf breaking » dans l’industrie). Sans IDL — JSON-RPC, REST —, seuls "
            "des tests de contrat le peuvent."
        ),
    },
)


# --- Sources et diff ligne à ligne -------------------------------------------------

def line_diff(left: str, right: str) -> list[dict[str, Any]]:
    """Diff ligne à ligne de deux textes : ``[{kind, left_no, right_no, text}]``.

    ``kind`` vaut ``same`` (ligne commune, deux numéros), ``removed`` (présente
    à gauche seulement) ou ``added`` (à droite seulement). Relues dans l'ordre,
    les lignes ``same`` + ``removed`` redonnent le texte de gauche, et les
    lignes ``same`` + ``added`` celui de droite.
    """
    left_lines, right_lines = left.splitlines(), right.splitlines()
    rows: list[dict[str, Any]] = []
    # autojunk=False : sinon difflib écarte les lignes fréquentes (« } », lignes vides) et aligne mal les blocs.
    matcher = difflib.SequenceMatcher(None, left_lines, right_lines, autojunk=False)
    for operation, left_start, left_end, right_start, right_end in matcher.get_opcodes():
        if operation == "equal":
            rows.extend(
                {"kind": "same", "left_no": left_start + offset + 1, "right_no": right_start + offset + 1,
                 "text": left_lines[left_start + offset]}
                for offset in range(left_end - left_start)
            )
            continue
        rows.extend(
            {"kind": "removed", "left_no": index + 1, "right_no": None, "text": left_lines[index]}
            for index in range(left_start, left_end)
        )
        rows.extend(
            {"kind": "added", "left_no": None, "right_no": index + 1, "text": right_lines[index]}
            for index in range(right_start, right_end)
        )
    return rows


_BLOCK_OPENING = re.compile(r"\s*(?:message|service|enum)\s+(\w+)\s*\{")


def _canonical(declaration: str) -> str:
    """Déclaration sans commentaire ni espace, coupée après une accolade ouvrante : sa forme comparable."""
    code = re.sub(r"\s+", "", declaration.split("//", 1)[0])
    return code[: code.index("{") + 1] if "{" in code else code


def _declaration_lines(source: str) -> dict[tuple[str, str], int]:
    """Numéro de ligne de chaque déclaration d'un ``.proto``, indexé par (bloc englobant, déclaration)."""
    lines: dict[tuple[str, str], int] = {}
    blocks: list[str] = []
    for number, line in enumerate(source.splitlines(), start=1):
        code = _canonical(line)
        if not code:
            continue
        lines[(blocks[-1] if blocks else "", code)] = number
        opening = _BLOCK_OPENING.match(line)
        if opening:
            blocks.append(opening.group(1))
        elif code.startswith("}") and blocks:
            blocks.pop()
    return lines


def _read_proto(path: Path) -> dict[str, str]:
    return {"path": path.relative_to(ROOT_DIR).as_posix(), "source": path.read_text(encoding="utf-8")}


def contract_changes() -> list[dict[str, Any]]:
    """Tous les changements du contrat v2 : Protobuf (calculés par ``diff_contracts``) puis JSON-RPC.

    Chaque changement porte en plus ``scenarios`` : les scénarios qui le mettent en évidence.
    """
    changes = [*diff_contracts(pb_v1.DESCRIPTOR, pb_v2.DESCRIPTOR), *(dict(change) for change in _JSONRPC_CHANGES)]
    for change in changes:
        change["scenarios"] = [
            scenario.id for scenario in _SCENARIOS
            if change["id"] in (scenario.change_id, *scenario.related_change_ids)
        ]
    return changes


def contract_overview() -> dict[str, Any]:
    """Vue d'ensemble de l'évolution v1 → v2 (``GET /api/contract`` du dashboard).

    ``{proto_v1, proto_v2: {path, source}, diff, changes, rules, scenarios,
    outcomes, stats}``. Les changements Protobuf portent ``lines`` (numéros de
    ligne de leurs déclarations dans chaque source) et chaque ligne du diff porte
    ``change_ids`` : une interface peut relier un changement aux lignes qu'il touche.
    """
    sources = {"v1": _read_proto(PROTO_V1), "v2": _read_proto(PROTO_V2)}
    index = {side: _declaration_lines(proto["source"]) for side, proto in sources.items()}
    changes = contract_changes()
    touched: dict[tuple[str, int], list[str]] = {}
    for change in changes:
        change["lines"] = {side: [] for side in sources}
        if change["contract"] != "protobuf":
            continue
        for side in sources:
            for declaration in (change[side] or "").splitlines():
                number = index[side].get((change["scope"], _canonical(declaration)))
                if number is not None:
                    change["lines"][side].append(number)
                    touched.setdefault((side, number), []).append(change["id"])
    diff = line_diff(sources["v1"]["source"], sources["v2"]["source"])
    for row in diff:
        row["change_ids"] = list(dict.fromkeys(
            touched.get(("v1", row["left_no"]), []) + touched.get(("v2", row["right_no"]), [])
        ))
    return {
        "proto_v1": sources["v1"],
        "proto_v2": sources["v2"],
        "diff": diff,
        "changes": changes,
        "rules": [dict(rule) for rule in GOLDEN_RULES],
        "scenarios": [scenario.metadata() for scenario in _SCENARIOS],
        "outcomes": {outcome: dict(info) for outcome, info in OUTCOME_INFO.items()},
        "stats": {
            "breaking": sum(change["kind"] == BREAKING for change in changes),
            "compatible": sum(change["kind"] == COMPATIBLE for change in changes),
            "lines_removed": sum(row["kind"] == "removed" for row in diff),
            "lines_added": sum(row["kind"] == "added" for row in diff),
        },
    }


# =============================================================================
# 2. Scénarios : un client v1 face aux serveurs v2
# =============================================================================

PRODUCT_ID = "SKU-1001"
STOCK_DELTA = -3
LISTED_PRODUCTS = 3
FACTORIAL_N = 20

_PREVIEW_ITEMS = 8   # éléments conservés par champ répété dans la vue « message » d'une lecture
_SERVICE_V1: ServiceDescriptor = pb_v1.DESCRIPTOR.services_by_name["InventoryService"]
_SERVICE_V2: ServiceDescriptor = pb_v2.DESCRIPTOR.services_by_name["InventoryService"]
# Un scénario à la fois : « strict » et l'inventaire v2 sont un état partagé par tous les appelants.
_SCENARIO_LOCK = threading.Lock()

Inventory = dict[str, dict[str, Any]]   # référence → {"stock", "price"}


@dataclass
class _Evidence:
    """Tout ce qu'un scénario a permis d'observer, avant d'en tirer un verdict."""

    scenario: "_Scenario"
    reference: Any                  # résultat qu'un serveur v1 aurait renvoyé
    reference_view: Any             # ce que le client v1 en aurait retenu
    reference_state: Inventory      # inventaire du serveur v1 de référence après l'appel
    stock_before: Inventory         # inventaire du serveur v2 avant l'appel
    result: Any = None
    error: RpcError | None = None   # le serveur a refusé l'appel
    view: Any = None                # ce que le code du client v1 retient du résultat
    crash: Exception | None = None  # ce code a échoué en lisant le résultat
    truth: Inventory = field(default_factory=dict)   # inventaire du serveur v2 après l'appel
    call_id: str = ""
    duration_ms: float = 0.0
    wire: dict[str, Any] | None = None

    @property
    def status(self) -> str:
        """Statut tel que le protocole l'exprime : statut gRPC, code JSON-RPC, ou « OK »."""
        if self.error is None:
            return OK
        detail = self.error.detail if isinstance(self.error.detail, dict) else {}
        return str(detail.get("grpc_status") or detail.get("jsonrpc_code") or self.error.code)

    @property
    def outcome(self) -> str:
        if self.error is not None:
            return "rejected"
        if self.crash is not None:
            return "crash"
        # Ni erreur ni plantage : seule la comparaison avec le comportement v1 et avec la
        # vérité du serveur peut révéler que quelque chose s'est mal passé.
        if self.view != self.reference_view or self.truth != self.reference_state:
            return "silent_corruption"
        return "compatible"


@dataclass(frozen=True)
class _Scenario:
    id: str
    title: str
    protocol: str                           # « grpc » ou « custom »
    change_id: str
    expected_outcome: str
    summary: str
    method: str                             # procédure du contrat v1 appelée par le client
    params: dict[str, Any]
    read: Callable[[Any], Any]              # le code du client v1 qui exploite le résultat
    explain: Callable[[_Evidence], str]
    focus: str = "request"                  # l'échange (requête ou réponse) où la rupture se voit
    strict: bool = False                    # réglage du serveur gRPC v2 pendant le scénario
    related_change_ids: tuple[str, ...] = ()

    def metadata(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "protocol": self.protocol,
            "change_id": self.change_id,
            "related_change_ids": list(self.related_change_ids),
            "expected_outcome": self.expected_outcome,
            "summary": self.summary,
            "method": self.method,
            "params": dict(self.params),
            "strict": self.strict,
        }


# --- Le code du client v1 : ce qu'il lit dans un résultat ----------------------------

def _read_factorial(result: dict[str, Any]) -> dict[str, Any]:
    return {"n": result["n"], "result": result["result"], "digits": result["digits"]}


def _read_stock_update(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "previous_stock": result["previous_stock"],
        "new_stock": result["new_stock"],
        "delta": result["delta"],
        "applied": result["applied"],
    }


def _read_product(product: dict[str, Any]) -> Inventory:
    return {product["id"]: {"stock": product["stock"], "price": product["price"]}}


def _read_listing(result: dict[str, Any]) -> Inventory:
    return {reference: entry for product in result["products"] for reference, entry in _read_product(product).items()}


# --- Explications : chaque scénario commente ses propres observations -------------------

def _euros(amount: float) -> str:
    return f"{amount:.2f} €".replace(".", ",")


def _explain_renamed_rpc(evidence: _Evidence) -> str:
    return (
        f"Le client v1 appelle {evidence.wire['path']}. Le contrat v2 a renommé cette RPC en GetProduct : le "
        f"serveur ne publie plus l’ancien chemin et répond {evidence.status} avant même de décoder la requête. "
        "Rien n’est exécuté et le client le sait — c’est la rupture la moins dangereuse : une panne franche, "
        "visible dès le premier appel."
    )


def _explain_field_reuse_silent(evidence: _Evidence) -> str:
    reference, delta = evidence.scenario.params["product_id"], evidence.scenario.params["delta"]
    written = next(node for node in evidence.wire["request"]["as_v1"]["fields"] if node["name"] == "delta")
    before = evidence.stock_before[reference]["stock"]
    wanted, actual = evidence.reference_state[reference]["stock"], evidence.truth[reference]["stock"]
    return (
        f"Le client v1 demande update_stock({reference}, {delta}) et écrit « delta » au champ "
        f"n°{written['field']} : tag 0x{written['tag']['hex']} ({written['wire_type_name']}), valeur "
        f"0x{written['value']['hex']} (ZigZag de {delta}). Pour le contrat v2, le n°{written['field']} est la "
        "chaîne « warehouse » (LEN) : le type de fil ne correspond pas, le décodeur range le champ parmi les "
        "inconnus, et « delta » — déplacé au n°4 — garde sa valeur par défaut, 0. Le serveur applique donc une "
        f"variation de 0 et répond OK : le stock de {reference} est resté à {actual} alors qu’un serveur v1 "
        f"l’aurait fait passer de {before} à {wanted}. Aucune erreur, d’aucun côté : l’écart de "
        f"{abs(actual - wanted)} unités ne se verra qu’au prochain inventaire."
    )


def _explain_field_reuse_strict(evidence: _Evidence) -> str:
    reference = evidence.scenario.params["product_id"]
    return (
        "Mêmes octets que dans le scénario précédent, mais le serveur v2 valide maintenant ses entrées : "
        "« warehouse » (n°2) est vide, puisque le client v1 y a écrit un entier que le décodeur a ignoré. Le "
        f"serveur répond {evidence.status} ({evidence.error.message}) et n’applique rien : le stock de "
        f"{reference} reste à {evidence.truth[reference]['stock']}, et le client en est informé. Une validation "
        "applicative transforme la corruption silencieuse en rejet explicite."
    )


def _explain_added_field(evidence: _Evidence) -> str:
    view = evidence.view
    response = evidence.wire["response"]
    value = f" (« {response['as_v2']['message']['algorithm']} »)" if response else ""
    return (
        f"Le serveur v2 ajoute « algorithm » au n°5 de sa réponse{value}. Le client v1 ne connaît pas ce "
        "numéro : le type de fil (LEN) lui indique combien d’octets sauter, et il lit "
        f"{view['n']}! = {view['result']} ({view['digits']} chiffres), exactement ce qu’un serveur v1 aurait "
        "renvoyé. Un nouveau champ sous un nouveau numéro : la seule évolution qui ne casse personne."
    )


def _explain_type_change_read(evidence: _Evidence) -> str:
    reference, seen = next(iter(evidence.view.items()))
    real = evidence.truth[reference]
    wrong = sum(
        value != evidence.truth[product][key]
        for product, entry in evidence.view.items()
        for key, value in entry.items()
    )
    return (
        f"L’appel réussit (statut OK) et renvoie {len(evidence.view)} produits… faux. Pour {reference}, le "
        f"client v1 lit un prix de {_euros(seen['price'])} et un stock de {seen['stock']} ; le serveur détient "
        f"{_euros(real['price'])} et {real['stock']} unités. Le prix (n°4) est passé de double à int64 : type "
        "de fil différent, champ ignoré, valeur par défaut 0.0. Le n°5 porte désormais « reserved », que le "
        "client prend pour le stock ; le vrai stock voyage au n°15, qu’il ne connaît pas. "
        f"{wrong} valeurs sur {2 * len(evidence.view)} sont fausses, et rien ne le signale."
    )


def _explain_jsonrpc_required_param(evidence: _Evidence) -> str:
    reference = evidence.scenario.params["product_id"]
    request = evidence.wire["request"]["document"]["params"] if evidence.wire else evidence.scenario.params
    return (
        f"Le client v1 envoie update_stock avec les paramètres nommés {', '.join(request)}. La signature v2 "
        "exige en plus « warehouse » : la liaison des arguments échoue dans le squelette, qui répond "
        f"{evidence.status} ({evidence.error.message}) sans appeler la procédure. Le stock de {reference} "
        f"reste à {evidence.truth[reference]['stock']}. Faute d’IDL, rien n’annonçait cette rupture avant "
        "l’exécution."
    )


def _explain_jsonrpc_renamed_method(evidence: _Evidence) -> str:
    return (
        f"La requête porte \"method\": \"{evidence.scenario.method}\" ; le serveur v2 n’expose plus que "
        "get_product. Le dispatcher ne trouve aucune procédure sous ce nom et répond "
        f"{evidence.status} (Method not found) : un rejet franc, l’équivalent du statut UNIMPLEMENTED de gRPC."
    )


def _explain_jsonrpc_renamed_keys(evidence: _Evidence) -> str:
    crash = evidence.crash
    return (
        f"L’appel list_products RÉUSSIT : le serveur v2 renvoie {len(evidence.result['products'])} produits "
        "dans un JSON parfaitement valide. Mais leurs clés ont changé (price → price_cents, stock → quantity), "
        f"et le code du client qui lit product[{crash.args[0]!r}] lève {type(crash).__name__}. Ce n’est pas "
        "une erreur RPC : elle surgit dans le code applicatif, après l’appel, là où personne ne soupçonne un "
        "problème de contrat."
    )


def _explain_jsonrpc_added_key(evidence: _Evidence) -> str:
    view = evidence.view
    return (
        f"Le résultat v2 porte une clé de plus, « algorithm » (« {evidence.result.get('algorithm')} »). Le "
        f"client v1 ne lit que n, result et digits : il obtient {view['n']}! = {view['result']}, comme avant. "
        "Un lecteur tolérant, qui ignore les clés inconnues, rend l’ajout compatible — exactement comme les "
        "champs inconnus de Protobuf."
    )


_SCENARIOS: tuple[_Scenario, ...] = (
    _Scenario(
        id="renamed_rpc",
        title="RPC renommée",
        protocol="grpc",
        change_id="rpc_renamed_get_product_details",
        expected_outcome="rejected",
        summary="Le client v1 appelle GetProductDetails ; le serveur v2 ne connaît plus que GetProduct.",
        method="get_product_details",
        params={"product_id": PRODUCT_ID},
        read=_read_product,
        explain=_explain_renamed_rpc,
    ),
    _Scenario(
        id="field_reuse_silent",
        title="Numéro de champ réutilisé — serveur permissif",
        protocol="grpc",
        change_id="number_reused_update_stock_request_2",
        related_change_ids=("field_moved_update_stock_request_delta",),
        expected_outcome="silent_corruption",
        summary=(
            f"update_stock({STOCK_DELTA}) : le n°2 ne porte plus « delta » mais « warehouse ». "
            "Le serveur comprend delta = 0 et répond OK."
        ),
        method="update_stock",
        params={"product_id": PRODUCT_ID, "delta": STOCK_DELTA},
        read=_read_stock_update,
        explain=_explain_field_reuse_silent,
    ),
    _Scenario(
        id="field_reuse_strict",
        title="Numéro de champ réutilisé — serveur strict",
        protocol="grpc",
        change_id="number_reused_update_stock_request_2",
        related_change_ids=("field_moved_update_stock_request_delta",),
        expected_outcome="rejected",
        summary="Le même appel, face à un serveur qui valide « warehouse » : la corruption devient un rejet.",
        method="update_stock",
        params={"product_id": PRODUCT_ID, "delta": STOCK_DELTA},
        read=_read_stock_update,
        explain=_explain_field_reuse_strict,
        strict=True,
    ),
    _Scenario(
        id="added_field",
        title="Champ ajouté sous un nouveau numéro",
        protocol="grpc",
        change_id="field_added_factorial_reply_5",
        expected_outcome="compatible",
        summary="calculate_factorial : la réponse v2 porte un champ « algorithm » que le client v1 ne connaît pas.",
        method="calculate_factorial",
        params={"n": FACTORIAL_N},
        read=_read_factorial,
        explain=_explain_added_field,
        focus="response",
    ),
    _Scenario(
        id="type_change_read",
        title="Type et numéros modifiés dans une réponse",
        protocol="grpc",
        change_id="type_changed_product_4",
        related_change_ids=("number_reused_product_5", "field_moved_product_stock"),
        expected_outcome="silent_corruption",
        summary="list_products : le client v1 décode des Product v2 — prix à 0, stock lu dans le mauvais champ.",
        method="list_products",
        params={"limit": LISTED_PRODUCTS},
        read=_read_listing,
        explain=_explain_type_change_read,
        focus="response",
    ),
    _Scenario(
        id="jsonrpc_new_required_param",
        title="Paramètre obligatoire ajouté",
        protocol="custom",
        change_id="jsonrpc_param_required",
        expected_outcome="rejected",
        summary="update_stock exige désormais « warehouse » : les arguments d’un client v1 ne se lient plus.",
        method="update_stock",
        params={"product_id": PRODUCT_ID, "delta": STOCK_DELTA},
        read=_read_stock_update,
        explain=_explain_jsonrpc_required_param,
    ),
    _Scenario(
        id="jsonrpc_renamed_method",
        title="Procédure renommée",
        protocol="custom",
        change_id="jsonrpc_method_renamed",
        expected_outcome="rejected",
        summary="get_product_details s’appelle maintenant get_product : l’ancien nom n’est plus enregistré.",
        method="get_product_details",
        params={"product_id": PRODUCT_ID},
        read=_read_product,
        explain=_explain_jsonrpc_renamed_method,
    ),
    _Scenario(
        id="jsonrpc_renamed_result_key",
        title="Clés du résultat renommées",
        protocol="custom",
        change_id="jsonrpc_result_keys_renamed",
        expected_outcome="crash",
        summary='list_products renvoie price_cents et quantity : le code client qui lit product["stock"] plante.',
        method="list_products",
        params={"limit": LISTED_PRODUCTS},
        read=_read_listing,
        explain=_explain_jsonrpc_renamed_keys,
        focus="response",
    ),
    _Scenario(
        id="jsonrpc_added_key",
        title="Clé ajoutée au résultat",
        protocol="custom",
        change_id="jsonrpc_result_key_added",
        expected_outcome="compatible",
        summary="calculate_factorial renvoie une clé « algorithm » en plus : un lecteur tolérant ne la voit pas.",
        method="calculate_factorial",
        params={"n": FACTORIAL_N},
        read=_read_factorial,
        explain=_explain_jsonrpc_added_key,
        focus="response",
    ),
)

CONTRACT_SCENARIOS: list[dict[str, Any]] = [scenario.metadata() for scenario in _SCENARIOS]


# --- Les octets échangés, relus avec chacun des deux contrats ---------------------------

def _lost_fields(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Champs présents sur le fil que ce contrat ne sait pas lire (une entrée par champ distinct)."""
    lost: dict[str, dict[str, Any]] = {}

    def visit(siblings: list[dict[str, Any]], prefix: str) -> None:
        for node in siblings:
            path = f"{prefix}{node['name'] or '#' + str(node['field'])}"
            if node.get("unknown") or node.get("mismatch"):
                entry = lost.setdefault(path, {
                    "path": path,
                    "number": node["field"],
                    "reason": "wire_type_mismatch" if node.get("mismatch") else "unknown_field",
                    "wire_type": node["wire_type_name"],
                    "expected_wire_type": node.get("expected_wire_type"),
                    "count": 0,
                })
                entry["count"] += 1
            elif node["children"]:
                visit(node["children"], f"{path}.")

    visit(nodes, "")
    return list(lost.values())


def _reading(data: bytes, descriptor: Descriptor, contract: str, label: str) -> dict[str, Any]:
    """Les mêmes octets, interprétés avec UN contrat : champ par champ, puis comme le code généré les voit."""
    fields = decode_wire(data, descriptor)
    # ``message`` vient de la vraie classe générée : c'est exactement ce que le destinataire obtient.
    message = message_factory.GetMessageClass(descriptor).FromString(data)
    return {
        "contract": contract,
        "label": label,
        "message_type": descriptor.full_name,
        "message": message_preview(message, _PREVIEW_ITEMS),
        "fields": fields,
        "segments": segments_for(data, descriptor),
        "lost": _lost_fields(fields),
    }


def _exchange(data: bytes, v1: Descriptor, v2: Descriptor | None, *, client_writes: bool) -> dict[str, Any]:
    """Un message Protobuf et ses deux lectures ; ``as_v2`` vaut ``None`` si la v2 n'a plus cette RPC."""
    labels = (
        ("Ce que le client v1 a écrit", "Ce que le serveur v2 en lit")
        if client_writes else
        ("Ce que le client v1 en lit", "Ce que le serveur v2 a écrit")
    )
    return {
        "message": v1.name,
        "hex": data.hex(),
        "size": len(data),
        "written_by": "v1" if client_writes else "v2",
        "as_v1": _reading(data, v1, "v1", labels[0]),
        "as_v2": _reading(data, v2, "v2", labels[1]) if v2 is not None else None,
    }


def _traced_payload(runtime: LabRuntime, call_id: str, stage: str, header_size: int) -> bytes | None:
    """Octets publiés à l'étape ``stage`` de la trace de l'appel, tramage retiré ; ``None`` si non tracé."""
    trace = runtime.collector.get(call_id) if call_id else None
    for event in tuple(trace.events) if trace is not None else ():
        if event.stage == stage and event.payload is not None and not event.detail.get("payload_truncated"):
            return event.payload[header_size:]
    return None


def _protobuf_wire(runtime: LabRuntime, scenario: _Scenario, call_id: str) -> dict[str, Any]:
    """Requête et réponse gRPC d'un scénario, chacune lue avec le contrat v1 puis avec le contrat v2.

    La requête est re-sérialisée par le code généré du contrat v1 — exactement
    ce que fait le stub — et existe donc toujours ; la réponse vient de la trace
    de l'appel (``None`` si le serveur a rejeté l'appel ou si le bus est coupé).
    """
    codec = conv.CODECS[scenario.method]
    method = _SERVICE_V1.methods_by_name[codec.rpc]
    served = _SERVICE_V2.methods_by_name.get(method.name)
    request = codec.request_to_proto(scenario.params).SerializeToString()
    response = _traced_payload(runtime, call_id, "client.receive", FRAME_HEADER_SIZE)
    return {
        "format": "protobuf",
        "focus": scenario.focus,
        "path": _rpc_path(method),
        "request": _exchange(request, method.input_type, served.input_type if served else None, client_writes=True),
        "response": (
            _exchange(response, method.output_type, served.output_type, client_writes=False)
            if response is not None and served is not None else None
        ),
    }


def _json_wire(runtime: LabRuntime, scenario: _Scenario, call_id: str) -> dict[str, Any] | None:
    """Messages JSON-RPC d'un scénario, tels que la trace de l'appel les a enregistrés."""
    request = _traced_payload(runtime, call_id, "client.marshal", 0)
    if request is None:
        return None
    response = _traced_payload(runtime, call_id, "client.receive", jsonrpc.HEADER_SIZE)
    return {
        "format": "json",
        "focus": scenario.focus,
        "request": _json_message(request),
        "response": _json_message(response) if response is not None else None,
    }


def _json_message(body: bytes) -> dict[str, Any]:
    return {"text": body.decode("utf-8"), "size": len(body), "document": json.loads(body)}


# --- Déroulement --------------------------------------------------------------------------

def _inventory(service: InventoryService) -> Inventory:
    """Stock et prix de chaque produit, lus directement dans le service : la vérité côté serveur."""
    products = service.list_products(len(service.product_ids()))["products"]
    return {product["id"]: {"stock": product["stock"], "price": product["price"]} for product in products}


def _observe(runtime: LabRuntime, scenario: _Scenario, client: Any) -> _Evidence:
    # Le serveur v1 de référence : même inventaire initial, même appel, aucun réseau.
    reference_service = InventoryService()
    reference = getattr(reference_service, scenario.method)(**scenario.params)
    evidence = _Evidence(
        scenario=scenario,
        reference=reference,
        reference_view=scenario.read(reference),
        reference_state=_inventory(reference_service),
        stock_before=_inventory(runtime.service_v2),
    )
    started = time.perf_counter_ns()
    try:
        evidence.result = client.invoke(scenario.method, scenario.params)
    except RpcRemoteError as error:   # une panne de transport, elle, n'est pas une issue de contrat : elle remonte
        evidence.error = error
    evidence.duration_ms = (time.perf_counter_ns() - started) / 1e6
    if evidence.error is None:
        try:
            evidence.view = scenario.read(evidence.result)
        except (LookupError, TypeError) as crash:
            evidence.crash = crash
    evidence.truth = _inventory(runtime.service_v2)
    call_id = client.last_call_id
    evidence.call_id = call_id if call_id and runtime.collector.get(call_id) is not None else ""
    build_wire = _protobuf_wire if scenario.protocol == "grpc" else _json_wire
    evidence.wire = build_wire(runtime, scenario, evidence.call_id)
    return evidence


def _differences(expected: Any, observed: Any, where: str, path: str = "") -> list[dict[str, Any]]:
    """Écarts entre deux structures de même forme, feuille par feuille."""
    if isinstance(expected, dict) and isinstance(observed, dict):
        return [
            difference
            for key, value in expected.items()
            for difference in _differences(value, observed.get(key), where, f"{path}.{key}" if path else str(key))
        ]
    if expected == observed:
        return []
    return [{"where": where, "path": path, "expected": expected, "observed": observed}]


def _summarise(scenario: _Scenario, view: Any, state: Inventory) -> str:
    """Une phrase pour ce que le client a lu (``view``) et l'état du serveur qui en résulte (``state``)."""
    if scenario.method == "calculate_factorial":
        return f"{view['n']}! = {view['result']} ({view['digits']} chiffres)."
    if scenario.method == "update_stock":
        reference = scenario.params["product_id"]
        return (
            f"Réponse : stock {view['previous_stock']} → {view['new_stock']} (delta {view['delta']:+d}). "
            f"Stock de {reference} sur le serveur : {state[reference]['stock']}."
        )
    return " ; ".join(
        f"{reference} : {_euros(entry['price'])}, {entry['stock']} en stock" for reference, entry in view.items()
    ) + "."


def _report(evidence: _Evidence) -> dict[str, Any]:
    scenario, outcome = evidence.scenario, evidence.outcome
    matches = outcome == scenario.expected_outcome
    # Les produits que l'appel désigne ou renvoie : ceux dont la vérité côté serveur mérite d'être montrée.
    if "product_id" in scenario.params:
        watched = [scenario.params["product_id"]]
    else:
        watched = [product["id"] for product in evidence.reference.get("products", ())]
    if evidence.error is not None:
        observed_summary = f"Appel rejeté — {evidence.status} : {evidence.error.message}"
    elif evidence.crash is not None:
        crash = evidence.crash
        observed_summary = (
            f"Appel réussi (statut OK), puis {type(crash).__name__}: {crash} dans le code du client."
        )
    else:
        observed_summary = _summarise(scenario, evidence.view, evidence.truth)
    differences = _differences(evidence.reference_state, evidence.truth, "server")
    if evidence.view is not None:
        differences = _differences(evidence.reference_view, evidence.view, "client") + differences
    arguments = ", ".join(
        f"{name}={json.dumps(value, ensure_ascii=False)}" for name, value in scenario.params.items()
    )
    if matches:
        explanation = scenario.explain(evidence)
    else:
        explanation = (
            f"Résultat inattendu : ce scénario devait se terminer par « "
            f"{OUTCOME_INFO[scenario.expected_outcome]['label']} », il se termine par « "
            f"{OUTCOME_INFO[outcome]['label']} ». Le serveur « contrat v2 » ne se comporte pas comme le décrit "
            "son contrat de démonstration."
        )
    change = next((item for item in contract_changes() if item["id"] == scenario.change_id), None)
    return {
        **scenario.metadata(),
        "change": change,
        "outcome": outcome,
        "outcome_label": OUTCOME_INFO[outcome]["label"],
        "matches": matches,
        "status": evidence.status,
        "sent": {
            "contract": "v1",
            "method": scenario.method,
            "params": dict(scenario.params),
            "call": f"client.{scenario.method}({arguments})",
            "target": evidence.wire["path"] if scenario.protocol == "grpc" else scenario.method,
        },
        "expected": {
            "status": OK,
            "code": OK,
            "result": evidence.reference,
            "client_view": evidence.reference_view,
            "server_state": {reference: evidence.reference_state[reference] for reference in watched},
            "summary": _summarise(scenario, evidence.reference_view, evidence.reference_state),
        },
        "observed": {
            "ok": evidence.error is None,
            "status": evidence.status,
            "code": evidence.error.code if evidence.error is not None else OK,
            "result": evidence.result,
            "client_view": evidence.view,
            "error": evidence.error.to_dict() if evidence.error is not None else None,
            "client_crash": (
                {"type": type(evidence.crash).__name__, "message": str(evidence.crash)}
                if evidence.crash is not None else None
            ),
            "stock_before": {reference: evidence.stock_before[reference] for reference in watched},
            "server_truth": {reference: evidence.truth[reference] for reference in watched},
            "differences": differences,
            "summary": observed_summary,
        },
        "explanation": explanation,
        "wire": evidence.wire,
        "call_id": evidence.call_id,
        "duration_ms": round(evidence.duration_ms, 3),
    }


def run_contract_scenario(runtime: LabRuntime, scenario_id: str) -> dict[str, Any]:
    """Joue un scénario de rupture de contrat sur un laboratoire démarré.

    Un client resté au contrat v1 (``runtime.client_v2``) fait UN appel au
    serveur « contrat v2 ». Le résultat confronte ``sent`` (ce qu'il a envoyé),
    ``expected`` (ce qu'un serveur v1 aurait fait), ``observed`` (ce qui s'est
    passé, avec la vérité lue dans ``runtime.service_v2``) et en déduit
    ``outcome``. ``wire`` montre les messages échangés ; en gRPC, chacun est lu
    avec le contrat v1 puis avec le contrat v2.

    Le scénario part de l'inventaire v2 initial et le rend dans cet état ;
    l'interrupteur ``strict`` du serveur gRPC v2 retrouve sa valeur d'avant.
    Lève ``ValueError`` si le scénario est inconnu ; une panne de transport
    (serveur injoignable) remonte telle quelle.
    """
    scenario = next((candidate for candidate in _SCENARIOS if candidate.id == scenario_id), None)
    if scenario is None:
        known = ", ".join(candidate.id for candidate in _SCENARIOS)
        raise ValueError(f"Scénario de contrat inconnu : {scenario_id!r} (attendu : {known})")
    with _SCENARIO_LOCK:
        client = runtime.client_v2(scenario.protocol)
        previous_strict = runtime.grpc_v2.strict
        try:
            runtime.service_v2.reset()
            runtime.grpc_v2.strict = scenario.strict
            evidence = _observe(runtime, scenario, client)
        finally:
            client.close()
            runtime.grpc_v2.strict = previous_strict
            runtime.service_v2.reset()
    return _report(evidence)


def run_all_contract_scenarios(runtime: LabRuntime) -> list[dict[str, Any]]:
    """Joue tous les scénarios, dans l'ordre de ``CONTRACT_SCENARIOS``."""
    return [run_contract_scenario(runtime, scenario.id) for scenario in _SCENARIOS]


# =============================================================================
# 3. Affichage dans le terminal
# =============================================================================

ACCENT = "#8B7CFF"
SUCCESS = "#4ADE80"
WARNING = "#FBBF24"
ORANGE = "#FB923C"
DANGER = "#F87171"
MUTED = "grey58"
MAX_WIDTH = 118

_OUTCOME_STYLES = {"compatible": SUCCESS, "rejected": WARNING, "crash": ORANGE, "silent_corruption": DANGER}
_PROTOCOL_STYLES = {"grpc": "#2FD9C4", "custom": "#5AA2FF"}
_VALUE_PREVIEW = 44      # caractères d'une valeur décodée
_BYTES_PREVIEW = 8       # octets bruts montrés par champ
_MESSAGE_PREVIEW = 400   # caractères d'un message JSON-RPC


def _section(console: Console, number: int, title: str, subtitle: str) -> None:
    console.print()
    console.print(Rule(Text(f" {number} · {title} ", style=f"bold {ACCENT}"), align="left", style=ACCENT))
    console.print(Text(subtitle, style=MUTED))
    console.print()


def _render_changes(console: Console, changes: list[dict[str, Any]]) -> None:
    table = Table(box=box.SIMPLE_HEAD, header_style=f"bold {ACCENT}", expand=True, pad_edge=False)
    table.add_column("Nature", no_wrap=True)
    table.add_column("Changement", ratio=5)
    table.add_column("Contrat v1", ratio=4, overflow="fold")
    table.add_column("Contrat v2", ratio=4, overflow="fold")
    for change in changes:
        breaking = change["kind"] == BREAKING
        table.add_row(
            Text("RUPTURE" if breaking else "compatible", style=f"bold {DANGER}" if breaking else SUCCESS),
            Text.assemble((change["title"], "bold"), "\n", (change["element"], MUTED)),
            Text(change["v1"] or "—", style=MUTED if change["v1"] is None else ""),
            Text(change["v2"] or "—", style=MUTED if change["v2"] is None else ""),
        )
    console.print(table)


def _render_diff(console: Console, diff: list[dict[str, Any]]) -> None:
    signs = {"same": (" ", MUTED), "removed": ("-", DANGER), "added": ("+", SUCCESS)}
    for row in diff:
        sign, style = signs[row["kind"]]
        left = "" if row["left_no"] is None else str(row["left_no"])
        right = "" if row["right_no"] is None else str(row["right_no"])
        console.print(Text(f"{left:>4} {right:>4} {sign} {row['text']}", style=style), soft_wrap=True)


def _interpretation(node: dict[str, Any]) -> Text:
    """Ce qu'un contrat fait d'un champ reçu : une valeur nommée, ou rien."""
    if node.get("unknown"):
        return Text("numéro inconnu — ignoré", style=WARNING)
    if node.get("mismatch"):
        expected = node.get("expected_wire_type")
        reason = f"attend {expected}, reçoit {node['wire_type_name']}" if expected else "contenu incompatible"
        return Text(f"« {node['name']} » {reason} — ignoré", style=DANGER)
    value = json.dumps(node["decoded"], ensure_ascii=False)
    if len(value) > _VALUE_PREVIEW:
        value = value[:_VALUE_PREVIEW] + "…"
    return Text(f"{node['name']} = {value}")


def _paired_fields(
    left: list[dict[str, Any]], right: list[dict[str, Any]], prefix: str = ""
) -> Iterator[tuple[str, dict[str, Any], dict[str, Any]]]:
    """Apparie les champs des deux lectures (mêmes octets, donc mêmes champs dans le même ordre).

    Rend ``(numéro, champ lu en v1, champ lu en v2)`` ; le numéro d'un champ
    imbriqué est précédé de celui de son parent (« 1.4 »). Un seul élément par
    champ répété, et l'on ne descend dans un sous-message que si les deux
    contrats le lisent différemment : seul ce qui diverge est détaillé.
    """
    seen: set[int] = set()
    for ours, theirs in zip(left, right):
        if ours["field"] in seen:
            continue
        seen.add(ours["field"])
        number = f"{prefix}{ours['field']}"
        if ours["children"] and theirs["children"] and ours["decoded"] != theirs["decoded"]:
            yield from _paired_fields(ours["children"], theirs["children"], f"{number}.")
        else:
            yield number, ours, theirs


def _render_readings(console: Console, exchange: dict[str, Any]) -> None:
    """Les mêmes octets, lus avec le contrat v1 puis avec le contrat v2."""
    v1, v2 = exchange["as_v1"], exchange["as_v2"]
    table = Table(
        box=box.SIMPLE_HEAD, header_style=f"bold {ACCENT}", pad_edge=False, expand=True,
        title=Text(f"{exchange['message']} — {exchange['size']} octets, deux lectures", style=MUTED),
        title_justify="left",
    )
    table.add_column("Octets", no_wrap=True, style=MUTED)
    table.add_column("n°", justify="right", no_wrap=True)
    table.add_column(v1["label"], ratio=1)
    table.add_column(v2["label"], ratio=1)
    data = bytes.fromhex(exchange["hex"])
    for number, ours, theirs in _paired_fields(v1["fields"], v2["fields"]):
        raw = data[ours["tag"]["start"]:ours["value"]["end"]]   # tag, longueur éventuelle, valeur
        shown = raw[:_BYTES_PREVIEW].hex(" ") + (" …" if len(raw) > _BYTES_PREVIEW else "")
        table.add_row(shown, number, _interpretation(ours), _interpretation(theirs))
    console.print(table)


def _render_messages(console: Console, wire: dict[str, Any]) -> None:
    """Les deux messages JSON-RPC de l'appel, tels qu'ils ont circulé."""
    # expand + ratio : le libellé garde sa largeur, c'est le message, trop long pour la ligne, qui est abrégé.
    messages = Table.grid(padding=(0, 2), expand=True)
    messages.add_column(style=MUTED, no_wrap=True)
    messages.add_column(ratio=1, overflow="ellipsis", no_wrap=True)
    for label, message in (("Requête", wire["request"]), ("Réponse", wire["response"])):
        if message is not None:
            messages.add_row(label, Text(message["text"][:_MESSAGE_PREVIEW], style=MUTED))
    console.print(messages)


def _render_scenario(console: Console, result: dict[str, Any]) -> None:
    style = _OUTCOME_STYLES[result["outcome"]]
    heading = Text.assemble(
        (f" {PROTOCOL_LABELS[result['protocol']]} ", f"bold {_PROTOCOL_STYLES[result['protocol']]}"),
        ("· ", MUTED),
        (f"{result['title']} ", "bold"),
    )
    console.print(Rule(heading, align="left", style="grey35"))
    facts = Table.grid(padding=(0, 2))
    facts.add_column(style=MUTED, no_wrap=True)
    facts.add_column()
    facts.add_row("Envoyé", Text(result["sent"]["call"], style="bold"))
    facts.add_row("Serveur v1", Text(result["expected"]["summary"]))
    facts.add_row("Serveur v2", Text(result["observed"]["summary"], style=style))
    facts.add_row("Verdict", Text.assemble(
        (f" {result['outcome_label'].upper()} ", f"bold reverse {style}"),
        ("  conforme au scénario" if result["matches"] else "  INATTENDU", MUTED if result["matches"] else DANGER),
    ))
    console.print(facts)
    console.print()
    console.print(Text(result["explanation"]))
    wire = result["wire"]
    if wire is not None:
        exchange = wire[wire["focus"]]
        if wire["format"] == "json":
            console.print()
            _render_messages(console, wire)
        elif exchange is not None and exchange["as_v2"] is not None:
            console.print()
            _render_readings(console, exchange)
    console.print()


def _render_rules(console: Console, rules: list[dict[str, Any]]) -> None:
    table = Table.grid(padding=(0, 2))
    table.add_column(no_wrap=True)
    table.add_column()
    for rule in rules:
        label, style = ("✓ à faire", SUCCESS) if rule["good"] else ("✗ jamais", DANGER)
        table.add_row(
            Text(label, style=f"bold {style}"),
            Text.assemble((rule["title"], "bold"), "\n", (rule["text"], MUTED), "\n"),
        )
    console.print(table)


def main(argv: list[str] | None = None) -> int:
    """Affiche les changements du contrat puis joue les scénarios ; code 1 si une issue est inattendue."""
    parser = argparse.ArgumentParser(
        prog="python -m benchmark_lab.contract_evolution",
        description="Démonstration des ruptures de contrat : un client v1 face aux serveurs « contrat v2 ».",
    )
    parser.add_argument(
        "--scenario", choices=[scenario.id for scenario in _SCENARIOS], help="ne joue que ce scénario"
    )
    parser.add_argument("--diff", action="store_true", help="affiche aussi le diff ligne à ligne des deux .proto")
    args = parser.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):   # console Windows : accents et filets en UTF-8
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    width = min(shutil.get_terminal_size((MAX_WIDTH, 40)).columns, MAX_WIDTH)
    # markup=False : les textes affichés contiennent des crochets (JSON, déclarations).
    console = Console(width=width, highlight=False, markup=False)

    overview = contract_overview()
    # Bus privé : la démonstration a besoin des traces (octets des réponses) sans toucher au bus global.
    with LabRuntime.ephemeral(bus=EventBus()) as runtime:
        if args.scenario:
            results = [run_contract_scenario(runtime, args.scenario)]
        else:
            results = run_all_contract_scenarios(runtime)

    stats = overview["stats"]
    console.print()
    console.print(Panel(
        Group(
            Text.assemble((APP_NAME, f"bold {ACCENT}"), ("  ·  Évolution de contrat", "bold")),
            Text(
                "Le serveur a été redéployé avec un contrat v2 ; les clients, eux, sont restés au contrat v1.\n"
                f"{stats['breaking']} ruptures et {stats['compatible']} évolutions compatibles séparent les deux "
                "versions.",
                style=MUTED,
            ),
        ),
        border_style=ACCENT, box=box.ROUNDED, padding=(1, 2),
    ))

    _section(
        console, 1, "Ce qui a changé",
        f"{overview['proto_v1']['path']} → {overview['proto_v2']['path']}, puis le serveur JSON-RPC (sans IDL).",
    )
    _render_changes(console, overview["changes"])
    if args.diff:
        _render_diff(console, overview["diff"])

    _section(
        console, 2, "Un client v1 face aux serveurs v2",
        "Pour chaque appel : ce qu’un serveur v1 aurait fait, ce qui s’est réellement passé, et le verdict.",
    )
    for result in results:
        _render_scenario(console, result)

    _section(console, 3, "Règles d’or", "Faire évoluer un contrat sans casser les clients déjà déployés.")
    _render_rules(console, overview["rules"])

    tally = Text("Bilan : ", style="bold")
    for outcome, info in OUTCOME_INFO.items():
        count = sum(result["outcome"] == outcome for result in results)
        if count:
            tally.append(f" {count} × {info['label'].lower()} ", style=f"bold {_OUTCOME_STYLES[outcome]}")
    console.print(tally)
    console.print()
    return 0 if all(result["matches"] for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
