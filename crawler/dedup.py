"""Soft dedup (§6.3).

url_hash catches the same page twice. This catches the same *bando* published on
two portals — a ministry and a region both announcing one scheme — where the URLs
differ and the titles nearly match.

Flagged duplicates are LINKED, not dropped. Each portal's page may carry detail
the other lacks, and choosing the canonical one is a human call, not ours.
"""

from __future__ import annotations

import logging
import re
from datetime import date

from rapidfuzz import fuzz

log = logging.getLogger(__name__)

# §6.3 names ~90. High enough that "Bando Innovazione 2026" and "Bando
# Innovazione 2025" stay distinct, low enough to survive punctuation and an
# issuer prefix.
SIMILARITY_THRESHOLD = 90

# Noise that differs between portals announcing the same scheme.
_NOISE = re.compile(
    r"\b(bando|avviso|pubblico|contributi?|contributo|finanziamento|"
    r"voucher|fondo|misura|anno|edizione|20\d{2})\b",
    re.I,
)


def normalise_title(title: str) -> str:
    """Strip boilerplate so two portals' phrasings converge."""
    text = title.lower()
    text = _NOISE.sub(" ", text)
    text = re.sub(r"[^\w\s]", " ", text)
    return " ".join(text.split())


def _coerce_date(value: object) -> date | None:
    """Accept a date or an ISO string.

    Deadlines arrive from two directions: Stage B parses them into `date`, while
    the dedup pool comes back from Supabase as JSON, where a date is a string.
    Comparing the two blew up on real data even though every unit test passed —
    the fixtures used `date` on both sides, so the seam was never exercised.
    """
    if value is None or isinstance(value, date):
        return value if isinstance(value, date) else None
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _same_deadline_month(a: object, b: object) -> bool:
    """§6.3 scopes comparison to the same deadline month.

    Two grants with no deadline are not evidence of duplication — half the
    archive is 'a sportello' — so a missing deadline never matches.
    """
    left, right = _coerce_date(a), _coerce_date(b)
    if left is None or right is None:
        return False
    return (left.year, left.month) == (right.year, right.month)


def find_duplicate(
    candidate: dict,
    existing: list[dict],
    *,
    threshold: int = SIMILARITY_THRESHOLD,
) -> tuple[str | None, float]:
    """Best probable duplicate of `candidate` among `existing`.

    Returns (grant_id, score) or (None, 0.0). Compares only within the same
    deadline month, and never against the same source: one portal listing a
    bando twice is its own problem, not a cross-portal duplicate.
    """
    candidate_title = normalise_title(candidate.get("title") or "")
    if not candidate_title:
        return None, 0.0

    candidate_deadline = candidate.get("deadline")
    best_id: str | None = None
    best_score = 0.0

    for other in existing:
        if other.get("source_id") and other["source_id"] == candidate.get("source_id"):
            continue

        if not _same_deadline_month(candidate_deadline, other.get("deadline")):
            continue

        other_title = normalise_title(other.get("title") or "")
        if not other_title:
            continue

        score = fuzz.token_sort_ratio(candidate_title, other_title)
        if score >= threshold and score > best_score:
            best_score = score
            best_id = other["id"]

    if best_id:
        log.info("probable cross-portal duplicate (score %.0f)", best_score)

    return best_id, best_score / 100.0
