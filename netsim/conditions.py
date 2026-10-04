"""Conditions réseau simulées, partagées par les proxys de chaos.

Un seul objet ``NetworkConditions`` est branché sur les trois proxys (JSON-RPC maison, gRPC,
REST) : modifier un curseur soumet donc les trois protocoles exactement au même réseau, ce
qui rend leurs comportements comparables.

Les proxys relisent ces conditions à chaque morceau d'octets : un changement prend effet
immédiatement, sans rien redémarrer.
"""
from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from typing import Any

CUSTOM_PRESET = "custom"

_BOOLEAN_FIELDS: frozenset[str] = frozenset({"blackhole", "down"})
_PROBABILITY_FIELDS: frozenset[str] = frozenset({"spike_probability", "reset_probability"})

# Réseau parfait : le point de départ de tous les préréglages.
_IDEAL: dict[str, Any] = {
    "latency_ms": 0.0,
    "jitter_ms": 0.0,
    "spike_probability": 0.0,
    "spike_ms": 0.0,
    "reset_probability": 0.0,
    "blackhole": False,
    "down": False,
    "bandwidth_kbps": 0.0,
}

# Champs réglables, dans l'ordre du tableau du plan (``preset`` est une étiquette, pas un réglage).
FIELD_NAMES: tuple[str, ...] = tuple(_IDEAL)


def _preset(**overrides: Any) -> dict[str, Any]:
    return {**_IDEAL, **overrides}


PRESETS: dict[str, dict[str, Any]] = {
    "ideal": _preset(),
    "lan": _preset(latency_ms=1.0, jitter_ms=0.4),
    "wan": _preset(latency_ms=40.0, jitter_ms=8.0),
    "mobile_3g": _preset(
        latency_ms=200.0, jitter_ms=60.0, spike_probability=0.05, spike_ms=800.0, reset_probability=0.01
    ),
    "satellite": _preset(latency_ms=600.0, jitter_ms=40.0),
    "flaky": _preset(
        latency_ms=80.0, jitter_ms=30.0, spike_probability=0.15, spike_ms=1500.0, reset_probability=0.08
    ),
    "blackhole": _preset(blackhole=True),
    "outage": _preset(down=True),
}

# Libellé, phrase d'explication et nom d'icône (indicatif) affichés par le CLI et le dashboard.
PRESET_INFO: dict[str, dict[str, str]] = {
    "ideal": {
        "label": "Boucle locale idéale",
        "description": "Aucune perturbation : le proxy relaie les octets tels quels, sans délai ajouté.",
        "icon": "zap",
    },
    "lan": {
        "label": "Réseau local",
        "description": "Deux machines du même bâtiment : environ 1 ms d’aller-retour, à peine perceptible.",
        "icon": "network",
    },
    "wan": {
        "label": "Internet (WAN)",
        "description": "Un serveur dans une autre région : 40 ms d’aller-retour et une gigue modérée.",
        "icon": "globe",
    },
    "mobile_3g": {
        "label": "Mobile 3G",
        "description": "Lien cellulaire : 200 ms d’aller-retour, des pics de 800 ms sur 5 % des requêtes "
                       "et 1 % de coupures.",
        "icon": "smartphone",
    },
    "satellite": {
        "label": "Liaison satellite",
        "description": "Orbite géostationnaire : 600 ms d’aller-retour, stables mais incompressibles.",
        "icon": "satellite",
    },
    "flaky": {
        "label": "Réseau instable",
        "description": "Lien dégradé : latence erratique, pics de 1,5 s sur 15 % des requêtes "
                       "et 8 % de coupures.",
        "icon": "activity",
    },
    "blackhole": {
        "label": "Trou noir",
        "description": "Les octets partent mais rien ne revient : seule une échéance (timeout) libère l’appelant.",
        "icon": "circle-off",
    },
    "outage": {
        "label": "Panne du serveur",
        "description": "Serveur injoignable : chaque connexion est coupée dès son ouverture.",
        "icon": "power-off",
    },
}


def _validated(name: str, value: Any) -> Any:
    """Contrôle le type et la plage d'un champ ; renvoie la valeur normalisée."""
    if name in _BOOLEAN_FIELDS:
        if not isinstance(value, bool):
            raise ValueError(f"« {name} » doit être un booléen (reçu : {value!r})")
        return value
    # bool est un sous-type d'int : « latency_ms=True » serait accepté sans ce garde-fou.
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"« {name} » doit être un nombre fini (reçu : {value!r})")
    number = float(value)
    if number < 0:
        raise ValueError(f"« {name} » doit être ≥ 0 (reçu : {value!r})")
    if name in _PROBABILITY_FIELDS and number > 1:
        raise ValueError(f"« {name} » est une probabilité, attendue entre 0 et 1 (reçu : {value!r})")
    return number


def _require_preset(name: Any) -> str:
    if name not in PRESETS:
        raise ValueError(f"Préréglage inconnu : {name!r} (disponibles : {', '.join(PRESETS)})")
    return name


@dataclass
class NetworkConditions:
    """État du réseau simulé. Toutes les méthodes sont sûres vis-à-vis des threads."""

    latency_ms: float = 0.0          # latence ALLER-RETOUR ajoutée (½ par sens)
    jitter_ms: float = 0.0           # variation aléatoire ± sur l'aller-retour
    spike_probability: float = 0.0   # probabilité qu'une requête subisse un pic
    spike_ms: float = 0.0            # durée du pic
    reset_probability: float = 0.0   # probabilité de couper la connexion à l'arrivée d'une requête
    blackhole: bool = False          # les octets sont avalés, aucune réponse (→ timeout)
    down: bool = False               # serveur injoignable : connexions coupées immédiatement
    bandwidth_kbps: float = 0.0      # débit maximal par sens (0 = illimité)
    preset: str = "ideal"            # préréglage actif, « custom » après un réglage manuel

    def __post_init__(self) -> None:
        # Attribut ordinaire et non champ de dataclass : asdict() et replace() restent utilisables.
        self._lock = threading.Lock()
        values = {name: _validated(name, getattr(self, name)) for name in FIELD_NAMES}
        if self.preset != CUSTOM_PRESET:
            _require_preset(self.preset)
        self._assign(values, self.preset if PRESETS.get(self.preset) == values else CUSTOM_PRESET)

    def update(self, **fields: Any) -> dict[str, Any]:
        """Modifie un ou plusieurs champs, tous validés avant d'en appliquer un seul.

        Le préréglage devient « custom ». Si ``preset`` figure parmi les champs, il sert de
        base aux autres valeurs : ``update(**instantané)`` restaure ainsi fidèlement un état
        sauvegardé par ``snapshot()``, étiquette comprise.
        """
        base = fields.pop("preset", None)
        if base is not None and base != CUSTOM_PRESET:
            _require_preset(base)
        unknown = sorted(set(fields) - set(FIELD_NAMES))
        if unknown:
            raise ValueError(f"Champ(s) inconnu(s) : {', '.join(unknown)} (attendus : {', '.join(FIELD_NAMES)})")
        changes = {name: _validated(name, value) for name, value in fields.items()}
        with self._lock:
            if base in PRESETS:
                values = {**PRESETS[base], **changes}
                self._assign(values, base if values == PRESETS[base] else CUSTOM_PRESET)
            elif changes:
                self._assign(changes, CUSTOM_PRESET)
            return self._values()

    def apply_preset(self, name: str) -> dict[str, Any]:
        """Remplace tous les champs par ceux du préréglage ``name``."""
        values = PRESETS[_require_preset(name)]
        with self._lock:
            self._assign(values, name)
            return self._values()

    def reset(self) -> dict[str, Any]:
        """Retour au réseau parfait."""
        return self.apply_preset("ideal")

    def snapshot(self) -> dict[str, Any]:
        """Photographie cohérente de tous les champs (jamais un état à moitié modifié)."""
        with self._lock:
            return self._values()

    def __reduce__(self) -> tuple[Any, ...]:
        # Un verrou ne se copie pas : copy(), deepcopy() et pickle repartent de l'instantané.
        return type(self), tuple(self.snapshot().values())

    # -- interne (verrou tenu par l'appelant) ---------------------------------

    def _assign(self, values: dict[str, Any], preset: str) -> None:
        for name, value in values.items():
            setattr(self, name, value)
        self.preset = preset

    def _values(self) -> dict[str, Any]:
        values = {name: getattr(self, name) for name in FIELD_NAMES}
        values["preset"] = self.preset
        return values


def presets_dict() -> list[dict[str, Any]]:
    """Préréglages prêts à sérialiser pour l'API : ``[{id, label, description, icon, conditions}]``."""
    return [
        {"id": name, **PRESET_INFO[name], "conditions": dict(values)}
        for name, values in PRESETS.items()
    ]
