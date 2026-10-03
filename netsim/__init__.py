"""Réseau simulé et résilience : ce qui sépare un appel distant d'un appel local.

* ``conditions``  — l'état du réseau (latence, gigue, pics, coupures, trou noir, panne) ;
* ``chaos_proxy`` — le proxy TCP qui applique ces conditions à de vraies connexions ;
* ``resilience``  — les parades côté client : échéance, retries, disjoncteur, idempotence.
"""
from .chaos_proxy import ARM_KINDS, ChaosProxy
from .conditions import CUSTOM_PRESET, PRESET_INFO, PRESETS, NetworkConditions, presets_dict
from .resilience import CircuitBreaker, ResilientClient, RetryPolicy

__all__ = [
    "ARM_KINDS",
    "CUSTOM_PRESET",
    "PRESETS",
    "PRESET_INFO",
    "ChaosProxy",
    "CircuitBreaker",
    "NetworkConditions",
    "ResilientClient",
    "RetryPolicy",
    "presets_dict",
]
