"""Évolution de contrat et transparence de localisation (``benchmark_lab``).

Le laboratoire de ce fichier écoute sur des ports éphémères et publie sur un bus
privé : les tests ne dépendent ni des ports par défaut ni du bus global.
"""
from __future__ import annotations

import inspect
import json
import textwrap
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Iterator

import grpc
import pytest
from google.protobuf import descriptor_pb2, descriptor_pool
from google.protobuf.descriptor import FieldDescriptor, FileDescriptor

from benchmark_lab import contract_evolution as contract
from benchmark_lab import transparency_demo as transparency
from common.errors import FAILED_PRECONDITION, METHOD_NOT_FOUND, FailedPreconditionError, NotFoundError
from common.inventory import InventoryService
from common.telemetry import EventBus
from lab import LabRuntime
from rpc_custom import RpcClientStub
from rpc_grpc import converters as conv
from rpc_grpc.generated import service_pb2 as pb_v1
from rpc_grpc.generated import service_v2_pb2 as pb_v2
from rpc_grpc.generated.service_pb2_grpc import InventoryServiceStub
from rpc_grpc.wire_inspector import FRAME_HEADER_SIZE

PRODUCT = "SKU-1001"
INITIAL_STOCK = 84
OTHER_PRODUCT = "SKU-1005"   # produit réservé aux tests qui modifient le stock du service principal
EXPECTED_OUTCOMES = {
    "renamed_rpc": "rejected",
    "field_reuse_silent": "silent_corruption",
    "field_reuse_strict": "rejected",
    "added_field": "compatible",
    "type_change_read": "silent_corruption",
    "jsonrpc_new_required_param": "rejected",
    "jsonrpc_renamed_method": "rejected",
    "jsonrpc_renamed_result_key": "crash",
    "jsonrpc_added_key": "compatible",
}
GRPC_SCENARIOS = [name for name in EXPECTED_OUTCOMES if not name.startswith("jsonrpc_")]
FIELD_TYPE = descriptor_pb2.FieldDescriptorProto


# --- Outils ------------------------------------------------------------------

@pytest.fixture(scope="module")
def lab() -> Iterator[LabRuntime]:
    with LabRuntime.ephemeral(bus=EventBus()) as runtime:
        yield runtime


@pytest.fixture(scope="module")
def overview() -> dict[str, Any]:
    return contract.contract_overview()


@pytest.fixture(scope="module")
def results(lab: LabRuntime) -> dict[str, dict[str, Any]]:
    return {result["id"]: result for result in contract.run_all_contract_scenarios(lab)}


def changes_by_id(overview: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {change["id"]: change for change in overview["changes"]}


def declaration(field: FieldDescriptor) -> str:
    """Déclaration d'un champ, reconstruite sans passer par le module testé."""
    referenced = field.message_type or field.enum_type
    type_name = referenced.name if referenced else FIELD_TYPE.Type.Name(field.type)[len("TYPE_"):].lower()
    return f"{'repeated ' if field.is_repeated else ''}{type_name} {field.name} = {field.number};"


def build_contract(
    name: str, messages: dict[str, list[tuple[str, int, str]]], rpcs: list[tuple[str, str, str]]
) -> FileDescriptor:
    """Contrat Protobuf fabriqué en mémoire : messages ``{nom: [(champ, numéro, type)]}`` et RPC unaires."""
    package = "demo.v1"
    proto = descriptor_pb2.FileDescriptorProto(name=name, package=package, syntax="proto3")
    for message_name, fields in messages.items():
        message = proto.message_type.add(name=message_name)
        for field_name, number, type_name in fields:
            message.field.add(
                name=field_name, number=number, label=FIELD_TYPE.LABEL_OPTIONAL,
                type=FIELD_TYPE.Type.Value(f"TYPE_{type_name.upper()}"),
            )
    service = proto.service.add(name="Demo")
    for rpc, request, reply in rpcs:
        service.method.add(name=rpc, input_type=f".{package}.{request}", output_type=f".{package}.{reply}")
    pool = descriptor_pool.DescriptorPool()   # un pool par contrat : les deux versions portent les mêmes noms
    pool.AddSerializedFile(proto.SerializeToString())
    return pool.FindFileByName(name)


def signature(description: dict[str, Any]) -> str:
    """Signature d'une procédure telle que ``rpc.discover`` la décrit."""
    params = [
        param["name"] if param["required"] else f"{param['name']}={json.dumps(param['default'])}"
        for param in description["params"]
    ]
    return f"{description['name']}({', '.join(params)})"


def stock_of(service: InventoryService, product_id: str = PRODUCT) -> int:
    return service.get_product_details(product_id)["stock"]


# --- Vue d'ensemble du contrat -------------------------------------------------

def test_overview_has_the_documented_shape(overview: dict[str, Any]) -> None:
    assert {"proto_v1", "proto_v2", "diff", "changes", "rules", "scenarios", "outcomes", "stats"} <= set(overview)
    assert overview["proto_v1"]["path"] == "rpc_grpc/protos/service.proto"
    assert overview["proto_v2"]["path"] == "rpc_grpc/protos/service_v2.proto"
    assert "package rpcexplorer.v1;" in overview["proto_v1"]["source"]
    assert "package rpcexplorer.v2;" in overview["proto_v2"]["source"]
    assert set(overview["outcomes"]) == set(contract.OUTCOMES) == set(EXPECTED_OUTCOMES.values())
    assert overview["scenarios"] == contract.CONTRACT_SCENARIOS
    stats = overview["stats"]
    assert stats["breaking"] + stats["compatible"] == len(overview["changes"])
    json.dumps(overview)   # servi tel quel par le dashboard


def test_diff_reconstructs_both_files(overview: dict[str, Any]) -> None:
    diff = overview["diff"]
    left = [row["text"] for row in diff if row["kind"] in ("same", "removed")]
    right = [row["text"] for row in diff if row["kind"] in ("same", "added")]
    assert left == overview["proto_v1"]["source"].splitlines()
    assert right == overview["proto_v2"]["source"].splitlines()


def test_diff_numbers_every_line_once_and_in_order(overview: dict[str, Any]) -> None:
    diff = overview["diff"]
    assert {row["kind"] for row in diff} == {"same", "removed", "added"}
    left_numbers = [row["left_no"] for row in diff if row["left_no"] is not None]
    right_numbers = [row["right_no"] for row in diff if row["right_no"] is not None]
    assert left_numbers == list(range(1, len(left_numbers) + 1))
    assert right_numbers == list(range(1, len(right_numbers) + 1))
    for row in diff:
        assert (row["left_no"] is None) == (row["kind"] == "added")
        assert (row["right_no"] is None) == (row["kind"] == "removed")
    assert overview["stats"]["lines_removed"] == sum(row["kind"] == "removed" for row in diff)
    assert overview["stats"]["lines_added"] == sum(row["kind"] == "added" for row in diff)


def test_every_change_is_fully_described(overview: dict[str, Any]) -> None:
    changes = overview["changes"]
    assert len({change["id"] for change in changes}) == len(changes)
    for change in changes:
        assert change["kind"] in ("breaking", "compatible")
        assert change["severity"] in ("critical", "major", "minor", "info")
        assert change["contract"] in ("protobuf", "jsonrpc")
        assert change["v1"] or change["v2"]
        for key in ("title", "element", "wire_effect", "explanation"):
            assert change[key].strip(), (change["id"], key)
        assert set(change["lines"]) == {"v1", "v2"}
    assert {"breaking", "compatible"} == {change["kind"] for change in changes}


@pytest.mark.parametrize(
    ("change_id", "kind", "v1", "v2"),
    [
        ("rpc_renamed_get_product_details", "breaking",
         "rpc GetProductDetails (ProductRequest) returns (Product);", "rpc GetProduct (ProductRequest) returns (Product);"),
        ("number_reused_update_stock_request_2", "breaking", "sint32 delta = 2;", "string warehouse = 2;"),
        ("field_moved_update_stock_request_delta", "breaking", "sint32 delta = 2;", "sint32 delta = 4;"),
        ("type_changed_product_4", "breaking", "double price = 4;", "int64 price_cents = 4;"),
        ("number_reused_product_5", "breaking", "int32 stock = 5;", "int32 reserved = 5;"),
        ("field_moved_product_stock", "breaking", "int32 stock = 5;", "int32 stock = 15;"),
        ("field_added_factorial_request_2", "compatible", None, "bool use_cache = 2;"),
        ("field_added_factorial_reply_5", "compatible", None, "string algorithm = 5;"),
        ("package_renamed", "compatible", "package rpcexplorer.v1;", "package rpcexplorer.v2;"),
    ],
)
def test_known_changes_are_detected_and_classified(
    overview: dict[str, Any], change_id: str, kind: str, v1: str | None, v2: str | None
) -> None:
    change = changes_by_id(overview)[change_id]
    assert (change["kind"], change["v1"], change["v2"]) == (kind, v1, v2)


def test_wire_effects_quote_the_real_tags(overview: dict[str, Any]) -> None:
    changes = changes_by_id(overview)
    reuse = changes["number_reused_update_stock_request_2"]["wire_effect"]
    assert "0x10" in reuse and "0x12" in reuse and "VARINT" in reuse and "LEN" in reuse
    retyped = changes["type_changed_product_4"]["wire_effect"]
    assert "0x20" in retyped and "0x21" in retyped and "0.0" in retyped
    assert "0x28" in changes["number_reused_product_5"]["wire_effect"]
    removed = changes["rpcs_removed_inventory_service"]
    assert removed["kind"] == "breaking"
    assert all(name in removed["title"] for name in ("StreamAnalytics", "BulkUpdateStock", "CheckStock"))


def test_changes_cover_every_difference_between_the_two_contracts(overview: dict[str, Any]) -> None:
    """Contre-vérification indépendante : chaque écart entre les descriptors figure dans un changement."""
    protobuf = [change for change in overview["changes"] if change["contract"] == "protobuf"]
    reported = {
        side: {line for change in protobuf for line in (change[side] or "").splitlines()} for side in ("v1", "v2")
    }
    old, new = pb_v1.DESCRIPTOR, pb_v2.DESCRIPTOR
    differing = 0
    for name, message in old.message_types_by_name.items():
        successor = new.message_types_by_name.get(name)
        if successor is None:
            assert f"message {name} {{ … }}" in reported["v1"]
            continue
        for number in {*message.fields_by_number, *successor.fields_by_number}:
            before, after = message.fields_by_number.get(number), successor.fields_by_number.get(number)
            if before and after and declaration(before) == declaration(after):
                continue
            differing += 1
            assert before is None or declaration(before) in reported["v1"], (name, number)
            assert after is None or declaration(after) in reported["v2"], (name, number)
    assert differing == 7   # FactorialRequest 2, FactorialReply 5, Product 4, 5 et 15, UpdateStockRequest 2 et 4
    old_rpcs = old.services_by_name["InventoryService"].methods_by_name
    new_rpcs = new.services_by_name["InventoryService"].methods_by_name
    for name in set(old_rpcs) ^ set(new_rpcs):
        side = "v1" if name in old_rpcs else "v2"
        assert any(line.startswith(f"rpc {name} ") for line in reported[side]), name


def test_changes_point_at_their_lines_in_the_sources(overview: dict[str, Any]) -> None:
    sources = {side: overview[f"proto_{side}"]["source"].splitlines() for side in ("v1", "v2")}
    located = 0
    for change in overview["changes"]:
        if change["contract"] != "protobuf":
            assert change["lines"] == {"v1": [], "v2": []}
            continue
        for side in ("v1", "v2"):
            declarations = (change[side] or "").splitlines()
            assert len(change["lines"][side]) == len(declarations), change["id"]
            for text, number in zip(declarations, change["lines"][side]):
                code = sources[side][number - 1].split("//")[0]
                assert " ".join(code.split()) == " ".join(text.replace("{ … }", "{").split())
                located += 1
    assert located >= 20
    linked = {change_id for row in overview["diff"] for change_id in row["change_ids"]}
    assert linked == {change["id"] for change in overview["changes"] if change["contract"] == "protobuf"}


def test_golden_rules_say_what_to_do_and_what_never_to_do(overview: dict[str, Any]) -> None:
    rules = overview["rules"]
    assert len(rules) >= 6
    for rule in rules:
        assert rule["title"].strip() and rule["text"].strip()
        assert isinstance(rule["good"], bool)
    assert {rule["good"] for rule in rules} == {True, False}
    forbidden = " ".join(rule["title"] for rule in rules if not rule["good"])
    advised = " ".join(rule["title"] for rule in rules if rule["good"])
    assert "numéro" in forbidden and "type" in forbidden and "RPC" in forbidden
    assert "Réserver" in advised and "nouveaux numéros" in advised


def test_jsonrpc_changes_match_what_the_servers_really_expose(lab: LabRuntime, overview: dict[str, Any]) -> None:
    """Sans IDL, ces changements sont relevés à la main : on les confronte aux deux squelettes réels."""
    v1 = {method["name"]: signature(method) for method in lab.servers["custom"].methods()}
    v2 = {method["name"]: signature(method) for method in lab.servers["custom_v2"].methods()}
    changes = changes_by_id(overview)
    renamed = changes["jsonrpc_method_renamed"]
    assert renamed["v1"] == v1["get_product_details"] and "get_product_details" not in v2
    assert renamed["v2"] == v2["get_product"] and "get_product" not in v1
    required = changes["jsonrpc_param_required"]
    assert (required["v1"], required["v2"]) == (v1["update_stock"], v2["update_stock"])
    assert v1["list_products"] == v2["list_products"]   # la rupture est dans le résultat : rien ne la décrit
    assert changes["jsonrpc_result_keys_renamed"]["kind"] == "breaking"
    assert changes["jsonrpc_result_key_added"]["kind"] == "compatible"


def test_diff_engine_classifies_changes_absent_from_the_lab_contracts() -> None:
    old = build_contract(
        "old.proto",
        {
            "Req": [("a", 1, "int32"), ("b", 2, "string"), ("c", 3, "int32"), ("d", 4, "sint32")],
            "Rep": [("x", 1, "int32"), ("y", 2, "double")],
        },
        [("Get", "Req", "Rep"), ("Old", "Req", "Rep"), ("Swap", "Req", "Rep")],
    )
    new = build_contract(
        "new.proto",
        {
            "Req": [("a", 1, "int64"), ("renamed_b", 2, "string"), ("d", 4, "int32")],
            "Rep": [("x", 1, "int32")],
            "Audit": [("who", 1, "string")],
        },
        [("Get", "Req", "Rep"), ("Fresh", "Rep", "Audit"), ("Swap", "Rep", "Rep")],
    )
    changes = {change["id"]: change for change in contract.diff_contracts(old, new)}
    assert {change_id: change["kind"] for change_id, change in changes.items()} == {
        "rpc_retyped_swap": "breaking",
        "rpcs_removed_demo": "breaking",
        "rpcs_added_demo": "compatible",
        "type_changed_req_1": "compatible",     # int32 → int64 : interchangeables sur le fil
        "field_renamed_req_2": "compatible",    # les noms ne voyagent pas
        "field_removed_req_3": "breaking",
        "type_changed_req_4": "breaking",       # sint32 → int32 : même type de fil, autre encodage
        "field_removed_rep_2": "breaking",
        "messages_added": "compatible",
    }
    # Le sens de circulation décide du récit : « Req » est écrit par le client, « Rep » par le serveur.
    assert changes["field_removed_req_3"]["wire_effect"].startswith("Un client v1 envoie")
    assert changes["field_removed_rep_2"]["wire_effect"].startswith("Le serveur v2 n'écrit plus")
    assert contract.diff_contracts(old, old) == []


# --- Scénarios ------------------------------------------------------------------

def test_scenario_metadata_references_existing_changes(overview: dict[str, Any]) -> None:
    scenarios = contract.CONTRACT_SCENARIOS
    assert [scenario["id"] for scenario in scenarios] == list(EXPECTED_OUTCOMES)
    changes = changes_by_id(overview)
    for scenario in scenarios:
        assert scenario["expected_outcome"] == EXPECTED_OUTCOMES[scenario["id"]]
        assert scenario["protocol"] == ("custom" if scenario["id"].startswith("jsonrpc_") else "grpc")
        assert scenario["title"].strip() and scenario["summary"].strip()
        for change_id in (scenario["change_id"], *scenario["related_change_ids"]):
            assert change_id in changes, (scenario["id"], change_id)
            assert scenario["id"] in changes[change_id]["scenarios"]
        expected_contract = "jsonrpc" if scenario["protocol"] == "custom" else "protobuf"
        assert changes[scenario["change_id"]]["contract"] == expected_contract


@pytest.mark.parametrize("scenario_id", list(EXPECTED_OUTCOMES))
def test_each_scenario_ends_with_its_expected_outcome(
    lab: LabRuntime, results: dict[str, dict[str, Any]], scenario_id: str
) -> None:
    result = results[scenario_id]
    assert result["outcome"] == result["expected_outcome"] == EXPECTED_OUTCOMES[scenario_id]
    assert result["matches"] is True
    assert result["outcome_label"] == contract.OUTCOME_INFO[result["outcome"]]["label"]
    assert result["change"]["id"] == result["change_id"]
    assert {"sent", "expected", "observed", "explanation", "wire", "status", "call_id", "duration_ms"} <= set(result)
    assert result["sent"]["contract"] == "v1" and result["sent"]["call"].startswith("client.")
    assert result["expected"]["status"] == "OK"
    assert len(result["explanation"]) > 120 and "inattendu" not in result["explanation"]
    assert lab.collector.get(result["call_id"]) is not None   # la trace de l'appel est consultable
    json.dumps(result)


def test_rejections_carry_the_status_of_their_protocol(results: dict[str, dict[str, Any]]) -> None:
    statuses = {
        "renamed_rpc": "UNIMPLEMENTED",
        "field_reuse_strict": "INVALID_ARGUMENT",
        "jsonrpc_new_required_param": "-32602",
        "jsonrpc_renamed_method": "-32601",
    }
    for scenario_id, status in statuses.items():
        result = results[scenario_id]
        observed = result["observed"]
        assert result["status"] == observed["status"] == status
        assert observed["ok"] is False and observed["result"] is None and observed["client_view"] is None
        assert observed["error"]["code"] == observed["code"] != "OK"
        assert status in result["explanation"]
    assert results["renamed_rpc"]["observed"]["code"] == METHOD_NOT_FOUND
    assert results["renamed_rpc"]["sent"]["target"] == "/rpcexplorer.v1.InventoryService/GetProductDetails"
    assert results["renamed_rpc"]["wire"]["request"]["as_v2"] is None   # la v2 n'a plus de RPC à ce chemin


def test_silent_corruption_answers_ok_but_the_stock_did_not_move(results: dict[str, dict[str, Any]]) -> None:
    result = results["field_reuse_silent"]
    observed, expected = result["observed"], result["expected"]
    assert observed["ok"] is True and result["status"] == "OK" and observed["error"] is None
    assert observed["stock_before"][PRODUCT]["stock"] == INITIAL_STOCK
    assert observed["server_truth"][PRODUCT]["stock"] == INITIAL_STOCK          # rien n'a bougé…
    assert expected["server_state"][PRODUCT]["stock"] == INITIAL_STOCK - 3      # …alors qu'un serveur v1 aurait débité
    assert observed["client_view"] == {"previous_stock": 84, "new_stock": 84, "delta": 0, "applied": True}
    assert expected["client_view"] == {"previous_stock": 84, "new_stock": 81, "delta": -3, "applied": True}
    assert {"where": "server", "path": f"{PRODUCT}.stock", "expected": 81, "observed": 84} in observed["differences"]
    assert "84" in result["explanation"] and "81" in result["explanation"] and "0x10" in result["explanation"]


def test_the_same_bytes_are_read_differently_by_each_contract(results: dict[str, dict[str, Any]]) -> None:
    wire = results["field_reuse_silent"]["wire"]
    assert wire["format"] == "protobuf" and wire["focus"] == "request"
    request = wire["request"]
    sent = pb_v1.UpdateStockRequest(product_id=PRODUCT, delta=-3).SerializeToString()
    assert request["hex"] == sent.hex() == "0a08534b552d313030311005"
    assert request["size"] == len(sent) and request["written_by"] == "v1"
    as_v1, as_v2 = request["as_v1"], request["as_v2"]
    assert as_v1["message"] == {"product_id": PRODUCT, "delta": -3, "idempotency_key": ""}
    assert as_v2["message"] == {"product_id": PRODUCT, "warehouse": "", "idempotency_key": "", "delta": 0}
    assert as_v1["fields"] != as_v2["fields"] and as_v1["segments"] != as_v2["segments"]
    assert [node["name"] for node in as_v1["fields"]] == ["product_id", "delta"]
    assert [node["name"] for node in as_v2["fields"]] == ["product_id", "warehouse"]
    assert as_v1["lost"] == []
    assert [(entry["number"], entry["reason"]) for entry in as_v2["lost"]] == [(2, "wire_type_mismatch")]
    for reading in (as_v1, as_v2):   # les segments couvrent tous les octets, sans trou
        bounds = [(segment["start"], segment["end"]) for segment in reading["segments"]]
        assert bounds[0][0] == 0 and bounds[-1][1] == request["size"]
        assert all(previous[1] == following[0] for previous, following in zip(bounds, bounds[1:]))
    # Le serveur strict reçoit exactement les mêmes octets : seule sa validation change l'issue.
    assert results["field_reuse_strict"]["wire"]["request"]["hex"] == request["hex"]
    assert results["field_reuse_strict"]["wire"]["response"] is None


@pytest.mark.parametrize("scenario_id", GRPC_SCENARIOS)
def test_wire_bytes_are_those_of_the_traced_call(
    lab: LabRuntime, results: dict[str, dict[str, Any]], scenario_id: str
) -> None:
    wire = results[scenario_id]["wire"]
    payloads = {event.stage: event.payload for event in lab.collector.get(results[scenario_id]["call_id"]).events}
    assert payloads["client.send"][FRAME_HEADER_SIZE:].hex() == wire["request"]["hex"]
    if results[scenario_id]["observed"]["ok"]:
        assert payloads["client.receive"][FRAME_HEADER_SIZE:].hex() == wire["response"]["hex"]
    else:
        assert wire["response"] is None


def test_added_field_is_skipped_by_the_old_client(results: dict[str, dict[str, Any]]) -> None:
    result = results["added_field"]
    assert result["observed"]["client_view"] == result["expected"]["client_view"]
    assert result["observed"]["differences"] == []
    response = result["wire"]["response"]
    assert result["wire"]["focus"] == "response" and response["written_by"] == "v2"
    assert response["as_v2"]["message"]["algorithm"] == conv.V2_FACTORIAL_ALGORITHM
    assert "algorithm" not in response["as_v1"]["message"]
    assert [(entry["number"], entry["reason"]) for entry in response["as_v1"]["lost"]] == [(5, "unknown_field")]
    assert response["as_v2"]["lost"] == []


def test_type_change_makes_the_old_client_display_wrong_data(
    lab: LabRuntime, results: dict[str, dict[str, Any]]
) -> None:
    result = results["type_change_read"]
    observed = result["observed"]
    view, truth = observed["client_view"], observed["server_truth"]
    assert observed["ok"] is True and len(view) == contract.LISTED_PRODUCTS and list(view) == list(truth)
    for product_id, seen in view.items():
        real = lab.service_v2.get_product_details(product_id)
        assert truth[product_id] == {"stock": real["stock"], "price": real["price"]}
        assert seen == {"stock": real["stock"] // 10, "price": 0.0}   # « reserved » pris pour le stock, prix ignoré
        assert real["price"] > 0
    assert len([difference for difference in observed["differences"] if difference["where"] == "client"]) == 6
    lost = {entry["path"]: entry for entry in result["wire"]["response"]["as_v1"]["lost"]}
    assert set(lost) == {"products.price", "products.#15"}
    assert lost["products.price"]["count"] == contract.LISTED_PRODUCTS
    assert lost["products.price"]["expected_wire_type"] == "I64" and lost["products.price"]["wire_type"] == "VARINT"
    first = result["wire"]["response"]["as_v2"]["message"]["products"][0]
    assert first["price_cents"] == 12990 and first["stock"] == INITIAL_STOCK and first["reserved"] == 8


def test_renamed_result_keys_crash_the_client_after_a_successful_call(results: dict[str, dict[str, Any]]) -> None:
    result = results["jsonrpc_renamed_result_key"]
    observed = result["observed"]
    assert observed["ok"] is True and result["status"] == "OK" and observed["error"] is None
    assert observed["client_crash"] == {"type": "KeyError", "message": "'stock'"}
    assert observed["client_view"] is None
    product = observed["result"]["products"][0]
    assert {"price_cents", "quantity"} <= set(product) and not {"price", "stock"} & set(product)
    wire = result["wire"]
    assert wire["format"] == "json" and wire["focus"] == "response"
    assert wire["request"]["document"]["method"] == "list_products"
    assert wire["response"]["document"]["result"]["products"][0]["quantity"] == INITIAL_STOCK
    assert wire["response"]["size"] == len(wire["response"]["text"].encode("utf-8"))


def test_added_result_key_is_harmless_for_a_tolerant_reader(results: dict[str, dict[str, Any]]) -> None:
    result = results["jsonrpc_added_key"]
    assert "algorithm" in result["observed"]["result"] and "algorithm" not in result["expected"]["result"]
    assert result["observed"]["client_view"] == result["expected"]["client_view"]
    assert results["jsonrpc_new_required_param"]["wire"]["response"]["document"]["error"]["code"] == -32602


def test_strict_flag_is_restored_whatever_it_was(lab: LabRuntime) -> None:
    assert lab.grpc_v2.strict is False
    assert contract.run_contract_scenario(lab, "field_reuse_strict")["outcome"] == "rejected"
    assert lab.grpc_v2.strict is False
    lab.grpc_v2.strict = True
    try:
        # Le scénario « permissif » impose son réglage le temps de l'appel, puis rend celui de l'utilisateur.
        assert contract.run_contract_scenario(lab, "field_reuse_silent")["outcome"] == "silent_corruption"
        assert lab.grpc_v2.strict is True
    finally:
        lab.grpc_v2.strict = False


def test_scenarios_leave_the_lab_as_they_found_it(lab: LabRuntime) -> None:
    main_before = lab.service.stats()
    network_before = lab.conditions.snapshot()
    for scenario_id in EXPECTED_OUTCOMES:
        contract.run_contract_scenario(lab, scenario_id)
        details = lab.service_v2.get_product_details(PRODUCT)
        assert (details["stock"], details["version"]) == (INITIAL_STOCK, 1), scenario_id
        assert lab.service_v2.stats()["operations"] == 0, scenario_id
    assert lab.service.stats() == main_before   # l'inventaire principal n'est jamais touché
    assert lab.conditions.snapshot() == network_before
    assert lab.grpc_v2.strict is False


def test_concurrent_scenarios_do_not_share_the_strict_switch(lab: LabRuntime) -> None:
    """Deux scénarios lancés ensemble (deux onglets du dashboard) ne se volent ni « strict » ni l'inventaire."""
    scenario_ids = ["field_reuse_strict", "field_reuse_silent"] * 4
    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(lambda name: contract.run_contract_scenario(lab, name)["outcome"], scenario_ids))
    assert outcomes == ["rejected", "silent_corruption"] * 4
    assert lab.grpc_v2.strict is False
    assert stock_of(lab.service_v2) == INITIAL_STOCK


def test_run_all_plays_the_scenarios_in_order(results: dict[str, dict[str, Any]]) -> None:
    assert list(results) == list(EXPECTED_OUTCOMES)
    assert {result["outcome"] for result in results.values()} == set(contract.OUTCOMES)


def test_unknown_scenario_is_refused(lab: LabRuntime) -> None:
    with pytest.raises(ValueError, match="field_reuse_silent"):
        contract.run_contract_scenario(lab, "nope")


def test_scenarios_still_conclude_when_the_bus_is_muted(lab: LabRuntime, results: dict[str, dict[str, Any]]) -> None:
    with lab.bus.muted():
        silent = contract.run_contract_scenario(lab, "field_reuse_silent")
        crash = contract.run_contract_scenario(lab, "jsonrpc_renamed_result_key")
    assert (silent["outcome"], crash["outcome"]) == ("silent_corruption", "crash")
    assert silent["call_id"] == crash["call_id"] == ""   # aucun appel tracé
    # La requête gRPC est re-sérialisée par le contrat v1 : elle reste disponible, à l'identique.
    assert silent["wire"]["request"] == results["field_reuse_silent"]["wire"]["request"]
    assert silent["wire"]["response"] is None
    assert crash["wire"] is None
    assert crash["explanation"] == results["jsonrpc_renamed_result_key"]["explanation"]


def test_cli_prints_changes_scenarios_and_rules(capsys: pytest.CaptureFixture[str]) -> None:
    assert contract.main([]) == 0
    output = capsys.readouterr().out
    for expected in ("RUPTURE", "CORRUPTION SILENCIEUSE", "PLANTAGE DU CLIENT", "REJET EXPLICITE", "COMPATIBLE",
                     "Règles d'or", "deux lectures", "Bilan"):
        assert expected in output, expected
    assert "INATTENDU" not in output
    # Les messages JSON-RPC sont abrégés à la largeur de la console ; leurs libellés, jamais.
    assert 'Requête  {"jsonrpc"' in output and 'Réponse  {"jsonrpc"' in output
    assert contract.main(["--scenario", "added_field", "--diff"]) == 0
    output = capsys.readouterr().out
    assert "string algorithm = 5;" in output and "CORRUPTION SILENCIEUSE" not in output


# --- Transparence de localisation ----------------------------------------------------

def test_code_comparison_has_the_documented_shape() -> None:
    comparison = transparency.code_comparison()
    assert {"operation", "snippets", "javascript_fetch", "takeaway"} <= set(comparison)
    assert [snippet["id"] for snippet in comparison["snippets"]] == ["local", "custom", "grpc", "rest"]
    for snippet in comparison["snippets"]:
        assert snippet["language"] == "python"
        assert snippet["code"].startswith(f"def {snippet['function']}(")
        assert snippet["title"].strip() and snippet["note"].strip() and snippet["call"].startswith(snippet["function"])
        assert isinstance(snippet["lines"], int) and snippet["lines"] > 0
        assert all(isinstance(concern, str) and concern for concern in snippet["concerns"])
    script = comparison["javascript_fetch"]
    assert script["language"] == "javascript" and script["lines"] > 0 and script["concerns"]
    assert "fetch(`/api/stock/" in script["code"] and "method: 'POST'" in script["code"]
    assert "JSON.stringify" in script["code"]
    assert comparison["takeaway"].strip() and "SKU-1001" in comparison["operation"]
    json.dumps(comparison)


def test_every_snippet_compiles_and_is_the_code_that_runs() -> None:
    for snippet in transparency.code_comparison()["snippets"]:
        compile(snippet["code"], f"<{snippet['id']}>", "exec")
        function = getattr(transparency, snippet["function"])
        assert snippet["code"] == textwrap.dedent(inspect.getsource(function)).rstrip()


def test_line_counts_ignore_blank_lines_comments_and_docstrings() -> None:
    sample = 'def f(x):\n    """Doc.\n\n    Suite.\n    """\n    # commentaire\n\n    y = x + 1  # compte\n    return y\n'
    assert transparency.count_code_lines(sample) == 3
    assert transparency.count_code_lines("// note\n\nconst a = 1;\nreturn a;\n", "javascript") == 2
    lines = {snippet["id"]: snippet["lines"] for snippet in transparency.code_comparison()["snippets"]}
    concerns = {snippet["id"]: len(snippet["concerns"]) for snippet in transparency.code_comparison()["snippets"]}
    assert lines["local"] == lines["custom"] == 3   # la transparence : la même écriture, à la ligne près
    assert lines["grpc"] == 4
    assert lines["rest"] >= 4 * lines["local"]
    assert concerns["local"] == concerns["custom"] == 0 < concerns["grpc"] < concerns["rest"]


def test_the_four_writings_really_run_and_do_the_same_thing(lab: LabRuntime) -> None:
    before = stock_of(lab.service, OTHER_PRODUCT)
    stub = RpcClientStub(*lab.endpoint("custom"), bus=lab.bus)
    channel = grpc.insecure_channel("{}:{}".format(*lab.endpoint("grpc")))
    try:
        assert transparency.update_stock_local(InventoryService(), OTHER_PRODUCT) == before - 3
        assert transparency.update_stock_custom_rpc(stub, OTHER_PRODUCT) == before - 3
        assert transparency.update_stock_grpc(InventoryServiceStub(channel), OTHER_PRODUCT) == before - 6
        assert transparency.update_stock_rest_by_hand(*lab.endpoint("rest"), OTHER_PRODUCT, quantity=4) == before - 10
        assert stock_of(lab.service, OTHER_PRODUCT) == before - 10
    finally:
        stub.close()
        channel.close()
        lab.service.update_stock(OTHER_PRODUCT, before - stock_of(lab.service, OTHER_PRODUCT))


def test_rest_by_hand_translates_http_errors_into_exceptions(lab: LabRuntime) -> None:
    host, port = lab.endpoint("rest")
    with pytest.raises(NotFoundError, match="SKU-0000"):
        transparency.update_stock_rest_by_hand(host, port, "SKU-0000")
    with pytest.raises(FailedPreconditionError):
        transparency.update_stock_rest_by_hand(host, port, OTHER_PRODUCT, quantity=10_000)


def test_run_comparison_proves_the_four_writings_equivalent(lab: LabRuntime) -> None:
    before = stock_of(lab.service)
    traced = len(lab.collector.recent(400))
    outcome = transparency.run_comparison(lab, repeats=3)
    runs = outcome["runs"]
    assert [run["id"] for run in runs] == ["local", "custom", "grpc", "rest"]
    assert all(run["ok"] and run["error"] is None for run in runs)
    assert {run["result"] for run in runs} == {before - 3} == {outcome["expected_new_stock"]}
    assert outcome["equivalent"] is True and str(before - 3) in outcome["summary"]
    assert "connexion TCP" in outcome["summary"]   # la durée REST inclut l'ouverture de connexion : c'est dit
    assert all(run["duration_ms"] >= run["min_ms"] > 0 for run in runs)
    assert runs[0]["ratio_to_local"] == 1.0 and all(run["ratio_to_local"] > 1 for run in runs[1:])
    assert outcome["stock_before"] == outcome["stock_after"] == stock_of(lab.service) == before
    assert len(lab.collector.recent(400)) == traced   # bus coupé : la comparaison ne laisse aucune trace
    json.dumps(outcome)


def test_run_comparison_agrees_on_failures_too(lab: LabRuntime) -> None:
    before = stock_of(lab.service)
    outcome = transparency.run_comparison(lab, quantity=10_000, repeats=2)
    assert all(not run["ok"] and run["result"] is None for run in outcome["runs"])
    assert {run["error"]["code"] for run in outcome["runs"]} == {FAILED_PRECONDITION}
    # Chaque écriture échoue avec l'exception de son middleware ; seul le code canonique les réunit.
    assert [run["error"]["type"] for run in outcome["runs"]] == [
        "InsufficientStock", "FailedPreconditionError", "grpc.RpcError", "FailedPreconditionError",
    ]
    assert outcome["equivalent"] is True and FAILED_PRECONDITION in outcome["summary"]
    assert stock_of(lab.service) == before
    with pytest.raises(ValueError, match="repeats"):
        transparency.run_comparison(lab, repeats=0)


def test_concurrent_comparisons_do_not_disturb_each_other(lab: LabRuntime) -> None:
    before = stock_of(lab.service)
    with ThreadPoolExecutor(max_workers=3) as pool:
        outcomes = list(pool.map(lambda _: transparency.run_comparison(lab, repeats=1), range(3)))
    assert all(outcome["equivalent"] for outcome in outcomes)
    assert {run["result"] for outcome in outcomes for run in outcome["runs"]} == {before - 3}
    assert stock_of(lab.service) == before


def test_transparency_cli_shows_the_four_writings_and_their_results(capsys: pytest.CaptureFixture[str]) -> None:
    assert transparency.main() == 0
    output = capsys.readouterr().out
    for snippet in transparency.code_comparison()["snippets"]:
        assert f"def {snippet['function']}(" in output
        assert snippet["title"] in output
    assert "Transparence de localisation" in output and "× local" in output
    assert "renvoient le même nouveau stock" in output
