"""Outil de développement : capture d'écran headless du dashboard (Playwright).

    python tools/shot.py http://127.0.0.1:8000/#/benchmark out.png --do "click:#run" --do "wait:1500"

Affiche aussi les erreurs console / exceptions JS / requêtes en échec de la page,
ce qui en fait un test de fumée du frontend. Nécessite ``pip install playwright``.

Actions ``--do`` (exécutées dans l'ordre, avant la capture) :
    click:<sélecteur>      wait:<ms>          fill:<sélecteur>=<valeur>
    press:<touche>         hover:<sélecteur>  eval:<javascript>
    waitfor:<sélecteur>    scroll:<sélecteur> text:<sélecteur>  (affiche le texte de l'élément)
"""
from __future__ import annotations

import argparse
import sys

from playwright.sync_api import Browser, Error as PlaywrightError, Playwright, sync_playwright


def launch(p: Playwright) -> Browser:
    """Microsoft Edge s'il est installé (Windows), sinon le Chromium fourni avec Playwright."""
    try:
        return p.chromium.launch(channel="msedge", headless=True)
    except PlaywrightError:
        return p.chromium.launch(headless=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("url")
    parser.add_argument("out")
    parser.add_argument("--width", type=int, default=1440)
    parser.add_argument("--height", type=int, default=900)
    parser.add_argument("--full", action="store_true", help="capture la page entière (défilement compris)")
    parser.add_argument("--wait", type=int, default=900, help="attente après chargement (ms)")
    parser.add_argument("--theme", choices=("dark", "light"), default=None)
    parser.add_argument("--selector", default=None, help="ne capture que cet élément")
    parser.add_argument("--scale", type=float, default=1.0, help="device scale factor")
    parser.add_argument("--do", action="append", default=[], help="action à exécuter avant la capture")
    args = parser.parse_args()

    problems: list[str] = []
    with sync_playwright() as p:
        browser = launch(p)
        context = browser.new_context(
            viewport={"width": args.width, "height": args.height},
            device_scale_factor=args.scale,
            color_scheme=args.theme or "dark",
        )
        page = context.new_page()
        page.on("console", lambda m: problems.append(f"console.{m.type}: {m.text}") if m.type in ("error", "warning") else None)
        page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
        page.on("requestfailed", lambda r: problems.append(f"requestfailed: {r.method} {r.url} — {r.failure}"))
        page.on("response", lambda r: problems.append(f"http {r.status}: {r.url}") if r.status >= 400 else None)

        if args.theme:
            page.add_init_script(f"try {{ localStorage.setItem('rpcx.theme', '{args.theme}'); }} catch (e) {{}}")
        page.goto(args.url, wait_until="load")
        page.wait_for_timeout(args.wait)

        for action in args.do:
            kind, _, arg = action.partition(":")
            if kind == "click":
                page.click(arg, timeout=5000)
            elif kind == "wait":
                page.wait_for_timeout(int(arg))
            elif kind == "fill":
                selector, _, value = arg.partition("=")
                page.fill(selector, value, timeout=5000)
            elif kind == "press":
                page.keyboard.press(arg)
            elif kind == "hover":
                page.hover(arg, timeout=5000)
            elif kind == "eval":
                print("eval ->", page.evaluate(arg))
            elif kind == "waitfor":
                page.wait_for_selector(arg, timeout=15000)
            elif kind == "scroll":
                page.locator(arg).first.scroll_into_view_if_needed(timeout=5000)
            elif kind == "text":
                print("text ->", page.locator(arg).first.inner_text(timeout=5000))
            else:
                print(f"action inconnue : {action}", file=sys.stderr)
            page.wait_for_timeout(120)

        if args.selector:
            page.locator(args.selector).first.screenshot(path=args.out)
        else:
            page.screenshot(path=args.out, full_page=args.full)
        browser.close()

    print(f"capture : {args.out}")
    if problems:
        print(f"{len(problems)} problème(s) détecté(s) dans la page :")
        for line in problems[:40]:
            print("  -", line)
    else:
        print("aucune erreur console / réseau")
    return 0


if __name__ == "__main__":
    sys.exit(main())
