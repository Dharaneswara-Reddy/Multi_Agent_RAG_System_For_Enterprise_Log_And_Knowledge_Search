"""Expanded Meridian corpus: 200 generated documents over 42 services.

The original 18 hand-written documents remain the canonical answers for the
original golden-set questions. This expansion exists to make retrieval face a
realistic amount of plausible-but-wrong neighbouring content, so the measured
retrieval numbers reflect a genuine ranking problem rather than a corpus small
enough that any reasonable embedding wins.
"""

from __future__ import annotations

from aiops.ingestion.expansion.render import all_services, render_all
from aiops.ingestion.expansion.specs import Decision, Fault, Guide, Incident, Service

__all__ = [
    "Decision",
    "Fault",
    "Guide",
    "Incident",
    "Service",
    "all_services",
    "render_all",
]
