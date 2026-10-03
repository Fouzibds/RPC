"""Préparation des sorties standard : UTF-8 partout, séquences ANSI sur la console Windows.

Module sans dépendance : ``main.py`` l'appelle avant tout affichage — y compris
l'aide d'``argparse``, dont les accents doivent survivre à une redirection.
"""
from __future__ import annotations

import sys

_STD_OUTPUT_HANDLE = -11
_ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004


def prepare_stdio() -> None:
    """Force l'UTF-8 sur stdout et stderr, et active les séquences ANSI de la console Windows.

    Redirigée vers un fichier ou un tube, la sortie d'un programme Python sous
    Windows est encodée en cp1252 : accents, filets et glyphes y seraient perdus.
    """
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    if sys.platform == "win32":
        _enable_virtual_terminal()


def _enable_virtual_terminal() -> None:
    """Demande à la console Windows d'interpréter les séquences ANSI (couleurs 24 bits, filets arrondis).

    Sans cela, Rich retombe sur l'ancienne API console : 16 couleurs et cadres à
    angles droits. Sans effet si la sortie n'est pas une console.
    """
    import ctypes

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.GetStdHandle(_STD_OUTPUT_HANDLE)
    mode = ctypes.c_uint32()
    if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
        kernel32.SetConsoleMode(handle, mode.value | _ENABLE_VIRTUAL_TERMINAL_PROCESSING)
