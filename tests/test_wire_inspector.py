"""Décodeur du format de fil Protobuf : encodages connus, contrat absent ou incompatible, octets malformés."""
from __future__ import annotations

import json

import pytest
from google.protobuf import descriptor_pb2, descriptor_pool, message_factory

from common.inventory import InventoryService
from rpc_grpc import converters as conv
from rpc_grpc.generated import service_pb2 as pb
from rpc_grpc.generated import service_v2_pb2 as pb_v2
from rpc_grpc.wire_inspector import (
    FRAME_HEADER_SIZE,
    decode_wire,
    frame_segments,
    grpc_frame,
    segments_for,
)

UPDATE_HEX = "0a08534b552d313030311005"   # UpdateStockRequest(product_id="SKU-1001", delta=-3)


@pytest.fixture(scope="module")
def product() -> dict:
    return InventoryService().get_product_details("SKU-1001")


@pytest.fixture(scope="module")
def probe():
    """Message construit à la volée : champs « packed » et types de largeur fixe absents du contrat métier."""
    field_type = descriptor_pb2.FieldDescriptorProto
    file_proto = descriptor_pb2.FileDescriptorProto(name="wire_probe.proto", package="probe", syntax="proto3")
    message = file_proto.message_type.add(name="Probe")
    fields = (
        ("values", field_type.TYPE_INT32, field_type.LABEL_REPEATED),
        ("weights", field_type.TYPE_DOUBLE, field_type.LABEL_REPEATED),
        ("deltas", field_type.TYPE_SINT32, field_type.LABEL_REPEATED),
        ("ratio", field_type.TYPE_FLOAT, field_type.LABEL_OPTIONAL),
        ("crc", field_type.TYPE_FIXED32, field_type.LABEL_OPTIONAL),
        ("offset", field_type.TYPE_SFIXED64, field_type.LABEL_OPTIONAL),
        ("blob", field_type.TYPE_BYTES, field_type.LABEL_OPTIONAL),
    )
    for number, (name, kind, label) in enumerate(fields, start=1):
        message.field.add(name=name, number=number, type=kind, label=label)
    pool = descriptor_pool.DescriptorPool()
    pool.Add(file_proto)
    return message_factory.GetMessageClass(pool.FindMessageTypeByName("probe.Probe"))


def _assert_contiguous(segments: list[dict], start: int, end: int) -> None:
    assert segments[0]["start"] == start
    assert segments[-1]["end"] == end
    for previous, current in zip(segments, segments[1:]):
        assert current["start"] == previous["end"], f"trou ou chevauchement avant {current['label']}"
    assert all(segment["end"] > segment["start"] for segment in segments)


# --- Encodages connus --------------------------------------------------------

def test_known_encoding_matches_the_real_serializer():
    assert pb.UpdateStockRequest(product_id="SKU-1001", delta=-3).SerializeToString().hex() == UPDATE_HEX


def test_decode_with_descriptor_names_types_and_offsets():
    product_id, delta = decode_wire(bytes.fromhex(UPDATE_HEX), pb.UpdateStockRequest.DESCRIPTOR)

    assert product_id == {
        "field": 1,
        "name": "product_id",
        "wire_type": 2,
        "wire_type_name": "LEN",
        "type": "string",
        "tag": {"start": 0, "end": 1, "hex": "0a"},
        "length": {"start": 1, "end": 2, "value": 8},
        "value": {"start": 2, "end": 10, "hex": "534b552d31303031"},
        "decoded": "SKU-1001",
        "children": None,
    }
    assert delta["name"] == "delta" and delta["type"] == "sint32"
    assert delta["wire_type_name"] == "VARINT" and delta["length"] is None
    assert delta["tag"] == {"start": 10, "end": 11, "hex": "10"}
    assert delta["value"] == {"start": 11, "end": 12, "hex": "05"}
    assert delta["decoded"] == -3   # ZigZag : 5 → -3


def test_decode_without_descriptor_cannot_know_names_nor_zigzag():
    product_id, delta = decode_wire(bytes.fromhex(UPDATE_HEX))

    assert product_id["name"] is None and product_id["type"] is None
    assert product_id["decoded"] == "SKU-1001" and product_id["guess"] == "string"
    assert delta["decoded"] == 5
    assert delta["interpretations"] == {"uint64": 5, "int64": 5, "sint64": -3}


def test_negative_numbers_zigzag_versus_twos_complement():
    summary = pb.BulkUpdateSummary(total_delta=-42).SerializeToString()
    level = pb.StockLevel(stock=-1).SerializeToString()

    (total_delta,) = decode_wire(summary, pb.BulkUpdateSummary.DESCRIPTOR)
    (stock,) = decode_wire(level, pb.StockLevel.DESCRIPTOR)

    assert total_delta["type"] == "sint64" and total_delta["decoded"] == -42
    assert len(bytes.fromhex(total_delta["value"]["hex"])) == 1    # ZigZag : un seul octet
    assert stock["type"] == "int32" and stock["decoded"] == -1
    assert len(bytes.fromhex(stock["value"]["hex"])) == 10          # complément à deux : dix octets


def test_doubles_bools_and_large_integers():
    snapshot = pb.AnalyticsSnapshot(
        seq=7, timestamp_ms=1_760_000_000_123, inventory_value=198_391.9, orders_per_min=-12.25,
        operations=2**63 + 5,
    )
    nodes = {node["name"]: node for node in decode_wire(snapshot.SerializeToString(), pb.AnalyticsSnapshot.DESCRIPTOR)}
    active = decode_wire(pb.Product(active=True).SerializeToString(), pb.Product.DESCRIPTOR)[0]

    assert nodes["inventory_value"]["wire_type_name"] == "I64"
    assert nodes["inventory_value"]["decoded"] == 198_391.9
    assert nodes["orders_per_min"]["decoded"] == -12.25
    assert nodes["timestamp_ms"]["decoded"] == 1_760_000_000_123
    assert nodes["operations"]["decoded"] == 2**63 + 5
    assert active["type"] == "bool" and active["decoded"] is True


def test_nested_product_is_decoded_recursively_with_absolute_offsets(product):
    data = pb.ProductList(products=[conv.product_to_proto(product)], total=1).SerializeToString()

    wrapper, total = decode_wire(data, pb.ProductList.DESCRIPTOR)

    assert wrapper["type"] == "message" and wrapper["message_type"] == "rpcexplorer.v1.Product"
    assert wrapper["decoded"] == product   # le décodeur retrouve exactement le dictionnaire métier
    assert total["decoded"] == 1
    fields = {node["name"]: node for node in wrapper["children"]}
    dimensions = fields["dimensions"]
    assert [child["name"] for child in dimensions["children"]] == ["width_cm", "height_cm", "depth_cm", "weight_kg"]
    for node in (*wrapper["children"], *dimensions["children"], *fields["supplier"]["children"]):
        assert data[node["value"]["start"]:node["value"]["end"]].hex() == node["value"]["hex"]
        assert data[node["tag"]["start"]:node["tag"]["end"]].hex() == node["tag"]["hex"]
    assert dimensions["children"][0]["value"]["start"] > dimensions["value"]["start"] > wrapper["value"]["start"]


def test_repeated_strings_yield_one_node_per_element(product):
    data = conv.product_to_proto(product).SerializeToString()

    tags = [node for node in decode_wire(data, pb.Product.DESCRIPTOR) if node["name"] == "tags"]

    assert [node["decoded"] for node in tags] == product["tags"]
    assert all(node["repeated"] and node["field"] == 7 for node in tags)


def test_packed_repeated_and_fixed_width_types(probe):
    message = probe(
        values=[1, -2, 300], weights=[1.5, -2.25], deltas=[-1, 2],
        ratio=0.5, crc=0xDEADBEEF, offset=-7, blob=b"\x00\xff",
    )

    nodes = {node["name"]: node for node in decode_wire(message.SerializeToString(), probe.DESCRIPTOR)}

    assert nodes["values"]["packed"] and nodes["values"]["decoded"] == [1, -2, 300]
    assert nodes["weights"]["decoded"] == [1.5, -2.25]
    assert nodes["deltas"]["decoded"] == [-1, 2]
    assert nodes["values"]["wire_type_name"] == "LEN" and nodes["values"]["children"] is None
    assert nodes["ratio"]["wire_type_name"] == "I32" and nodes["ratio"]["decoded"] == 0.5
    assert nodes["crc"]["decoded"] == 0xDEADBEEF
    assert nodes["offset"]["decoded"] == -7
    assert nodes["blob"]["decoded"] == "00ff"


# --- Sans contrat : heuristiques ---------------------------------------------

def test_heuristics_tell_text_from_nested_messages_and_bytes(product, probe):
    nodes = decode_wire(conv.product_to_proto(product).SerializeToString())
    by_field: dict[int, list[dict]] = {}
    for node in nodes:
        by_field.setdefault(node["field"], []).append(node)

    assert [node["decoded"] for node in by_field[7]] == product["tags"]   # « usb-c » reste du texte
    assert all(node["guess"] == "string" for node in by_field[7])
    supplier = by_field[9][0]
    assert supplier["guess"] == "message"
    assert supplier["decoded"] == {"#1": "Nordik Components", "#2": "SE", "#3": 6}
    assert by_field[4][0]["decoded"] == product["price"]                    # I64 plausible → double
    assert by_field[4][0]["interpretations"]["double"] == product["price"]

    blob = decode_wire(probe(blob=b"\xff\xfe\x00").SerializeToString())[0]
    assert blob["guess"] == "bytes" and blob["decoded"] == "fffe00"


# --- Contrat incompatible ----------------------------------------------------

def test_wire_type_mismatch_is_flagged_not_fatal(product):
    v2_bytes = conv.product_v2_to_proto(product).SerializeToString()

    nodes = {node["field"]: node for node in decode_wire(v2_bytes, pb.Product.DESCRIPTOR)}

    price = nodes[4]   # v1 attend un double (I64), la v2 envoie des centimes en varint
    assert price["mismatch"] is True and price["name"] == "price" and price["type"] == "double"
    assert price["wire_type_name"] == "VARINT" and price["expected_wire_type"] == "I64"
    assert price["decoded"] == round(product["price"] * 100)
    stock = nodes[5]   # même type de fil : aucune alerte, mais c'est « reserved » qui est lu
    assert "mismatch" not in stock and stock["name"] == "stock"
    assert stock["decoded"] == product["stock"] // 10
    assert nodes[15]["unknown"] is True and nodes[15]["decoded"] == product["stock"]
    assert "mismatch" not in nodes[1]


def test_v1_request_read_with_v2_contract_flags_the_reused_number():
    nodes = decode_wire(bytes.fromhex(UPDATE_HEX), pb_v2.UpdateStockRequest.DESCRIPTOR)

    reused = nodes[1]
    assert reused["name"] == "warehouse" and reused["type"] == "string"
    assert reused["mismatch"] is True and reused["expected_wire_type"] == "LEN"
    assert reused["decoded"] == 5


def test_nested_content_that_is_not_the_declared_message_is_flagged():
    # Champ 8 (« dimensions », sous-message) : bon type de fil (LEN), mais contenu qui n'est pas un message.
    data = b"\x42\x05" + b"\xff" * 5

    (node,) = decode_wire(data, pb.Product.DESCRIPTOR)

    assert node["name"] == "dimensions" and node["mismatch"] is True
    assert node["children"] is None and node["decoded"] == "ffffffffff"


# --- Octets malformés --------------------------------------------------------

@pytest.mark.parametrize(
    "data",
    [
        b"\x08",                       # tag sans valeur
        b"\x08\xff",                   # varint inachevé
        b"\x08" + b"\xff" * 11,        # varint de plus de 10 octets
        b"\x0a\x05ab",                 # longueur annoncée supérieure aux octets restants
        b"\x00\x01",                   # numéro de champ 0
        b"\x0b",                       # type de fil 3 (groupe)
        b"\x0f\x00",                   # type de fil 7 (inexistant)
        b"\x09\x00\x00",               # I64 tronqué
        b"\x0d\x00",                   # I32 tronqué
    ],
)
def test_malformed_input_raises_value_error(data):
    with pytest.raises(ValueError):
        decode_wire(data)
    with pytest.raises(ValueError):
        decode_wire(data, pb.Product.DESCRIPTOR)


def test_malformed_error_message_is_explicit():
    with pytest.raises(ValueError, match="5 octets annoncés, 2 disponibles"):
        decode_wire(b"\x0a\x05ab")


def test_empty_message_has_no_field():
    assert decode_wire(b"") == []
    assert segments_for(b"", pb.Product.DESCRIPTOR) == []


# --- Segments ----------------------------------------------------------------

def test_segments_form_tag_len_value_triplets():
    segments = segments_for(bytes.fromhex(UPDATE_HEX), pb.UpdateStockRequest.DESCRIPTOR)

    assert [(s["kind"], s["start"], s["end"]) for s in segments] == [
        ("tag", 0, 1), ("len", 1, 2), ("value", 2, 10), ("tag", 10, 11), ("value", 11, 12),
    ]
    assert [s["value"] for s in segments] == [0x0A, 8, "SKU-1001", 0x10, -3]
    assert {s["field"] for s in segments[:3]} == {"product_id"}
    value = segments[-1]
    assert value["field"] == "delta" and value["number"] == 2
    assert value["wire_type"] == 0 and value["wire_type_name"] == "VARINT" and value["type"] == "sint32"
    assert all(s["depth"] == 0 and s["label"] for s in segments)


def test_segments_cover_a_nested_message_without_gaps(product):
    data = pb.ProductList(products=[conv.product_to_proto(product)] * 2, total=24).SerializeToString()

    segments = segments_for(data, pb.ProductList.DESCRIPTOR, base_offset=5)

    _assert_contiguous(segments, 5, 5 + len(data))
    assert {segment["depth"] for segment in segments} == {0, 1, 2}
    width = next(segment for segment in segments if segment["field"] == "width_cm" and segment["kind"] == "value")
    assert width["depth"] == 2 and width["value"] == product["dimensions"]["width_cm"]
    assert data[width["start"] - 5:width["end"] - 5] == b"\x00\x00\x00\x00\x00\x00\x46\x40"   # 44.0 en IEEE 754
    json.dumps(segments)


def test_segments_limit_summarises_the_rest(product):
    data = pb.ProductList(products=[conv.product_to_proto(product)] * 40, total=40).SerializeToString()

    segments = segments_for(data, pb.ProductList.DESCRIPTOR, limit=50)

    assert len(segments) == 51
    assert segments[-1]["kind"] == "body" and segments[-1]["end"] == len(data)
    _assert_contiguous(segments, 0, len(data))


def test_segments_strict_flag_on_malformed_bytes():
    data = bytes.fromhex(UPDATE_HEX) + b"\x1a\x7fab"   # champ 3 : 127 octets annoncés, 2 présents

    with pytest.raises(ValueError):
        segments_for(data, pb.UpdateStockRequest.DESCRIPTOR)
    segments = segments_for(data, pb.UpdateStockRequest.DESCRIPTOR, strict=False)

    assert [segment["kind"] for segment in segments] == ["tag", "len", "value", "tag", "value", "body"]
    assert segments[-1]["start"] == 12 and segments[-1]["end"] == len(data)
    assert segments_for(b"\xff", strict=False) == [
        {"label": "Octets non décodables", "start": 0, "end": 1, "kind": "body"},
    ]


def test_mismatch_is_visible_in_segments(product):
    segments = segments_for(conv.product_v2_to_proto(product).SerializeToString(), pb.Product.DESCRIPTOR)

    flagged = [segment for segment in segments if segment.get("mismatch")]
    assert {segment["field"] for segment in flagged} == {"price"}
    assert "le contrat attend I64" in flagged[0]["label"]
    assert any(segment["field"] == "#15" for segment in segments)


# --- Tramage gRPC ------------------------------------------------------------

def test_grpc_frame_prefixes_flag_and_big_endian_length():
    message = bytes.fromhex(UPDATE_HEX)

    framed = grpc_frame(message)

    assert framed[:FRAME_HEADER_SIZE] == b"\x00\x00\x00\x00\x0c"
    assert framed[FRAME_HEADER_SIZE:] == message
    assert grpc_frame(b"x" * 70_000)[:5] == b"\x00\x00\x01\x11\x70"
    assert grpc_frame(b"") == b"\x00\x00\x00\x00\x00"


def test_frame_segments_cover_prefix_and_message():
    message = bytes.fromhex(UPDATE_HEX)

    segments = frame_segments(message, pb.UpdateStockRequest.DESCRIPTOR)

    assert segments[0] == {"label": "Drapeau de compression", "start": 0, "end": 1, "kind": "frame", "value": 0}
    assert segments[1] == {"label": "Longueur du message", "start": 1, "end": 5, "kind": "frame", "value": 12}
    assert segments[2]["start"] == 5 and segments[2]["kind"] == "tag"
    _assert_contiguous(segments, 0, len(grpc_frame(message)))
    assert [segment["kind"] for segment in frame_segments(b"")] == ["frame", "frame"]


def test_decoded_tree_is_json_serialisable(product, probe):
    payloads = (
        (conv.product_to_proto(product).SerializeToString(), pb.Product.DESCRIPTOR),
        (conv.product_to_proto(product).SerializeToString(), None),
        (probe(offset=-7, weights=[float("inf")], blob=b"\x01").SerializeToString(), None),
        (probe(weights=[float("nan")]).SerializeToString(), probe.DESCRIPTOR),
    )
    for data, descriptor in payloads:
        json.dumps(decode_wire(data, descriptor), allow_nan=False)
        json.dumps(segments_for(data, descriptor), allow_nan=False)
