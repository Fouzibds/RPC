"""Interface console du laboratoire (Rich).

* ``stdio``             — sorties standard en UTF-8, séquences ANSI de la console Windows ;
* ``theme``             — langage visuel commun : palette, glyphes, console, briques d'affichage ;
* ``wire``              — « sous le capot » : pipeline d'un appel, octets bruts, décodage Protobuf ;
* ``calls``             — arguments de la ligne de commande → appel → résultat affiché ;
* ``benchmark_view``    — progression et tableaux du banc d'essai ;
* ``failures_view``     — chronologie et verdicts du laboratoire de pannes ;
* ``contract_view``     — diff des contrats et scénarios de rupture ;
* ``transparency_view`` — les quatre écritures du même appel, côte à côte ;
* ``status_view``       — serveurs, proxys de chaos, trafic ;
* ``tour``              — visite guidée du même appel à travers les trois middlewares ;
* ``interactive``       — menu interactif ;
* ``cli_runner``        — une fonction ``run_*`` par mode de ``main.py``.

Aucun module de ce paquet ne mesure ni ne simule quoi que ce soit : tout vient de
``lab``, ``benchmark_lab`` et des traces du bus. Le CLI ne fait que montrer.
"""
