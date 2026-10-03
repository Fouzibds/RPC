# Suivi du projet — où on en est, et comment reprendre

Fichier de passation écrit le 3 octobre 2026. Il permet de reprendre le travail sur un autre
PC (ou dans une nouvelle session Claude) sans rien perdre. À supprimer une fois le projet fini.

## 1. Reprendre sur un autre PC

1. Copier le dossier `RPC/` **sans** `.venv/` (propre à chaque machine) ni `.scratch/` (jetable).
2. Sur le nouveau PC, installer Python 3.11 ou plus récent, puis dans le dossier :

   ```
   python -m venv .venv
   .venv\Scripts\python.exe -m pip install -r requirements.txt
   .venv\Scripts\python.exe -m pip install playwright      (facultatif : tests de l'interface)
   .venv\Scripts\python.exe -m rpc_grpc.generate
   .venv\Scripts\python.exe -m pytest
   ```

3. Ouvrir Claude Code dans ce dossier et lui écrire :

   > Lis SUIVI.md et implementation_plan.md, puis termine les tâches de la section
   > « Reste à faire » de SUIVI.md, dans l'ordre. Vérifie chaque étape avant de passer à la suivante.

La conversation d'origine reste sur l'ancien PC : tout ce qu'il faut savoir est dans ce fichier
et dans `implementation_plan.md` (architecture et contrats d'interfaces).

## 2. Ce qui est terminé et vérifié

| Brique | État au 3 octobre |
|---|---|
| `common/` (config, erreurs, télémétrie, service métier, interface cliente) | terminé |
| `rpc_grpc/protos/` v1 et v2 + code généré | terminé |
| `rpc_custom/` — RPC maison (JSON-RPC 2.0 sur sockets, stub, squelette) | terminé, testé |
| `rpc_grpc/` — serveur, client, intercepteurs, décodeur Protobuf, serveur v2 | terminé, testé |
| `rest_api/` — API REST de comparaison | terminé, testé |
| `netsim/` — proxy de chaos, conditions réseau, retries, disjoncteur | terminé, testé |
| `lab/runtime.py` — orchestrateur | terminé, testé |
| `benchmark_lab/` — banc d'essai, pannes, contrat, transparence, rapports | terminé, testé |
| `dashboard/` — API HTTP + WebSocket (`server.py`, `calls.py`, `hub.py`, `jobs.py`…) | écrit, tests présents |
| `main.py` + `cli/` — interface console Rich | écrit, tests présents |
| `dashboard/static/` — coquille, design system, composants, 8 pages | écrit, vérifié visuellement |

Vérifications faites à la main ce jour-là :

- `pytest` sur les modules réseau + runtime : 505 tests verts ; sur `benchmark_lab` : 188 verts.
- `python main.py --benchmark --quick --no-save` : sortie correcte, avec les deux tableaux exigés
  par le sujet (taille du paquet JSON vs Protobuf ; temps moyen par appel local / RPC maison / gRPC / REST).
- `python main.py --simulate-failures` : 5 scénarios en 8 s, verdicts corrects.
- `python main.py --contract` et `--call update_stock SKU-1001 -3 --protocol grpc --inspect` : code de sortie 0.
- Dashboard : les 8 pages se chargent sans erreur console ni défilement horizontal.

Non revérifié après les derniers changements : la suite `pytest` complète (tous fichiers
ensemble), `tests/test_dashboard_api.py` et `tests/test_cli.py`.

## 3. Ce qui était en cours au moment de la coupure

Trois agents travaillaient encore. S'ils ont été interrompus, leur travail peut être partiel :
tout relire avant de s'y fier.

1. **Intégration + README** — devait lancer toute la suite de tests, exercer chaque option du
   CLI et chaque route de l'API, corriger ce qui casse, puis écrire `README.md` (absent pour l'instant).
2. **Composants partagés du dashboard** — corrections dans `dashboard/static/js/components`,
   `js/core`, `css/` (hors `css/pages/`) :
   - le bouton en chargement affiche l'icône à côté du spinner ;
   - le compteur « SERVEURS » de la barre latérale affiche 5/5 au lieu de 3/3 ;
   - icônes manquantes dans `js/core/icons.js` : `smartphone`, `circle-off`, `power-off`, `printer` ;
   - le graphique en ligne « en direct » laisse déborder une graduation à droite ;
   - les onglets débordent quand ils sont trop nombreux.
3. **Cohérence entre pages + tests de l'interface** — liens croisés (`#/xray?protocol=…`,
   `#/xray?call=…`, `#/chaos?scenario=…`), en-têtes de page harmonisés, vocabulaire unique, et
   création de `tests/test_frontend_smoke.py` (Playwright + Edge, ignoré si absent).

## 4. Reste à faire, dans l'ordre

1. Lancer `.venv\Scripts\python.exe -m pytest` et corriger tout ce qui échoue.
2. Vérifier que les points de la section 3 sont réellement faits ; finir ceux qui ne le sont pas.
3. Écrire `README.md` s'il manque : présentation, installation, toutes les options de `main.py`,
   architecture, correspondance avec les 4 phases du sujet, comment lancer les tests.
4. Page « Sous le capot » : la première inspection affiche ~13 ms pour le JSON-RPC maison parce
   qu'elle paie l'ouverture de la connexion. Faire un appel d'échauffement (non tracé) avant
   l'appel inspecté, dans `dashboard/calls.py`, pour que la comparaison des trois protocoles soit juste.
5. Vider `reports/` : il contient une quinzaine de rapports `benchmark-*.json` produits par les
   essais. Garder `.gitkeep`, puis générer un seul rapport propre avec `python main.py --benchmark`.
6. Supprimer `.scratch/` (scripts et captures jetables) et ce fichier `SUIVI.md`.
7. Parcours final : `python main.py --dashboard`, ouvrir les 8 pages en thème sombre et clair.

## 5. Commandes utiles

```
python main.py                       menu interactif
python main.py --benchmark           banc d'essai complet
python main.py --simulate-failures   démonstration des pannes
python main.py --contract            ruptures de contrat
python main.py --demo                visite guidée « sous le capot »
python main.py --dashboard           dashboard web (http://127.0.0.1:8000)
```

Si un port est déjà pris : définir la variable d'environnement `RPCX_PORT_OFFSET` (par exemple
`100`) pour décaler tous les ports du laboratoire.
