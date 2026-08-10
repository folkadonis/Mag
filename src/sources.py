"""
Parallel story collection for the Mag digest.

Every fetcher below is self-contained and swallows its own errors: a dead
RSS feed, a timed-out API, or a malformed response never blocks the other
sources or crashes the run. Fetchers return plain dicts (no `id` yet);
`fetch_all` runs them concurrently and assigns stable integer ids to the
combined result.
"""

from __future__ import annotations

import calendar
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any

import feedparser
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger("mag.sources")

# Several publishers (Uber, X, some CDNs) reject unrecognised bot agents
# with 403/406, so present as a normal browser while still identifying the
# project in the comment field.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36 (compatible; Mag-AI-Digest/2.0; "
    "+https://github.com/folkadonis/Mag)"
)
HTTP_TIMEOUT = 30
DEFAULT_LOOKBACK_HOURS = 48
DEFAULT_MAX_ITEMS_PER_FEED = 40


def _session() -> requests.Session:
    """A requests session that retries transient failures with backoff.
    arXiv in particular is slow and intermittently drops connections."""
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=1.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "*/*"})
    return session


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _struct_time_to_dt(struct_time) -> datetime | None:
    if not struct_time:
        return None
    try:
        return datetime.fromtimestamp(calendar.timegm(struct_time), tz=timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def _clean_summary(raw: str | None, limit: int = 400) -> str:
    if not raw:
        return ""
    import re

    text = re.sub(r"<[^>]+>", " ", raw)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def fetch_rss(feed_cfg: dict[str, Any], lookback_hours: float = DEFAULT_LOOKBACK_HOURS) -> list[dict[str, Any]]:
    """Fetch and parse a single RSS/Atom feed, keeping only recent entries.

    Many publishers (OpenAI, Hugging Face) serve their entire archive back
    to 2015 in one feed, so an unfiltered fetch floods the ranker with
    thousands of stale stories. Entries older than the lookback window are
    dropped here; undated entries are kept but score poorly on recency.
    Never raises.
    """
    name = feed_cfg.get("name", "unknown")
    url = feed_cfg.get("url")
    weight = float(feed_cfg.get("weight", 1.0))
    item_type = feed_cfg.get("type", "blog")
    lookback = float(feed_cfg.get("lookback_hours", lookback_hours))
    max_items = int(feed_cfg.get("max_items", DEFAULT_MAX_ITEMS_PER_FEED))

    cutoff = _utcnow() - timedelta(hours=lookback)
    items: list[dict[str, Any]] = []
    stale = 0
    try:
        resp = _session().get(url, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        parsed = feedparser.parse(resp.content)
        for entry in parsed.entries:
            published = _struct_time_to_dt(
                getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
            )
            if published is not None and published < cutoff:
                stale += 1
                continue
            link = entry.get("link")
            title = entry.get("title")
            if not link or not title:
                continue
            summary = _clean_summary(entry.get("summary") or entry.get("description"))
            items.append(
                {
                    "title": title.strip(),
                    "url": link,
                    "source": name,
                    "type": item_type,
                    "published": published,
                    "points": 0,
                    "summary": summary,
                    "weight": weight,
                }
            )
    except Exception as exc:  # noqa: BLE001 - a dead feed must never block the run
        logger.warning("RSS fetch failed for %s (%s): %s", name, url, exc)
        return []

    if stale:
        logger.debug("%s: dropped %d entries older than %.0fh", name, stale, lookback)

    # Newest first, then cap: a feed that publishes 50 posts a day must not
    # crowd out every other source.
    items.sort(key=lambda it: it["published"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return items[:max_items]


def fetch_arxiv(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Fetch recent papers from the arXiv API across configured categories."""
    base_url = cfg.get("base_url", "http://export.arxiv.org/api/query")
    categories = cfg.get("categories", ["cs.CL", "cs.AI", "cs.LG"])
    max_results = int(cfg.get("max_results", 60))
    lookback_hours = float(cfg.get("lookback_hours", 30))
    weight = float(cfg.get("weight", 1.0))
    item_type = cfg.get("type", "paper")
    # arXiv is routinely the slowest source in the run; give it more room.
    timeout = int(cfg.get("timeout_seconds", 60))

    cat_query = " OR ".join(f"cat:{c}" for c in categories)
    params = {
        "search_query": cat_query,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
        "max_results": max_results,
    }

    cutoff = _utcnow() - timedelta(hours=lookback_hours)
    items: list[dict[str, Any]] = []
    try:
        resp = _session().get(base_url, params=params, timeout=timeout)
        resp.raise_for_status()
        parsed = feedparser.parse(resp.content)
        for entry in parsed.entries:
            published = _struct_time_to_dt(getattr(entry, "published_parsed", None))
            if published and published < cutoff:
                continue
            link = entry.get("link")
            title = entry.get("title")
            if not link or not title:
                continue
            arxiv_categories = [t.get("term") for t in entry.get("tags", []) if t.get("term")]
            summary = _clean_summary(entry.get("summary"))
            items.append(
                {
                    "title": " ".join(title.split()),
                    "url": link,
                    "source": f"arXiv ({', '.join(arxiv_categories[:2]) or 'cs'})",
                    "type": item_type,
                    "published": published,
                    "points": 0,
                    "summary": summary,
                    "weight": weight,
                }
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("arXiv fetch failed: %s", exc)
        return []

    return items


def fetch_hn(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Fetch recent high-scoring Hacker News stories via the Algolia API."""
    base_url = cfg.get("base_url", "https://hn.algolia.com/api/v1/search_by_date")
    min_points = int(cfg.get("min_points", 80))
    lookback_hours = float(cfg.get("lookback_hours", 30))
    weight = float(cfg.get("weight", 1.0))
    item_type = cfg.get("type", "hn")
    timeout = int(cfg.get("timeout_seconds", HTTP_TIMEOUT))

    cutoff_ts = int((_utcnow() - timedelta(hours=lookback_hours)).timestamp())
    params = {
        "tags": "story",
        "numericFilters": f"points>={min_points},created_at_i>={cutoff_ts}",
        "hitsPerPage": 100,
    }

    items: list[dict[str, Any]] = []
    try:
        resp = _session().get(base_url, params=params, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        for hit in data.get("hits", []):
            title = hit.get("title")
            url = hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID')}"
            if not title:
                continue
            published = None
            if hit.get("created_at_i"):
                published = datetime.fromtimestamp(hit["created_at_i"], tz=timezone.utc)
            items.append(
                {
                    "title": title.strip(),
                    "url": url,
                    "source": "Hacker News",
                    "type": item_type,
                    "published": published,
                    "points": int(hit.get("points") or 0),
                    "summary": "",
                    "weight": weight,
                }
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("HN fetch failed: %s", exc)
        return []

    return items


def fetch_hf_daily_papers(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Fetch today's Hugging Face daily papers."""
    base_url = cfg.get("base_url", "https://huggingface.co/api/daily_papers")
    weight = float(cfg.get("weight", 1.0))
    item_type = cfg.get("type", "paper")

    items: list[dict[str, Any]] = []
    try:
        resp = _session().get(base_url, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        for entry in data:
            paper = entry.get("paper", entry)
            paper_id = paper.get("id")
            title = paper.get("title")
            if not title or not paper_id:
                continue
            published = None
            pub_raw = entry.get("publishedAt") or paper.get("publishedAt")
            if pub_raw:
                try:
                    published = datetime.fromisoformat(pub_raw.replace("Z", "+00:00"))
                except ValueError:
                    published = None
            items.append(
                {
                    "title": " ".join(title.split()),
                    "url": f"https://huggingface.co/papers/{paper_id}",
                    "source": "HF Daily Papers",
                    "type": item_type,
                    "published": published,
                    "points": int(paper.get("upvotes") or 0),
                    "summary": _clean_summary(paper.get("summary")),
                    "weight": weight,
                }
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("HF daily papers fetch failed: %s", exc)
        return []

    return items


def fetch_all(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Run every configured fetcher in parallel and assign stable ids."""
    sources_cfg = config.get("sources", {})
    lookback_hours = float(sources_cfg.get("lookback_hours", DEFAULT_LOOKBACK_HOURS))
    jobs: list[tuple[str, Any, tuple]] = []

    for feed_cfg in sources_cfg.get("rss", []):
        jobs.append((f"rss:{feed_cfg.get('name')}", fetch_rss, (feed_cfg, lookback_hours)))

    if "arxiv" in sources_cfg:
        jobs.append(("arxiv", fetch_arxiv, (sources_cfg["arxiv"],)))

    if "hn" in sources_cfg:
        jobs.append(("hn", fetch_hn, (sources_cfg["hn"],)))

    if "hf_daily_papers" in sources_cfg:
        jobs.append(("hf_daily_papers", fetch_hf_daily_papers, (sources_cfg["hf_daily_papers"],)))

    all_items: list[dict[str, Any]] = []
    started = time.time()
    with ThreadPoolExecutor(max_workers=max(len(jobs), 1)) as pool:
        future_to_name = {pool.submit(fn, *args): name for name, fn, args in jobs}
        for future in as_completed(future_to_name):
            name = future_to_name[future]
            try:
                result = future.result()
                logger.info("%s: %d items", name, len(result))
                all_items.extend(result)
            except Exception as exc:  # noqa: BLE001 - belt and suspenders
                logger.warning("Fetcher %s raised unexpectedly: %s", name, exc)

    logger.info("Fetched %d raw items from %d sources in %.1fs", len(all_items), len(jobs), time.time() - started)

    for idx, item in enumerate(all_items):
        item["id"] = idx

    return all_items
