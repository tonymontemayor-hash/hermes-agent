"""Deterministic per-turn reasoning router for Sofia.

No LLM call is made here. The function only classifies the current user
request before the main inference begins.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any


def _normalize(value: Any) -> str:
    text = value if isinstance(value, str) else str(value or "")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", text.lower()).strip()


def _cfg(effort: str) -> dict:
    if effort == "off":
        return {"enabled": False}
    return {"enabled": True, "effort": effort}


def classify_reasoning(
    message: Any,
    *,
    has_images: bool = False,
    profile: str = "private",
) -> tuple[dict, str]:
    """Return (reasoning_config, explanation_tag).

    Private Sofia policy:
      OFF    trivial/direct requests
      LOW    lightweight reasoning
      MEDIUM multi-signal / history / design
      XHIGH  debugging / root-cause / high-complexity changes
    """

    text = _normalize(message)
    if not text:
        return _cfg("off"), "empty"

    # Direct commands should stay instantaneous unless they contain evidence
    # that this is actually a technical/configuration task.
    direct = re.search(
        r"\b("
        r"enciende|apaga|abre|cierra|activa|desactiva|"
        r"sube|baja|pon|ajusta|cambia|"
        r"turn on|turn off|open|close|set"
        r")\b",
        text,
    )

    risky_or_complex_object = re.search(
        r"\b("
        r"automatizacion|automation|script|dashboard|yaml|config|"
        r"servidor|server|docker|systemd|mcp|firewall|red|network|"
        r"base de datos|database"
        r")\b",
        text,
    )

    if direct and not risky_or_complex_object and len(text.split()) <= 20:
        return _cfg("off"), "direct-command"

    score = 0
    reasons: list[str] = []

    def add(pattern: str, points: int, tag: str) -> None:
        nonlocal score
        if re.search(pattern, text):
            score += points
            reasons.append(tag)

    # Lightweight reasoning.
    add(
        r"\b(por que|porque podria|explica|interpret[ae]|que significa|"
        r"why|explain|interpret)\b",
        1,
        "explain",
    )

    # Analysis across multiple pieces of evidence.
    add(
        r"\b(analiza|analisis|compara|comparar|correlaciona|correlacion|"
        r"tendencia|trend|compare|correlate)\b",
        2,
        "analysis",
    )
    add(
        r"\b(historial|historico|history|estadistica|statistics|"
        r"varias entidades|multiples entidades|multiple entities)\b",
        2,
        "history/multi-signal",
    )

    # Design/change work deserves deliberate reasoning.
    add(
        r"\b(crea|crear|diseña|disena|modifica|corrige|reestructura|"
        r"implementa|create|design|modify|fix|refactor|implement)\b.*"
        r"\b(automatizacion|automation|script|dashboard|yaml|config)\b",
        2,
        "configuration-change",
    )

    # Root-cause / debugging work should normally go straight to XHIGH.
    add(
        r"\b("
        r"diagnostica|diagnostico|debug|debugging|causa raiz|root cause|"
        r"falla intermitente|intermitente|no funciona|dejo de funcionar|"
        r"exception|traceback|stack trace"
        r")\b",
        3,
        "debugging",
    )

    add(
        r"\b(log|logs|traza|trazas|trace|traces)\b",
        2,
        "logs/traces",
    )

    add(
        r"\b(arquitectura|architecture|mcp|migracion|migration|"
        r"seguridad|security)\b",
        3,
        "architecture",
    )

    add(
        r"\b(verifica|verificar|valida|validar|investiga|investigar|"
        r"verify|validate|investigate)\b",
        1,
        "verification",
    )

    # Large pasted material usually deserves at least one extra level.
    raw = message if isinstance(message, str) else str(message or "")
    if len(raw) >= 1000 or "```" in raw:
        score += 1
        reasons.append("large-input")

    if has_images:
        score += 1
        reasons.append("image")

    if score >= 4:
        effort = "xhigh"
    elif score >= 2:
        effort = "medium"
    elif score >= 1:
        effort = "low"
    else:
        effort = "off"

    return _cfg(effort), f"{effort}:score={score}:{','.join(reasons) or 'simple'}"
