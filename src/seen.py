"""
Cross-run memory of already-published stories.

The fetch window has to be wide — arXiv skips weekends, and most corporate
engineering blogs publish weekly rather than daily, so a 48-hour window
silently drops the labs entirely. A wide window on its own would republish
the same story every morning until it aged out, so this module records what
has already gone out and filters it from later issues.

Two fingerprints are stored per story: the URL, and a hash of its
significant title words. The second catches the case where tomorrow a
different outlet covers the same story under its own URL.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.rank import tokenize_title

logger = logging.getLogger("mag.seen")

_EMPTY: dict[str, Any] = {"urls": {}, "titles": {}}


def title_fingerprint(title: str) -> str:
    """A stable hash of a title's significant words, order-independent, so
    'Anthropic ships Claude 5' and 'Claude 5 ships from Anthropic' collide."""
    tokens = sorted(tokenize_title(title or ""))
    if not tokens:
        return ""
    return hashlib.sha1(" ".join(tokens).encode("utf-8")).hexdigest()[:16]


def load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"urls": {}, "titles": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("seen store is not an object")
        return {"urls": dict(data.get("urls") or {}), "titles": dict(data.get("titles") or {})}
    except Exception as exc:  # noqa: BLE001 - a corrupt store must not kill the run
        logger.warning("Could not read seen store at %s (%s); starting fresh", path, exc)
        return {"urls": {}, "titles": {}}


def prune(store: dict[str, Any], retention_days: int) -> dict[str, Any]:
    """Drop entries older than the retention window so the file stays small
    and a story can legitimately resurface after a long absence."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)

    def _keep(bucket: dict[str, str]) -> dict[str, str]:
        kept = {}
        for key, stamp in bucket.items():
            try:
                when = datetime.fromisoformat(stamp)
            except (TypeError, ValueError):
                continue
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            if when >= cutoff:
                kept[key] = stamp
        return kept

    return {"urls": _keep(store.get("urls", {})), "titles": _keep(store.get("titles", {}))}


def is_seen(item: dict[str, Any], store: dict[str, Any]) -> bool:
    if item.get("url") in store.get("urls", {}):
        return True
    fingerprint = title_fingerprint(item.get("title", ""))
    return bool(fingerprint) and fingerprint in store.get("titles", {})


def filter_unseen(items: list[dict[str, Any]], store: dict[str, Any]) -> list[dict[str, Any]]:
    fresh = [item for item in items if not is_seen(item, store)]
    dropped = len(items) - len(fresh)
    if dropped:
        logger.info("Skipped %d stories already published in an earlier issue", dropped)
    return fresh


def record(sections: list[dict[str, Any]], store: dict[str, Any], path: Path) -> None:
    """Remember every story that actually made it into today's issue.

    Only published stories are recorded — a story that was considered but
    cut is free to come back tomorrow.
    """
    stamp = datetime.now(timezone.utc).isoformat()
    urls = store.setdefault("urls", {})
    titles = store.setdefault("titles", {})

    count = 0
    for section in sections:
        for item in section.get("items", []):
            url = item.get("url")
            if url:
                urls.setdefault(url, stamp)
            fingerprint = title_fingerprint(item.get("original_title") or item.get("title", ""))
            if fingerprint:
                titles.setdefault(fingerprint, stamp)
            # Sibling coverage of the same story counts as published too.
            for also in item.get("also_seen") or []:
                if also.get("url"):
                    urls.setdefault(also["url"], stamp)
            count += 1

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(store, indent=2, sort_keys=True), encoding="utf-8")
    logger.info("Recorded %d published stories in %s", count, path)
