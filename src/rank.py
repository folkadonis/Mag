"""
Filtering, deduplication, and scoring for the Mag digest.

Pipeline order: topic-keyword filter (papers exempt) -> Jaccard title
dedupe -> scoring -> sort descending by score.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Any

STOPWORDS = {
    "a", "an", "the", "and", "or", "but", "of", "to", "in", "on", "for",
    "with", "at", "by", "from", "is", "are", "was", "were", "be", "been",
    "being", "as", "it", "its", "this", "that", "these", "those", "you",
    "your", "we", "our", "how", "why", "what", "new", "vs", "into", "over",
    "up", "down", "out", "about", "using", "via", "not", "no", "can",
    "will", "just", "now", "than", "after", "before", "when", "which",
}

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def tokenize_title(title: str) -> set[str]:
    """Lowercase, strip punctuation, and drop stopwords/short tokens."""
    tokens = _TOKEN_RE.findall(title.lower())
    return {t for t in tokens if t not in STOPWORDS and len(t) > 1}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 0.0
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def topic_filter(items: list[dict[str, Any]], keywords: list[str]) -> list[dict[str, Any]]:
    """Keep items matching a topic keyword; papers are exempt from the filter."""
    lowered_keywords = [k.lower() for k in keywords]
    kept = []
    for item in items:
        if item.get("type") == "paper":
            kept.append(item)
            continue
        haystack = f"{item.get('title', '')} {item.get('summary', '')}".lower()
        if any(kw in haystack for kw in lowered_keywords):
            kept.append(item)
    return kept


def dedupe(items: list[dict[str, Any]], threshold: float) -> list[dict[str, Any]]:
    """Cluster near-duplicate titles; the highest-weight member represents
    the cluster and the rest become an `also_seen` credit on it."""
    ordered = sorted(
        items,
        key=lambda it: (-float(it.get("weight", 0.0)), it.get("id", 0)),
    )
    token_cache = {it["id"]: tokenize_title(it["title"]) for it in ordered}
    consumed: set[int] = set()
    result: list[dict[str, Any]] = []

    for item in ordered:
        if item["id"] in consumed:
            continue
        consumed.add(item["id"])
        rep = dict(item)
        rep["also_seen"] = []
        rep_tokens = token_cache[item["id"]]

        for other in ordered:
            if other["id"] in consumed:
                continue
            if jaccard(rep_tokens, token_cache[other["id"]]) >= threshold:
                consumed.add(other["id"])
                rep["also_seen"].append(
                    {"title": other["title"], "source": other["source"], "url": other["url"]}
                )

        result.append(rep)

    return result


def recency_decay(published: datetime | None, now: datetime, half_life_hours: float) -> float:
    """Exponential decay: 1.0 when brand new, 0.5 at half_life_hours old."""
    if published is None:
        return 0.0
    age_hours = (now - published).total_seconds() / 3600.0
    age_hours = max(age_hours, 0.0)
    return 0.5 ** (age_hours / half_life_hours)


def score_item(item: dict[str, Any], now: datetime, half_life_hours: float) -> float:
    weight = float(item.get("weight", 1.0))
    recency = recency_decay(item.get("published"), now, half_life_hours)
    points = int(item.get("points") or 0)
    also_seen_n = len(item.get("also_seen") or [])
    return weight * (
        1
        + 1.2 * recency
        + math.log1p(points) / 6
        + math.log1p(also_seen_n) / 2
    )


def rank(items: list[dict[str, Any]], config: dict[str, Any]) -> list[dict[str, Any]]:
    """Run the full filter -> dedupe -> score -> sort pipeline."""
    keywords = config.get("topics", {}).get("keywords", [])
    scoring_cfg = config.get("scoring", {})
    threshold = float(scoring_cfg.get("dedupe_jaccard_threshold", 0.5))
    half_life_hours = float(scoring_cfg.get("recency_half_life_hours", 24))

    filtered = topic_filter(items, keywords)
    deduped = dedupe(filtered, threshold)

    now = _utcnow()
    for item in deduped:
        item["score"] = score_item(item, now, half_life_hours)

    ranked = sorted(deduped, key=lambda it: it["score"], reverse=True)
    for position, item in enumerate(ranked, start=1):
        item["rank_position"] = position

    return ranked
