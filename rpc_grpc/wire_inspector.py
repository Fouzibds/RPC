"""Décodeur du format de fil Protobuf (« wire format »), octet par octet.

Sur le fil, un message Protobuf n'est qu'une suite de champs ``tag + valeur`` :

* le **tag** est un varint valant ``(numéro_de_champ << 3) | type_de_fil`` ;
* le **type de fil** dit seulement comment trouver la fin de la valeur :
  ``VARINT`` (0), ``I64`` (1, 8 octets), ``LEN`` (2, longueur puis octets),
  ``I32`` (5, 4 octets) ;
* ni les noms de champs ni les types déclarés ne voyagent : il faut le contrat
  (le *descriptor* issu du ``.proto``) pour savoir qu'un varint est un ``sint32``
  encodé en ZigZag ou que des octets ``LEN`` sont un sous-message.

``decode_wire`` rend cette structure visible, avec ou sans contrat. Sans
contrat, les valeurs sont devinées ; avec un contrat qui ne correspond pas aux
octets (cas d'une évolution d'IDL mal menée), le champ est signalé par
``"mismatch": True`` au lieu de faire échouer le décodage — exactement ce que
fait un vrai décodeur Protobuf, qui range alors le champ parmi les inconnus.
"""
from __future__ import annotations

import math
import struct
from typing import Any, Callable, Iterator, NamedTuple

from google.protobuf.descriptor import Descriptor, FieldDescriptor

VARINT, I64, LEN, I32 = 0, 1, 2, 5
WIRE_TYPE_NAMES: dict[int, str] = {VARINT: "VARINT", I64: "I64", LEN: "LEN", I32: "I32"}

FRAME_HEADER_SIZE = 5   # préfixe gRPC : 1 octet de compression + 4 octets de longueur
MAX_SEGMENTS = 480      # au-delà, la fin du message est résumée en un seul segment

_MAX_DEPTH = 64
_MAX_FIELD_NUMBER = (1 << 29) - 1
_MASK64 = (1 << 64) - 1
_FIXED_SIZES: dict[int, int] = {I64: 8, I32: 4}

_TYPE_NAMES: dict[int, str] = {
    FieldDescriptor.TYPE_DOUBLE: "double",
    FieldDescriptor.TYPE_FLOAT: "float",
    FieldDescriptor.TYPE_INT64: "int64",
    FieldDescriptor.TYPE_UINT64: "uint64",
    FieldDescriptor.TYPE_INT32: "int32",
    FieldDescriptor.TYPE_FIXED64: "fixed64",
    FieldDescriptor.TYPE_FIXED32: "fixed32",
    FieldDescriptor.TYPE_BOOL: "bool",
    FieldDescriptor.TYPE_STRING: "string",
    FieldDescriptor.TYPE_GROUP: "group",
    FieldDescriptor.TYPE_MESSAGE: "message",
    FieldDescriptor.TYPE_BYTES: "bytes",
    FieldDescriptor.TYPE_UINT32: "uint32",
    FieldDescriptor.TYPE_ENUM: "enum",
    FieldDescriptor.TYPE_SFIXED32: "sfixed32",
    FieldDescriptor.TYPE_SFIXED64: "sfixed64",
    FieldDescriptor.TYPE_SINT32: "sint32",
    FieldDescriptor.TYPE_SINT64: "sint64",
}


def _signed64(value: int) -> int:
    """Complément à deux sur 64 bits : c'est ainsi que int32/int64 encodent les négatifs (10 octets)."""
    return value - (1 << 64) if value >> 63 else value


def _signed32(value: int) -> int:
    """Un int32 voyage étendu à 64 bits ; seuls ses 32 bits de poids faible comptent."""
    value &= 0xFFFFFFFF
    return value - (1 << 32) if value >> 31 else value


def _zigzag(value: int) -> int:
    """Décodage ZigZag (sint32/sint64) : 0, -1, 1, -2… → les petits négatifs restent courts."""
    return (value >> 1) ^ -(value & 1)


_VARINT_DECODERS: dict[str, Callable[[int], Any]] = {
    "int32": _signed32,
    "int64": _signed64,
    "enum": _signed32,
    "uint32": lambda value: value & 0xFFFFFFFF,
    "uint64": lambda value: value,
    "sint32": lambda value: _zigzag(value & 0xFFFFFFFF),
    "sint64": _zigzag,
    "bool": bool,
}
_FIXED_FORMATS: dict[str, tuple[int, str]] = {
    "double": (I64, "<d"),
    "fixed64": (I64, "<Q"),
    "sfixed64": (I64, "<q"),
    "float": (I32, "<f"),
    "fixed32": (I32, "<I"),
    "sfixed32": (I32, "<i"),
}
# Interprétations possibles d'une valeur de largeur fixe quand le contrat est inconnu.
_FIXED_GUESSES: dict[int, tuple[tuple[str, str], ...]] = {
    I64: (("fixed64", "<Q"), ("sfixed64", "<q"), ("double", "<d")),
    I32: (("fixed32", "<I"), ("sfixed32", "<i"), ("float", "<f")),
}


class _RawField(NamedTuple):
    """Un champ tel qu'il apparaît sur le fil, avant toute interprétation."""

    number: int
    wire_type: int
    tag_start: int
    tag_end: int       # fin du tag = début de la longueur (LEN) ou de la valeur
    value_start: int
    value_end: int
    varint: int        # valeur brute (VARINT) ou longueur annoncée (LEN), 0 sinon


# --- Lecture brute -----------------------------------------------------------

def _read_varint(data: bytes, pos: int, end: int) -> tuple[int, int]:
    """Lit un varint (7 bits utiles par octet, bit de poids fort = « il y a une suite »)."""
    start, result, shift = pos, 0, 0
    while True:
        if pos >= end:
            raise ValueError(f"Varint tronqué à l'offset {start}")
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result & _MASK64, pos
        shift += 7
        if shift >= 70:
            raise ValueError(f"Varint de plus de 10 octets à l'offset {start}")


def _scan(data: bytes, start: int, end: int) -> Iterator[_RawField]:
    """Découpe ``data[start:end]`` en champs ; lève ``ValueError`` si les octets sont malformés."""
    pos = start
    while pos < end:
        tag, tag_end = _read_varint(data, pos, end)
        number, wire_type = tag >> 3, tag & 0x07
        if not 1 <= number <= _MAX_FIELD_NUMBER:
            raise ValueError(f"Numéro de champ invalide ({number}) à l'offset {pos}")
        if wire_type == VARINT:
            value, value_end = _read_varint(data, tag_end, end)
            field = _RawField(number, wire_type, pos, tag_end, tag_end, value_end, value)
        elif wire_type == LEN:
            length, value_start = _read_varint(data, tag_end, end)
            if length > end - value_start:
                raise ValueError(
                    f"Champ {number} à l'offset {pos} : {length} octets annoncés, "
                    f"{end - value_start} disponibles"
                )
            field = _RawField(number, wire_type, pos, tag_end, value_start, value_start + length, length)
        elif wire_type in _FIXED_SIZES:
            value_end = tag_end + _FIXED_SIZES[wire_type]
            if value_end > end:
                raise ValueError(
                    f"Champ {number} à l'offset {pos} : valeur {WIRE_TYPE_NAMES[wire_type]} tronquée"
                )
            field = _RawField(number, wire_type, pos, tag_end, tag_end, value_end, 0)
        else:
            raise ValueError(
                f"Type de fil {wire_type} non pris en charge à l'offset {pos} "
                "(groupes obsolètes ou octets corrompus)"
            )
        yield field
        pos = field.value_end


# --- Interprétation ----------------------------------------------------------

def _json_safe(value: float) -> float | str:
    """NaN et infinis n'existent pas en JSON : on les rend sous forme de texte."""
    return value if math.isfinite(value) else repr(value)


def _span(data: bytes, start: int, end: int) -> dict[str, Any]:
    return {"start": start, "end": end, "hex": data[start:end].hex()}


def _unpack(data: bytes, offset: int, fmt: str) -> Any:
    """Valeur de largeur fixe (petit-boutiste) lue à ``offset``."""
    value = struct.unpack_from(fmt, data, offset)[0]
    return _json_safe(value) if isinstance(value, float) else value


def _expected_wire_type(declared: str) -> int | None:
    if declared in _VARINT_DECODERS:
        return VARINT
    if declared in _FIXED_FORMATS:
        return _FIXED_FORMATS[declared][0]
    return LEN if declared in ("string", "bytes", "message") else None


def _decode_packed(data: bytes, raw: _RawField, declared: str) -> list[Any]:
    """Champ répété « packed » : les éléments se suivent sans tag dans un seul bloc LEN."""
    pos = raw.value_start
    if declared in _VARINT_DECODERS:
        decoder = _VARINT_DECODERS[declared]
        values: list[Any] = []
        while pos < raw.value_end:
            value, pos = _read_varint(data, pos, raw.value_end)
            values.append(decoder(value))
        return values
    wire_type, fmt = _FIXED_FORMATS[declared]
    size = _FIXED_SIZES[wire_type]
    if (raw.value_end - pos) % size:
        raise ValueError(f"Champ {raw.number} : bloc « packed » de taille incohérente")
    return [_unpack(data, offset, fmt) for offset in range(pos, raw.value_end, size)]


def _guess(node: dict[str, Any], data: bytes, raw: _RawField, depth: int) -> None:
    """Interprète une valeur sans l'aide du contrat (champ inconnu ou incompatible)."""
    if raw.wire_type == VARINT:
        # Impossible de savoir, sans le contrat, laquelle de ces lectures est la bonne.
        node["decoded"] = raw.varint
        node["interpretations"] = {
            "uint64": raw.varint,
            "int64": _signed64(raw.varint),
            "sint64": _zigzag(raw.varint),
        }
    elif raw.wire_type in _FIXED_GUESSES:
        readings = {name: _unpack(data, raw.value_start, fmt) for name, fmt in _FIXED_GUESSES[raw.wire_type]}
        unsigned, _, real = readings.values()
        plausible = isinstance(real, float) and (real == 0 or 1e-6 <= abs(real) <= 1e15)
        node["decoded"] = real if plausible else unsigned
        node["interpretations"] = readings
    else:
        _guess_len(node, data, raw, depth)


def _guess_len(node: dict[str, Any], data: bytes, raw: _RawField, depth: int) -> None:
    """Heuristique pour un bloc LEN : texte lisible, sinon sous-message, sinon octets.

    Un texte entièrement imprimable est testé en premier : de courtes chaînes
    comme « usb-c » forment par hasard un message Protobuf valide, alors qu'un
    vrai sous-message contient presque toujours des octets de contrôle.
    """
    payload = data[raw.value_start:raw.value_end]
    try:
        text: str | None = payload.decode("utf-8")
    except UnicodeDecodeError:
        text = None
    if text is not None and text.isprintable():
        node["decoded"], node["guess"] = text, "string"
        return
    try:
        children = _decode_range(data, raw.value_start, raw.value_end, None, depth + 1)
    except ValueError:
        children = None
    if children is not None:
        node["children"], node["decoded"], node["guess"] = children, _assemble(children), "message"
    elif text is not None and all(char.isprintable() or char in "\n\r\t" for char in text):
        node["decoded"], node["guess"] = text, "string"   # texte sur plusieurs lignes
    else:
        node["decoded"], node["guess"] = payload.hex(), "bytes"


def _decode_declared(
    node: dict[str, Any], data: bytes, raw: _RawField, field: FieldDescriptor, declared: str, depth: int
) -> None:
    """Interprète une valeur dont le type de fil correspond au type déclaré."""
    if raw.wire_type == VARINT:
        node["decoded"] = _VARINT_DECODERS[declared](raw.varint)
    elif raw.wire_type in _FIXED_SIZES:
        node["decoded"] = _unpack(data, raw.value_start, _FIXED_FORMATS[declared][1])
    elif declared == "string":
        node["decoded"] = data[raw.value_start:raw.value_end].decode("utf-8", errors="replace")
    elif declared == "bytes":
        node["decoded"] = data[raw.value_start:raw.value_end].hex()
    else:
        # Sous-message ou bloc « packed » : le type de fil (LEN) est le bon, mais le
        # contenu peut ne pas être celui que le contrat annonce.
        try:
            if declared == "message":
                node["message_type"] = field.message_type.full_name
                children = _decode_range(data, raw.value_start, raw.value_end, field.message_type, depth + 1)
                node["children"], node["decoded"] = children, _assemble(children)
            else:
                node["decoded"] = _decode_packed(data, raw, declared)
                node["packed"] = True
        except ValueError:
            node["mismatch"] = True
            _guess_len(node, data, raw, depth)


def _build_node(data: bytes, raw: _RawField, descriptor: Descriptor | None, depth: int) -> dict[str, Any]:
    field = descriptor.fields_by_number.get(raw.number) if descriptor is not None else None
    declared = _TYPE_NAMES.get(field.type) if field is not None else None
    node: dict[str, Any] = {
        "field": raw.number,
        "name": field.name if field is not None else None,
        "wire_type": raw.wire_type,
        "wire_type_name": WIRE_TYPE_NAMES[raw.wire_type],
        "type": declared,
        "tag": _span(data, raw.tag_start, raw.tag_end),
        "length": (
            {"start": raw.tag_end, "end": raw.value_start, "value": raw.varint}
            if raw.wire_type == LEN else None
        ),
        "value": _span(data, raw.value_start, raw.value_end),
        "decoded": None,
        "children": None,
    }
    if field is None:
        if descriptor is not None:
            node["unknown"] = True   # présent sur le fil, absent du contrat : ignoré par le destinataire
        _guess(node, data, raw, depth)
        return node
    if field.is_repeated:
        node["repeated"] = True
    expected = _expected_wire_type(declared)
    packed = field.is_repeated and raw.wire_type == LEN and expected in (VARINT, I64, I32)
    if raw.wire_type == expected or packed:
        _decode_declared(node, data, raw, field, declared, depth)
    else:
        node["mismatch"] = True
        node["expected_wire_type"] = WIRE_TYPE_NAMES.get(expected, "?")
        _guess(node, data, raw, depth)
    return node


def _decode_range(
    data: bytes, start: int, end: int, descriptor: Descriptor | None, depth: int
) -> list[dict[str, Any]]:
    if depth > _MAX_DEPTH:
        raise ValueError("Imbrication de messages trop profonde")
    return [_build_node(data, raw, descriptor, depth) for raw in _scan(data, start, end)]


def _assemble(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    """Valeur interprétée d'un sous-message : ses champs regroupés par nom.

    Un champ inconnu ou incompatible avec le contrat est rangé sous ``#numéro`` :
    le destinataire ne le lira jamais sous son nom déclaré.
    """
    result: dict[str, Any] = {}
    multiple: set[str] = set()
    for node in nodes:
        named = node["name"] is not None and not node.get("mismatch")
        key = node["name"] if named else f"#{node['field']}"
        value = node["decoded"]
        if named and node.get("packed"):
            result.setdefault(key, []).extend(value)
        elif named and node.get("repeated"):
            result.setdefault(key, []).append(value)
        elif key in multiple:
            result[key].append(value)
        elif key in result and not named:
            # Champ sans nom vu plusieurs fois : faute de contrat, on suppose un champ répété.
            result[key] = [result[key], value]
            multiple.add(key)
        else:
            result[key] = value
    return result


# --- API publique ------------------------------------------------------------

def decode_wire(data: bytes, descriptor: Descriptor | None = None) -> list[dict[str, Any]]:
    """Décode un message Protobuf sérialisé en une liste de champs annotés.

    Chaque nœud vaut ``{"field", "name", "wire_type", "wire_type_name", "type",
    "tag": {start, end, hex}, "length": {start, end, value} | None,
    "value": {start, end, hex}, "decoded", "children"}``. Les offsets sont
    absolus (relatifs au début de ``data``), y compris dans les sous-messages.

    Clés facultatives : ``repeated``, ``packed``, ``message_type``, ``unknown``
    (champ absent du contrat), ``mismatch`` + ``expected_wire_type`` (le type de
    fil reçu contredit le contrat), ``guess`` et ``interpretations`` (valeur
    devinée faute de contrat : texte lisible, sinon sous-message, sinon octets).

    Lève ``ValueError`` si les octets ne forment pas un message Protobuf.
    """
    data = bytes(data)
    return _decode_range(data, 0, len(data), descriptor, 0)


def _flatten(node: dict[str, Any], base: int, depth: int, segments: list[dict[str, Any]]) -> None:
    if node["name"]:
        title = f"champ {node['field']} « {node['name']} »"
    elif node.get("unknown"):
        title = f"champ {node['field']} (inconnu du contrat)"
    else:
        title = f"champ {node['field']}"
    shared: dict[str, Any] = {
        "field": node["name"] or f"#{node['field']}",
        "number": node["field"],
        "wire_type": node["wire_type"],
        "wire_type_name": node["wire_type_name"],
        "depth": depth,
    }
    tag_label = f"Tag — {title}, type de fil {node['wire_type']} ({node['wire_type_name']})"
    value_label = f"Valeur du {title}"
    if node["type"]:
        shared["type"] = node["type"]
    if node.get("mismatch"):
        shared["mismatch"] = True
        expected = node.get("expected_wire_type")
        tag_label += f" — le contrat attend {expected}" if expected else " — contenu incompatible avec le contrat"
        value_label += f" — ne correspond pas au type déclaré ({node['type']}) : ignorée par le destinataire"
    elif node["type"]:
        value_label += f" ({node['type']})"

    tag, length, value = node["tag"], node["length"], node["value"]
    segments.append({
        "label": tag_label,
        "start": base + tag["start"],
        "end": base + tag["end"],
        "kind": "tag",
        "value": (node["field"] << 3) | node["wire_type"],
        **shared,
    })
    if length is not None:
        size = length["value"]
        segments.append({
            "label": f"Longueur du {title} : {size} octet{'s' if size > 1 else ''}",
            "start": base + length["start"],
            "end": base + length["end"],
            "kind": "len",
            "value": size,
            **shared,
        })
    if node["children"] is not None:
        # Sous-message : ce sont ses propres champs qui couvrent les octets de la valeur.
        for child in node["children"]:
            _flatten(child, base, depth + 1, segments)
    elif value["end"] > value["start"]:
        segments.append({
            "label": value_label,
            "start": base + value["start"],
            "end": base + value["end"],
            "kind": "value",
            "value": node["decoded"],
            **shared,
        })


def segments_for(
    data: bytes,
    descriptor: Descriptor | None = None,
    base_offset: int = 0,
    *,
    limit: int = MAX_SEGMENTS,
    strict: bool = True,
) -> list[dict[str, Any]]:
    """Découpe un message en segments contigus ``tag`` / ``len`` / ``value`` (plan §5).

    Les segments couvrent tous les octets, sans trou ni chevauchement, décalés
    de ``base_offset`` (5 derrière un préfixe gRPC). ``depth`` indique le niveau
    d'imbrication. Au-delà de ``limit`` segments, le reste du message est résumé
    par un unique segment ``body`` : une liste de 1000 produits reste affichable.

    Avec ``strict=False``, des octets malformés ne lèvent pas d'erreur : la partie
    indéchiffrable devient elle aussi un segment ``body``.
    """
    data = bytes(data)
    segments: list[dict[str, Any]] = []
    tail_label = "Suite du message (non détaillée)"
    try:
        for raw in _scan(data, 0, len(data)):
            _flatten(_build_node(data, raw, descriptor, 0), base_offset, 0, segments)
            if len(segments) >= limit:
                del segments[limit:]
                break
    except ValueError:
        if strict:
            raise
        tail_label = "Octets non décodables"
    # Les segments se suivent sans trou : ce qui n'est pas détaillé commence là où ils s'arrêtent.
    covered = segments[-1]["end"] if segments else base_offset
    if covered < base_offset + len(data):
        segments.append({"label": tail_label, "start": covered, "end": base_offset + len(data), "kind": "body"})
    return segments


def grpc_frame(message: bytes) -> bytes:
    """Encadre un message comme le fait gRPC sur HTTP/2 (« Length-Prefixed-Message »).

    Un octet de drapeau (0 = non compressé) puis la longueur sur 4 octets
    big-endian : c'est ce préfixe qui permet d'enchaîner plusieurs messages sur
    le même flux HTTP/2 (streaming).
    """
    return b"\x00" + len(message).to_bytes(4, "big") + message


def frame_segments(
    message: bytes,
    descriptor: Descriptor | None = None,
    *,
    limit: int = MAX_SEGMENTS,
    strict: bool = True,
) -> list[dict[str, Any]]:
    """Segments de ``grpc_frame(message)`` : les 5 octets de préfixe puis les champs Protobuf."""
    return [
        {"label": "Drapeau de compression", "start": 0, "end": 1, "kind": "frame", "value": 0},
        {"label": "Longueur du message", "start": 1, "end": FRAME_HEADER_SIZE, "kind": "frame", "value": len(message)},
        *segments_for(message, descriptor, FRAME_HEADER_SIZE, limit=limit, strict=strict),
    ]
