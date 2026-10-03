"""Instrument classification (2026-08-18, directive).

The RVOL rankers structurally love leveraged single-stock/index products —
they print huge relative volume on any underlying move. They're allowed in
the universe (§7) but their CONCENTRATION is capped: both the live scanner
and the research selection admit at most a fraction of leveraged products
per ranking (default 25%).

Classification is name-based: leveraged/inverse funds are legally required
to say so in the fund name (2X, 3X, Ultra, Bull/Bear, Daily ... Leveraged).
Unknown/blank names classify as NOT leveraged — the cap never blocks an
ordinary stock by accident.
"""

from __future__ import annotations

import re

_LEVERAGED_RE = re.compile(
    r"(\b[123](\.5)?x\b|\bultra(pro)?\b|\bbull\b|\bbear\b|\bleveraged\b"
    r"|\binverse\b|\bdaily\b.*\b(long|short)\b|-[123]x\b)",
    re.IGNORECASE,
)


def is_leveraged_name(name: str | None) -> bool:
    """True when a fund NAME identifies a leveraged/inverse product."""
    if not name:
        return False
    return bool(_LEVERAGED_RE.search(name))


def leveraged_cap(top_n: int, fraction: float = 0.25) -> int:
    """How many leveraged products a top-N ranking may admit (at least 1 —
    a genuinely exceptional mover is never banned outright)."""
    return max(1, int(top_n * fraction))
