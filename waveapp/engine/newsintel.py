"""News intelligence (2026-09-01): category + direction per headline.

Research basis (Boudoukh et al., RFS 2019): identified-news days carry >2×
volatility and CATEGORY carries skew — deals/partnerships up, legal down,
earnings direction follows the result words. Wave classifies with keyword
rules (fast, transparent, no model risk) and journals the tags; the Brain
learns the weights from outcomes. This is comprehension the honest way:
rules tag, ML decides what the tags are worth.
"""

from __future__ import annotations

import re

_CATEGORIES: list[tuple[str, re.Pattern]] = [
    (
        "earnings",
        re.compile(r"\b(earnings|eps|revenue|quarter|q[1-4]\b|guidance|outlook|forecast)", re.I),
    ),  # noqa: E501
    ("fda", re.compile(r"\b(fda|phase\s*[123]|trial|approval|clearance|pdufa|drug)", re.I)),
    (
        "deal",
        re.compile(
            r"\b(merger|acquisition|acquire[sd]?|buyout|takeover|deal|partnership|contract|agreement)",
            re.I,
        ),
    ),  # noqa: E501
    (
        "analyst",
        re.compile(
            r"\b(upgrade[sd]?|downgrade[sd]?|price target|initiat(es|ed)"
            r"|overweight|underweight|buy rating|sell rating)",
            re.I,
        ),
    ),  # noqa: E501
    (
        "legal",
        re.compile(
            r"\b(lawsuit|probe|investigation|sec charges|fraud|subpoena|settle(s|ment)|recall)",
            re.I,
        ),
    ),  # noqa: E501
    (
        "macro",
        re.compile(
            r"\b(fed|rates?|inflation|tariff|opec|oil price|geopolit|war|attack|iran|china trade)",
            re.I,
        ),
    ),  # noqa: E501
]

_UP = re.compile(
    r"\b(surge[sd]?|soar(s|ed)?|jump(s|ed)?|rall(y|ies|ied)|beat(s)?\b|tops?\b|record|"
    r"upgrade[sd]?|raises?\b|boost(s|ed)?|wins?\b|approv(al|ed|es)|strong|higher|gains?\b|climbs?)",
    re.I,
)
_DOWN = re.compile(
    r"\b(plunge[sd]?|crater(s|ed)?|sink(s|ing)?|tumble[sd]?|miss(es|ed)?\b|cuts?\b|"
    r"downgrade[sd]?|falls?\b|drops?\b|slump(s|ed)?|weak|lower(s|ed)?\b|warns?\b|halts?\b|"
    r"recall|lawsuit|probe|investigation|slides?\b|crash(es|ed)?)",
    re.I,
)


def classify(headline: str) -> tuple[str, int]:
    """(category, direction) — direction +1 / −1 / 0 from the language.
    Both signals are FEATURES for the journal and the ticker, never a
    standalone trade trigger (hard rule 8: rules decide direction)."""
    text = headline or ""
    category = "other"
    for name, pattern in _CATEGORIES:
        if pattern.search(text):
            category = name
            break
    ups = len(_UP.findall(text))
    downs = len(_DOWN.findall(text))
    direction = 1 if ups > downs else (-1 if downs > ups else 0)
    return category, direction


# -- 4.3 novelty + relevance (Ke/Kelly/Xiu: fresh news ≈ ×1.7 impact) -------

_WORD = re.compile(r"[a-z0-9$%]+")


def _shingles(headline: str) -> frozenset:
    words = _WORD.findall((headline or "").lower())
    if len(words) < 3:
        return frozenset(words)
    return frozenset(tuple(words[i : i + 3]) for i in range(len(words) - 2))


class NoveltyTracker:
    """Per-symbol 24h headline dedupe (blueprint 4.3). A first print scores
    1.0; each near-duplicate (3-word-shingle Jaccard ≥ 0.5) decays the score
    — a re-hashed PR from 3 hours ago must NOT read as fresh. Pure memory,
    no model, deterministic."""

    WINDOW = 24 * 3600.0
    SIMILAR = 0.5

    def __init__(self) -> None:
        self._seen: dict[str, list[tuple[float, frozenset]]] = {}

    def assess(self, symbol: str, headline: str, now_ts: float) -> float:
        """Novelty in (0, 1]: 1.0 first print, 1/(1+repeats) after."""
        shingles = _shingles(headline)
        history = self._seen.setdefault(symbol, [])
        # prune the 24h window
        cutoff = now_ts - self.WINDOW
        if history and history[0][0] < cutoff:
            history[:] = [(ts, s) for ts, s in history if ts >= cutoff]
        repeats = 0
        for _ts, old in history:
            union = len(shingles | old)
            if union and len(shingles & old) / union >= self.SIMILAR:
                repeats += 1
        history.append((now_ts, shingles))
        if len(history) > 200:  # runaway-feed guard
            del history[:100]
        return 1.0 / (1.0 + repeats)


def relevance(symbol: str, headline: str) -> int:
    """1 when the ticker is the SUBJECT (named in the headline), 0 when the
    story only tags it (body mention / peer list)."""
    if not symbol or not headline:
        return 0
    pattern = rf"(?<![A-Z]){re.escape(symbol.upper())}(?![A-Z])"
    return 1 if re.search(pattern, headline.upper()) else 0
