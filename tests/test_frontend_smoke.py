"""Tests de bout en bout du dashboard, dans un vrai navigateur (plan §14).

Un navigateur sans interface (Microsoft Edge, ou à défaut le Chromium de Playwright) ouvre
le dashboard d'un laboratoire à ports éphémères servi par uvicorn dans un thread ; les
rapports vont dans un dossier temporaire. Chaque test manipule l'interface comme un
utilisateur, puis échoue aussi si la page a produit une erreur de console, une exception
JavaScript, une requête en échec ou une réponse HTTP ≥ 400. Sans Playwright ou sans
navigateur, le module entier est ignoré.

    .venv/Scripts/python.exe -m pytest tests/test_frontend_smoke.py -q -o addopts=""
"""
from __future__ import annotations

import json
import re
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, ContextManager, Iterator

import pytest

pytest.importorskip("playwright.sync_api", reason="Playwright n'est pas installé : tests du navigateur ignorés.")

# Imports placés après ``importorskip`` : sans Playwright, aucun d'eux ne doit être tenté.
from playwright.sync_api import Browser, Locator, Page, expect, sync_playwright

from _frontend_support import BrowserUnavailable, LiveDashboard, PageWatch, launch_browser, lingering_threads
from common import config
from common.telemetry import EventBus
from lab import LabRuntime

WAIT_MS = 15_000            # rendu d'une page, réponse d'un appel
LONG_WAIT_MS = 120_000      # banc d'essai, campagne de mesure du bilan
VIEWPORT_HEIGHT = 900
DEFAULT_WIDTH = 1440

# Le plus petit banc d'essai que l'API accepte de bon cœur : il sert à remplir les pages, pas à mesurer.
TINY_BENCHMARK = {
    "iterations": 20,
    "warmup": 2,
    "serialization_iterations": 30,
    "sweep_latencies_ms": [0, 5],
    "sweep_iterations": 3,
    "wire_calls": 3,
    "scaling_limits": [1, 10],
}
DUPLICATE_EXECUTION = "duplicate_execution"
OUTCOME_BADGES = {
    "compatible": "Compatible",
    "rejected": "Rejet",
    "crash": "Plantage",
    "silent_corruption": "Corruption silencieuse",
}


@dataclass(frozen=True)
class Route:
    """Une page du dashboard : son identifiant d'URL, son libellé de navigation (aussi titre de
    l'onglet) et un contenu qu'elle ne peut afficher qu'une fois les données du laboratoire reçues."""

    id: str
    label: str
    content: Callable[[Page], Locator]


ROUTES: tuple[Route, ...] = (
    Route("overview", "Vue d’ensemble", lambda page: page.get_by_role("status").filter(has_text="serveurs en service")),
    Route("console", "Console RPC", lambda page: page.get_by_role("radiogroup", name="Protocole")),
    Route("xray", "Sous le capot", lambda page: page.get_by_role("tablist", name="Protocole disséqué")),
    Route("benchmark", "Benchmark", lambda page: page.get_by_text(re.compile(r"^(Aucun rapport|Rapport du )"))),
    Route("chaos", "Chaos réseau", lambda page: page.get_by_role("group", name="Préréglages réseau")),
    Route("contract", "Contrat & IDL", lambda page: page.get_by_role("heading", name="Expérimenter la rupture")),
    Route("compare", "Transparence", lambda page: page.get_by_role("button", name="Exécuter les quatre")),
    Route("learn", "Bilan", lambda page: page.get_by_label("Provenance des chiffres").get_by_role("heading")),
)

Visit = Callable[..., ContextManager[Page]]


# --- Laboratoire, dashboard, navigateur ------------------------------------------------

@pytest.fixture(scope="module")
def browser() -> Iterator[Browser]:
    """Un navigateur sans interface pour tout le module — ou le module entier ignoré s'il est introuvable."""
    with sync_playwright() as playwright:
        try:
            chromium = launch_browser(playwright)
        except BrowserUnavailable as exc:
            pytest.skip(f"Aucun navigateur ne peut être lancé ({exc})")
        expect.set_options(timeout=WAIT_MS)
        yield chromium
        chromium.close()


@pytest.fixture(scope="module")
def reports_dir(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """Dossier temporaire des rapports : aucun banc d'essai du module n'écrit dans le dépôt."""
    folder = tmp_path_factory.mktemp("reports")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(config, "REPORTS_DIR", folder)
        yield folder


@pytest.fixture(scope="module")
def dashboard(browser: Browser, reports_dir: Path) -> Iterator[LiveDashboard]:
    """Un laboratoire réel et son dashboard, pour tout le module ; rien ne lui survit."""
    threads_before = set(threading.enumerate())
    with LabRuntime.ephemeral(bus=EventBus()) as runtime, LiveDashboard(runtime) as live:
        yield live
        live.wait_until_idle()      # ne pas arrêter les serveurs sous un banc d'essai laissé en route par un test en échec
    assert not lingering_threads(threads_before), "des threads du laboratoire ou du dashboard ont survécu à l'arrêt"


@pytest.fixture(autouse=True)
def fresh_lab(dashboard: LiveDashboard) -> None:
    """Chaque test part d'un laboratoire remis à zéro : stocks, réseau idéal, aucun résultat de scénario."""
    dashboard.reset()


@pytest.fixture
def visit(browser: Browser, dashboard: LiveDashboard) -> Visit:
    """Ouvre une page du dashboard : ``with visit("console") as page``.

    En sortie de bloc, le test échoue si la page a signalé la moindre anomalie ; si le test
    échoue de lui-même, les anomalies relevées sont jointes à son erreur.
    """

    @contextmanager
    def open_page(route: str, *, width: int = DEFAULT_WIDTH) -> Iterator[Page]:
        context = browser.new_context(
            viewport={"width": width, "height": VIEWPORT_HEIGHT},
            locale="fr-FR",
            color_scheme="dark",    # préférence du système : sans choix mémorisé, l'interface démarre en sombre
            accept_downloads=True,
        )
        context.set_default_timeout(WAIT_MS)
        page = context.new_page()
        watch = PageWatch(page)
        try:
            page.goto(f"{dashboard.base_url}/#/{route}")
            # « En direct » : l'état du laboratoire est chargé et le WebSocket est connecté.
            expect(live_indicator(page)).to_be_visible()
            yield page
        except BaseException as exc:
            if watch.problems:
                exc.add_note(f"Anomalies relevées dans la page :\n{watch.report()}")
            raise
        else:
            if watch.problems:
                pytest.fail(f"La page a signalé des anomalies :\n{watch.report()}", pytrace=False)
        finally:
            context.close()

    return open_page


# --- Gestes et lectures communs --------------------------------------------------------

def live_indicator(page: Page) -> Locator:
    return page.get_by_role("status").filter(has_text="En direct")


def network_chip(page: Page) -> Locator:
    """La puce « conditions réseau » de la barre haute (un lien vers la page Chaos réseau)."""
    return page.get_by_role("banner").locator("a[href='#/chaos']")


def stat(page: Page, label: str) -> Locator:
    """Le nombre affiché par la tuile de mesure ``label`` (il s'incrémente jusqu'à sa valeur finale)."""
    return page.locator(".stat").filter(has_text=label).locator(".stat__number")


def go_to(page: Page, route: Route) -> None:
    """Change de page dans l'onglet ouvert et attend que son contenu soit affiché."""
    page.goto(f"{page.url.split('#')[0]}#/{route.id}")
    expect(route.content(page).first).to_be_visible()


def run_console_call(page: Page) -> None:
    page.get_by_role("button", name=re.compile(r"^Exécuter(?! l’appel)")).click()


def set_number(field: Locator, value: int) -> None:
    field.fill(str(value))
    field.press("Enter")


def measure_everything(dashboard: LiveDashboard) -> None:
    """Joue un banc d'essai minuscule, les scénarios de contrat et un scénario de panne : les pages ont des résultats à montrer."""
    dashboard.api("POST", "/api/benchmark/run", TINY_BENCHMARK)
    dashboard.wait_until_idle()
    dashboard.api("POST", "/api/contract/run", {"scenario": "all"})
    dashboard.api("POST", "/api/failures/run", {"scenario": DUPLICATE_EXECUTION, "protocol": "custom"})
    dashboard.wait_until_idle()


# --- Coquille : état, navigation, thème, palette, écoute du fil ------------------------

def test_shell_shows_the_three_servers_and_the_live_stream(visit: Visit, dashboard: LiveDashboard) -> None:
    with visit("overview") as page:
        expect(page).to_have_title(re.compile("Vue d’ensemble"))
        sidebar = page.get_by_role("complementary").filter(has=page.get_by_role("navigation", name="Navigation principale"))
        servers = sidebar.get_by_role("listitem").filter(has=page.get_by_role("img", name="en service"))
        ports = dashboard.runtime.ports
        expect(servers).to_contain_text(["JSON-RPC maison", "gRPC / Protobuf", "REST / JSON"])
        expect(servers).to_contain_text([f":{ports.custom}", f":{ports.grpc}", f":{ports.rest}"])
        expect(sidebar).to_contain_text(f"v{config.VERSION}")
        expect(live_indicator(page)).to_have_attribute("data-state", "live")
        expect(page.get_by_role("status").filter(has_text="serveurs en service")).to_contain_text("3/3")
        expect(network_chip(page)).to_contain_text("Réseau idéal")


def test_navigation_updates_the_title_on_every_page(visit: Visit) -> None:
    with visit("overview") as page:
        navigation = page.get_by_role("navigation", name="Navigation principale")
        for route in reversed(ROUTES):      # « Vue d'ensemble » est déjà ouverte : elle est revisitée en dernier
            link = navigation.get_by_role("link", name=route.label)
            link.click()
            expect(page).to_have_url(re.compile(rf"#/{route.id}$"))
            expect(page).to_have_title(re.compile(re.escape(route.label)))
            expect(link).to_have_attribute("aria-current", "page")
            expect(page.get_by_role("navigation", name="Fil d’Ariane")).to_contain_text(route.label)
            expect(route.content(page).first).to_be_visible()
        page.go_back()
        expect(page).to_have_title(re.compile(re.escape(ROUTES[1].label)))


def test_theme_toggle_persists_across_reload(visit: Visit) -> None:
    with visit("overview") as page:
        root = page.locator("html")
        background = "getComputedStyle(document.documentElement).getPropertyValue('--bg-0')"
        expect(root).to_have_attribute("data-theme", "dark")
        dark = page.evaluate(background)

        page.get_by_role("button", name="Passer au thème clair").click()
        expect(root).to_have_attribute("data-theme", "light")
        assert page.evaluate(background) != dark

        page.reload()   # sans mémoire du choix, la préférence du système (sombre) reprendrait la main
        expect(root).to_have_attribute("data-theme", "light")
        expect(page.get_by_role("button", name="Passer au thème sombre")).to_be_visible()


def test_command_palette_filters_and_navigates(visit: Visit) -> None:
    with visit("overview") as page:
        page.keyboard.press("Control+K")
        palette = page.get_by_role("dialog", name="Palette de commandes")
        expect(palette.get_by_role("combobox", name="Rechercher une commande")).to_be_focused()
        commands = palette.get_by_role("option")
        expect(commands.first).to_be_visible()
        everything = commands.count()

        page.keyboard.type("contrat")
        expect(commands.first).to_contain_text("Contrat & IDL")
        expect(commands.first).to_have_attribute("aria-selected", "true")
        assert commands.count() < everything

        page.keyboard.press("Enter")
        expect(palette).to_be_hidden()
        expect(page).to_have_url(re.compile(r"#/contract$"))
        expect(page).to_have_title(re.compile("Contrat & IDL"))


def test_wiretap_lists_the_messages_of_a_call(visit: Visit) -> None:
    with visit("console") as page:
        toggle = page.get_by_role("button", name="Sous le capot", exact=True)
        toggle.click()
        expect(toggle).to_have_attribute("aria-pressed", "true")
        drawer = page.get_by_role("complementary", name="Sous le capot")
        log = drawer.get_by_role("log", name="Messages sur le fil")
        expect(log).to_contain_text("En écoute")

        run_console_call(page)      # update_stock par JSON-RPC : deux messages émis, deux reçus
        success = page.get_by_role("status").filter(has_text="Succès")
        expect(success).to_be_visible()
        call_id = re.search(r"custom-\d+", success.inner_text()).group()
        messages = log.get_by_role("button")
        expect(messages).to_have_count(4)
        for stage in ("client.send", "server.receive", "server.send", "client.receive"):
            expect(messages.filter(has_text=stage)).to_contain_text("update_stock")
        expect(drawer).to_contain_text("4 messages")

        messages.first.click()
        expect(messages.first).to_have_attribute("aria-expanded", "true")
        expect(log).to_contain_text(call_id)


# --- Console RPC -----------------------------------------------------------------------

def test_console_sync_call_reports_duration_and_sizes(visit: Visit, dashboard: LiveDashboard) -> None:
    with visit("console") as page:
        page.get_by_role("radiogroup", name="Protocole").get_by_role("radio", name="gRPC").click()
        expect(page.get_by_role("button", name="Procédure", exact=True)).to_contain_text("update_stock")
        run_console_call(page)

        success = page.get_by_role("status").filter(has_text="Succès")
        expect(success).to_contain_text(re.compile(r"update_stock a répondu en [\d,]+\s(µs|ms|s) par gRPC / Protobuf"))
        call_id = re.search(r"grpc-\d+", success.inner_text()).group()
        measured = dashboard.api("GET", f"/api/traces/{call_id}")["summary"]
        expect(stat(page, "Durée")).to_have_text(re.compile(r"^\d[\d,]*$"))
        expect(stat(page, "Requête")).to_have_text(str(measured["request_bytes"]))
        expect(stat(page, "Réponse")).to_have_text(str(measured["response_bytes"]))
        expect(page.get_by_role("tab", name="Résultat")).to_have_attribute("aria-selected", "true")


def test_console_unknown_product_shows_not_found(visit: Visit) -> None:
    with visit("console") as page:
        page.get_by_role("radiogroup", name="Protocole").get_by_role("radio", name="REST").click()
        page.get_by_role("combobox", name="Paramètre product_id").click()
        page.get_by_role("option", name="référence inconnue").click()
        run_console_call(page)

        failure = page.get_by_role("alert").filter(has_text="Échec de l’appel")
        expect(failure).to_contain_text("NOT_FOUND")
        expect(failure).to_contain_text("SKU-9999")
        expect(failure).to_contain_text("non rejouable")
        expect(page.get_by_role("tab", name="Erreur")).to_have_attribute("aria-selected", "true")


def test_console_async_batch_shows_a_speedup(visit: Visit) -> None:
    with visit("console") as page:
        page.get_by_role("radiogroup", name="Mode d’exécution").get_by_role("radio", name="Asynchrone").click()
        expect(page.get_by_role("spinbutton", name="Nombre d’appels lancés ensemble")).to_have_value("20")
        run_console_call(page)

        expect(page.get_by_role("status").filter(has_text="20 appels réussis")).to_be_visible()
        verdict = page.get_by_text(re.compile(r"plus vite qu’en file indienne"))
        expect(verdict).to_be_visible()
        factor = re.search(r"soit ([\d,]+)\s×", verdict.inner_text()).group(1)
        assert float(factor.replace(",", ".")) > 1
        expect(stat(page, "Accélération")).to_have_text(factor)


def test_console_stream_shows_items_arriving(visit: Visit) -> None:
    samples = 6
    with visit("console") as page:
        page.get_by_role("button", name="Procédure", exact=True).click()
        page.get_by_role("option", name="stream_analytics").click()
        set_number(page.get_by_role("spinbutton", name="Paramètre samples"), samples)
        set_number(page.get_by_role("spinbutton", name="Paramètre interval_ms"), 100)
        run_console_call(page)

        items = page.get_by_role("list", name="Éléments reçus").get_by_role("listitem")
        expect(items).to_have_count(samples)
        expect(items.first).to_contain_text("#1")
        expect(items.last).to_contain_text(f"#{samples}")
        expect(page.get_by_role("status").filter(has_text="Flux terminé")).to_contain_text(f"{samples} éléments")
        expect(stat(page, "Éléments reçus")).to_have_text(str(samples))


# --- Sous le capot ---------------------------------------------------------------------

def test_xray_dissects_a_call_on_the_three_protocols(visit: Visit, dashboard: LiveDashboard) -> None:
    pipeline = dashboard.api("GET", "/api/catalog")["pipeline"]
    with visit("xray") as page:
        protocols = page.get_by_role("tablist", name="Protocole disséqué").get_by_role("tab")
        expect(protocols).to_contain_text(["JSON-RPC maison", "gRPC / Protobuf", "REST / JSON"])

        stages = page.get_by_role("button", name=re.compile(r"^Étape \d+ :"))
        expect(stages).to_have_count(12)
        assert [stage.get_attribute("data-stage") for stage in stages.all()] == pipeline

        # Les octets de la requête JSON-RPC : quatre octets de longueur, puis le corps JSON.
        request = page.locator(".hexview").first
        size = int(re.search(r"\d+", request.locator(".hexview__count").inner_text()).group())
        octets = request.locator(".hexview__hex > span").all_inner_texts()
        assert len(octets) == size and all(re.fullmatch(r"[0-9a-f]{2}", octet) for octet in octets)
        assert int("".join(octets[:4]), 16) == size - 4

        compared = page.locator(".xr-proto-card")
        expect(compared).to_contain_text(["JSON-RPC maison", "gRPC / Protobuf", "REST / JSON"])
        for card in compared.all():
            expect(card).to_contain_text("OK")
        expect(page.get_by_text(re.compile(r"Protobuf \(gRPC\) échange \d+"))).to_be_visible()


def test_xray_stepping_changes_the_active_stage(visit: Visit, dashboard: LiveDashboard) -> None:
    catalog = dashboard.api("GET", "/api/catalog")
    pipeline = catalog["pipeline"]
    with visit("xray") as page:
        active = page.locator("[data-stage][aria-current='step']")
        counter = page.get_by_role("group", name="Lecture du voyage")

        page.get_by_role("button", name=re.compile(r"^Étape 1 :")).click()     # arrête la lecture automatique
        expect(active).to_have_attribute("data-stage", pipeline[0])
        page.get_by_role("button", name="Étape suivante").click()
        expect(active).to_have_attribute("data-stage", pipeline[1])
        page.get_by_role("button", name="Étape suivante").click()
        expect(active).to_have_attribute("data-stage", pipeline[2])
        expect(counter).to_contain_text("03 / 12")
        page.get_by_role("button", name="Étape précédente").click()
        expect(active).to_have_attribute("data-stage", pipeline[1])

        page.get_by_role("button", name=re.compile(r"^Étape 7 :")).click()
        expect(active).to_have_attribute("data-stage", pipeline[6])
        expect(page.get_by_role("heading", name=catalog["stages"][pipeline[6]]["label"], exact=True)).to_be_visible()


# --- Benchmark -------------------------------------------------------------------------

def test_benchmark_quick_run_renders_results_and_exports(visit: Visit, dashboard: LiveDashboard, reports_dir: Path) -> None:
    with visit("benchmark") as page:
        # Les plus petits réglages que la page permet : 10 appels, aucun échauffement, balayage sans latence.
        page.get_by_role("radiogroup", name="Préréglage").get_by_role("radio", name="Rapide").click()
        set_number(page.get_by_role("spinbutton", name="Appels chronométrés par protocole"), 10)
        set_number(page.get_by_role("spinbutton", name="Appels d’échauffement non comptés"), 0)
        sweep = page.get_by_role("group", name="Latences du balayage")
        for latency in ("20 ms", "50 ms"):
            sweep.get_by_role("button", name=latency, exact=True).click()

        with page.expect_response(lambda response: response.url.endswith("/api/benchmark/run")) as launch:
            page.get_by_role("button", name="Lancer le benchmark").click()
        sent = launch.value.request.post_data_json
        assert (sent["iterations"], sent["warmup"], sent["sweep_latencies_ms"]) == (10, 0, [0])
        job_id = launch.value.json()["job_id"]
        dashboard.wait_until_idle()
        report_id = dashboard.api("GET", f"/api/jobs/{job_id}")["result"]["id"]
        assert (reports_dir / f"{report_id}.json").is_file()

        report = page.locator("#bench-report")
        expect(report).to_contain_text(report_id, timeout=LONG_WAIT_MS)
        for section in ("#bench-payload", "#bench-latency", "#bench-serialization", "#bench-network"):
            expect(page.locator(section)).to_have_attribute("data-status", "ready")
        expect(page.get_by_role("heading", name="Taille des messages")).to_be_visible()
        expect(page.locator("#bench-payload svg").first).to_be_visible()
        expect(page.locator("#bench-latency")).to_contain_text("10 appels par protocole")
        expect(page.locator("#bench-latency svg").first).to_be_visible()

        with page.expect_download() as exported:
            report.get_by_role("button", name="JSON", exact=True).click()
        assert exported.value.suggested_filename == f"{report_id}.json"
        saved = json.loads(Path(exported.value.path()).read_text(encoding="utf-8"))
        assert saved["id"] == report_id and saved["config"]["iterations"] == 10

        with page.expect_download() as exported:
            report.get_by_role("button", name="CSV", exact=True).click()
        assert exported.value.suggested_filename == f"{report_id}.csv"
        assert Path(exported.value.path()).stat().st_size > 0


# --- Chaos réseau ----------------------------------------------------------------------

def test_chaos_wan_preset_changes_conditions_and_top_bar(visit: Visit, dashboard: LiveDashboard) -> None:
    with visit("chaos") as page:
        presets = page.get_by_role("group", name="Préréglages réseau")
        expect(presets.locator("[data-preset='ideal']")).to_have_attribute("aria-pressed", "true")
        expect(network_chip(page)).to_contain_text("Réseau idéal")

        wan = presets.locator("[data-preset='wan']")
        wan.click()
        expect(wan).to_have_attribute("aria-pressed", "true")
        conditions = dashboard.api("GET", "/api/network")["conditions"]
        assert conditions["preset"] == "wan" and conditions["latency_ms"] > 0
        expect(network_chip(page)).to_contain_text("Internet (WAN)")
        expect(network_chip(page)).to_contain_text(f"+{conditions['latency_ms']:.0f}")
        expect(page.get_by_role("slider", name="Latence aller-retour")).to_have_value(f"{conditions['latency_ms']:.0f}")

        page.get_by_role("button", name="Réinitialiser").click()
        expect(presets.locator("[data-preset='ideal']")).to_have_attribute("aria-pressed", "true")
        expect(network_chip(page)).to_contain_text("Réseau idéal")
        assert dashboard.api("GET", "/api/network")["conditions"]["preset"] == "ideal"


def test_chaos_call_under_outage_shows_an_error(visit: Visit, dashboard: LiveDashboard) -> None:
    with visit("chaos") as page:
        presets = page.get_by_role("group", name="Préréglages réseau")
        outage = presets.locator("[data-preset='outage']")
        outage.click()
        expect(outage).to_have_attribute("aria-pressed", "true")
        assert dashboard.api("GET", "/api/network")["conditions"]["down"] is True
        expect(network_chip(page)).to_have_attribute("data-tone", "danger")

        call = page.get_by_role("button", name="1 appel").first
        outcome = page.locator(".chaos-last")
        call.click()
        expect(outcome).to_contain_text("UNAVAILABLE")
        expect(outcome).to_contain_text("1 tentative")

        page.get_by_role("button", name="Réinitialiser").click()
        expect(presets.locator("[data-preset='ideal']")).to_have_attribute("aria-pressed", "true")
        assert dashboard.api("GET", "/api/network")["conditions"]["preset"] == "ideal"
        call.click()
        expect(outcome).to_contain_text("OK")


def test_chaos_guided_scenario_reaches_its_verdict(visit: Visit, dashboard: LiveDashboard) -> None:
    scenario = next(
        item for item in dashboard.api("GET", "/api/failures/scenarios")["scenarios"] if item["id"] == DUPLICATE_EXECUTION
    )
    with visit("chaos") as page:
        card = page.locator("#chaos-scenarios").get_by_role("article").filter(has_text=scenario["title"])
        expect(card).to_contain_text("Jamais joué")
        card.get_by_role("button", name="Lancer", exact=True).click()
        expect(card).to_contain_text("Joué", timeout=LONG_WAIT_MS)

        result = dashboard.api("GET", "/api/failures/scenarios")["results"][DUPLICATE_EXECUTION]
        metrics = result["metrics"]
        assert metrics["naive_executions"] == 2 and metrics["idempotent_executions"] == 1
        verdict = page.locator(".chaos-verdict")
        expect(verdict).to_contain_text(result["verdict"])
        expect(verdict).to_contain_text("2 exécutions")
        expect(verdict).to_contain_text("À retenir")

        # Le scénario a déréglé le réseau pour provoquer sa panne : il doit l'avoir remis en état.
        network = dashboard.api("GET", "/api/network")
        assert network["conditions"]["preset"] == "ideal"
        assert all(count == 0 for armed in network["armed"].values() for count in armed.values())
        expect(network_chip(page)).to_contain_text("Réseau idéal")
        expect(page.locator("[data-preset='ideal']")).to_have_attribute("aria-pressed", "true")


# --- Contrat & IDL ---------------------------------------------------------------------

def test_contract_diff_renders(visit: Visit, dashboard: LiveDashboard) -> None:
    overview = dashboard.api("GET", "/api/contract")
    with visit("contract") as page:
        diff = page.locator(".diff")
        for version, proto in (("v1", overview["proto_v1"]), ("v2", overview["proto_v2"])):
            expect(diff).to_contain_text(f"{Path(proto['path']).name} · {version}")
        expect(diff.locator(".diff__count--add")).to_have_text(re.compile(r"^\+[1-9]\d*$"))
        expect(diff.locator(".diff__count--remove")).to_have_text(re.compile(r"^−[1-9]\d*$"))
        expect(diff.locator("tr.diff__row").first).to_be_visible()
        expect(diff.get_by_text("BREAKING").first).to_be_visible()
        expect(page.locator(".contract-change")).to_have_count(len(overview["changes"]))
        expect(page.get_by_text(f"{overview['stats']['breaking']} changements cassants", exact=True)).to_be_visible()


def test_contract_run_all_shows_every_outcome(visit: Visit, dashboard: LiveDashboard) -> None:
    with visit("contract") as page:
        scenarios = page.locator("article[data-outcome]")
        expect(scenarios.first).to_be_visible()
        assert all(card.get_attribute("data-outcome") == "" for card in scenarios.all())

        page.get_by_role("button", name="Tout exécuter").first.click()
        expect(page.locator("article[data-outcome='']")).to_have_count(0)

        overview = dashboard.api("GET", "/api/contract")
        observed = {scenario["title"]: overview["results"][scenario["id"]]["outcome"] for scenario in overview["scenarios"]}
        assert len(overview["results"]) == len(overview["scenarios"])
        shown = set()
        for card in scenarios.all():
            outcome = card.get_attribute("data-outcome")
            assert outcome == observed[card.get_by_role("heading").inner_text()]
            expect(card).to_contain_text(OUTCOME_BADGES[outcome], ignore_case=True)
            shown.add(outcome)
        assert shown == set(OUTCOME_BADGES)
        expect(page.locator("article[data-outcome='silent_corruption']").first).to_contain_text(
            "Corruption silencieuse", ignore_case=True
        )
        expect(page.get_by_text(f"{len(overview['scenarios'])} scénarios sur {len(overview['scenarios'])} joués")).to_be_visible()


# --- Transparence ----------------------------------------------------------------------

def test_compare_renders_the_four_snippets(visit: Visit, dashboard: LiveDashboard) -> None:
    snippets = dashboard.api("GET", "/api/code-compare")["snippets"]
    assert len(snippets) == 4
    with visit("compare") as page:
        cards = page.locator(".cmp-variant")
        expect(cards).to_contain_text([snippet["title"] for snippet in snippets])
        for card, snippet in zip(cards.all(), snippets):
            expect(card.locator(".code")).to_contain_text(f"def {snippet['function']}(")
            expect(card).to_contain_text(re.compile(rf"{snippet['lines']}\s*lignes? utiles?"))
            expect(card.locator(".code__line.is-highlighted")).not_to_have_count(0)


def test_compare_run_the_four_gives_identical_results(visit: Visit, dashboard: LiveDashboard) -> None:
    def stock() -> int:
        comparison = dashboard.api("GET", "/api/code-compare")
        products = dashboard.api("GET", "/api/catalog")["products"]
        return next(product["stock"] for product in products if product["id"] == comparison["product_id"])

    before = stock()
    with visit("compare") as page:
        page.get_by_role("button", name="Exécuter les quatre").first.click()
        verdict = page.get_by_text(re.compile(r"4 écritures, un seul résultat"))
        expect(verdict).to_be_visible(timeout=LONG_WAIT_MS)

        table = page.get_by_role("table", name="Résultat, durée et octets de chaque écriture")
        rows = table.locator("tbody").get_by_role("row")
        expect(rows).to_have_count(4)
        results = {row.get_by_role("cell").first.inner_text() for row in rows.all()}
        assert len(results) == 1 and results.pop() == re.search(r"(\d+)\s*$", verdict.inner_text()).group(1)
        expect(table.get_by_role("img", name="Réussi")).to_have_count(4)
    assert stock() == before    # la page rétablit le stock après chaque exécution


# --- Bilan -----------------------------------------------------------------------------

def test_learn_scorecard_fills_with_measured_figures(visit: Visit, dashboard: LiveDashboard) -> None:
    def arguments() -> list[dict[str, Any]]:
        summary = dashboard.api("GET", "/api/summary")
        return summary["advantages"] + summary["drawbacks"]

    with visit("learn") as page:
        status = page.get_by_label("Provenance des chiffres")
        cards = page.locator(".learn-arg[data-state]")
        expect(cards).to_have_count(len(arguments()))
        expect(page.get_by_role("heading", name="Avantages", exact=False).first).to_be_visible()
        expect(page.locator("#learn-matrix").get_by_role("table")).to_be_visible()
        expect(page.locator(".learn-arg[data-state='pending']")).not_to_have_count(0)
        expect(status).not_to_have_attribute("data-state", "measured")

        # La campagne enchaîne le banc d'essai rapide, les scénarios de pannes et de contrat, puis les lots d'appels.
        status.get_by_role("button", name=re.compile("Mesurer maintenant|Compléter les mesures")).click()
        expect(status).to_have_attribute("data-state", "measured", timeout=LONG_WAIT_MS)

        measured = [item for item in arguments() if item["metric"]]
        assert len(measured) > len(arguments()) / 2
        figures = page.locator(".learn-arg[data-state='measured']")
        expect(figures).to_have_count(len(measured))
        expect(page.locator(".learn-arg[data-state='pending']")).to_have_count(0)
        for figure in figures.all():
            expect(figure.locator(".learn-arg__value")).to_have_text(re.compile(r"\d"))
        expect(page.get_by_text(f"{len(measured)} arguments chiffrés sur {len(arguments())}").first).to_be_visible()
        expect(page.locator("#learn-matrix tr.learn-matrix__measured").first).to_contain_text(re.compile(r"\d"))


# --- Mise en page ----------------------------------------------------------------------

@pytest.mark.parametrize("width", [1100, 1440])
def test_no_page_scrolls_horizontally(visit: Visit, dashboard: LiveDashboard, width: int) -> None:
    measure_everything(dashboard)   # pages remplies : graphiques, tableaux et verdicts sont ce qui déborde le plus volontiers
    overflowing = {}
    with visit("overview", width=width) as page:
        for route in ROUTES:
            go_to(page, route)
            expect(page.locator(".page .skeleton:visible")).to_have_count(0)
            page.evaluate("document.fonts.ready.then(() => true)")
            excess = page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
            if excess > 0:
                overflowing[route.id] = excess
    assert not overflowing, f"défilement horizontal à {width} px (dépassement en px) : {overflowing}"
