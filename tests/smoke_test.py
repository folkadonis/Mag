"""
Offline smoke test for the Mag pipeline. No network access is used —
everything runs against in-memory fixtures. Covers the load-bearing
guarantees:

1. Jaccard title dedupe actually merges near-duplicate stories and credits
   the loser to the winner's `also_seen`.
2. The topic-keyword filter drops off-topic non-paper items but exempts
   papers from the filter entirely.
3. Real story URLs survive all the way from raw item -> ranked -> fallback
   digest -> rendered HTML (i.e. hydrate/render never invents or drops a
   link for a story that should be published).
4. The RSS lookback cutoff and per-feed cap keep archive-dumping feeds
   (OpenAI, Hugging Face) from flooding the ranker with years-old posts.
5. The seen store stops a story published in one issue from reappearing in
   the next, including when a different outlet reruns it under its own URL.
6. `hydrate` publishes only ids the model was actually given, and the
   plain-language fields (headline, why-it-matters, glossary) reach both
   the HTML and plain-text renderings.

Run with `pytest tests/smoke_test.py` or directly with `python
tests/smoke_test.py`.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import editor, rank, render, seen, sources  # noqa: E402


def make_item(
    item_id: int,
    title: str,
    url: str,
    source: str,
    item_type: str,
    published: datetime,
    points: int = 0,
    weight: float = 1.0,
    summary: str = "",
) -> dict:
    return {
        "id": item_id,
        "title": title,
        "url": url,
        "source": source,
        "type": item_type,
        "published": published,
        "points": points,
        "summary": summary,
        "weight": weight,
    }


def test_dedupe_merges_near_duplicate_titles_and_credits_also_seen():
    now = datetime.now(timezone.utc)
    items = [
        make_item(
            0,
            "OpenAI releases new GPT-5 model with better reasoning",
            "https://openai.com/blog/gpt-5",
            "OpenAI",
            "blog",
            now,
            weight=1.4,
        ),
        make_item(
            1,
            "OpenAI Releases New GPT-5 Model With Better Reasoning",
            "https://techcrunch.com/gpt-5-launch",
            "TechCrunch AI",
            "blog",
            now,
            weight=0.8,
        ),
        make_item(
            2,
            "A completely unrelated story about container shipping rates",
            "https://example.com/shipping",
            "TechCrunch AI",
            "blog",
            now,
            weight=0.8,
        ),
    ]

    deduped = rank.dedupe(items, threshold=0.5)

    assert len(deduped) == 2, f"expected the two GPT-5 stories to merge, got {len(deduped)} groups"

    rep = next(d for d in deduped if "openai.com" in d["url"])
    assert rep["source"] == "OpenAI"  # higher-weight member wins
    assert len(rep["also_seen"]) == 1
    assert rep["also_seen"][0]["source"] == "TechCrunch AI"

    unrelated = next(d for d in deduped if d["id"] == 2)
    assert unrelated["also_seen"] == []


def test_topic_filter_drops_offtopic_blog_but_exempts_papers():
    now = datetime.now(timezone.utc)
    items = [
        make_item(
            0,
            "Quarterly earnings report beats analyst expectations",
            "https://example.com/earnings",
            "TechCrunch AI",
            "blog",
            now,
        ),
        make_item(
            1,
            "A Survey of Results in Combinatorial Geometry",
            "https://arxiv.org/abs/0000.0000",
            "arXiv (cs.CL)",
            "paper",
            now,
            summary="A pure mathematics survey with no bearing on AI whatsoever.",
        ),
        make_item(
            2,
            "Anthropic launches new Claude agent capabilities",
            "https://anthropic.com/news/agents",
            "Anthropic",
            "blog",
            now,
        ),
    ]
    keywords = ["ai", "llm", "transformer", "machine learning", "claude", "agent"]

    filtered = rank.topic_filter(items, keywords)
    kept_ids = {item["id"] for item in filtered}

    assert 0 not in kept_ids, "off-topic blog post should be filtered out"
    assert 1 in kept_ids, "papers must be exempt from the topic filter even without a keyword match"
    assert 2 in kept_ids, "on-topic blog post should survive the filter"


def test_real_urls_survive_into_rendered_html():
    config = {
        "topics": {"keywords": ["ai", "llm", "transformer", "machine learning", "claude"]},
        "scoring": {"dedupe_jaccard_threshold": 0.5, "recency_half_life_hours": 24},
        "editor": {"max_stories_considered": 10},
    }
    now = datetime.now(timezone.utc)
    items = [
        make_item(
            0,
            "Anthropic ships new Claude model with better tool use",
            "https://anthropic.com/news/claude-update",
            "Anthropic",
            "blog",
            now,
            weight=1.5,
        ),
        make_item(
            1,
            "Efficient Attention Mechanisms for Long-Context LLMs",
            "https://arxiv.org/abs/9999.99999",
            "arXiv (cs.CL)",
            "paper",
            now,
            weight=1.0,
            summary="A paper about attention and long-context language models.",
        ),
    ]

    ranked = rank.rank(items, config)
    assert len(ranked) == 2, "both on-topic, non-duplicate stories should survive ranking"

    considered = editor.build_considered(ranked, max_n=10)
    sections = editor.fallback_digest(considered)

    total_stories = sum(len(section["items"]) for section in sections)
    meta = {
        "date": "Test Day",
        "subject": "Mag — Test Day",
        "total_stories": total_stories,
        "sources_count": 2,
        "footer_note": "smoke test",
    }

    html = render.render_html(sections, meta)

    assert "https://anthropic.com/news/claude-update" in html
    assert "https://arxiv.org/abs/9999.99999" in html

    text = render.render_text(sections, meta)
    assert "https://anthropic.com/news/claude-update" in text
    assert "https://arxiv.org/abs/9999.99999" in text


def test_rss_lookback_drops_archive_entries_but_keeps_undated_ones():
    """OpenAI and Hugging Face serve their whole archive in one feed. The
    lookback cutoff is the only thing standing between the ranker and a
    story from 2015."""
    now = datetime.now(timezone.utc)
    feed = f"""<?xml version="1.0"?>
    <rss version="2.0"><channel>
      <item><title>Fresh story about AI</title><link>https://example.com/fresh</link>
        <pubDate>{now.strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate></item>
      <item><title>Ancient story about AI</title><link>https://example.com/ancient</link>
        <pubDate>Fri, 11 Dec 2015 10:00:00 +0000</pubDate></item>
      <item><title>Undated story about AI</title><link>https://example.com/undated</link></item>
    </channel></rss>"""

    class _Resp:
        content = feed.encode("utf-8")

        def raise_for_status(self):
            return None

    class _Session:
        def get(self, *args, **kwargs):
            return _Resp()

    original = sources._session
    sources._session = lambda: _Session()
    try:
        items = sources.fetch_rss({"name": "Test", "url": "https://example.com/feed"}, lookback_hours=48)
    finally:
        sources._session = original

    urls = {item["url"] for item in items}
    assert "https://example.com/fresh" in urls, "a story from today must survive the cutoff"
    assert "https://example.com/ancient" not in urls, "a 2015 story must be dropped by the cutoff"
    assert "https://example.com/undated" in urls, "undated entries are kept; recency scoring handles them"


def test_rss_max_items_caps_a_firehose_feed():
    now = datetime.now(timezone.utc)
    entries = "".join(
        f"<item><title>Story {i} about AI</title><link>https://example.com/{i}</link>"
        f"<pubDate>{now.strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate></item>"
        for i in range(50)
    )
    feed = f'<?xml version="1.0"?><rss version="2.0"><channel>{entries}</channel></rss>'

    class _Resp:
        content = feed.encode("utf-8")

        def raise_for_status(self):
            return None

    original = sources._session
    sources._session = lambda: type("S", (), {"get": lambda self, *a, **k: _Resp()})()
    try:
        items = sources.fetch_rss({"name": "Test", "url": "https://x/feed", "max_items": 5})
    finally:
        sources._session = original

    assert len(items) == 5, f"max_items should cap the feed at 5, got {len(items)}"


def test_seen_store_blocks_republishing_across_runs():
    """A story published yesterday must not reappear today, even when a
    different outlet covers it under a different URL."""
    import tempfile

    now = datetime.now(timezone.utc)
    published = make_item(
        0, "Anthropic ships Claude with better tool use",
        "https://anthropic.com/news/x", "Anthropic", "blog", now,
    )
    sections = [{"name": "S", "intro": "", "items": [{**published, "original_title": published["title"]}]}]

    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "seen.json"
        store = seen.load(path)
        assert store == {"urls": {}, "titles": {}}, "a missing store must start empty, not crash"

        seen.record(sections, store, path)
        reloaded = seen.load(path)

        same_story = make_item(
            1, "Anthropic ships Claude with better tool use",
            "https://anthropic.com/news/x", "Anthropic", "blog", now,
        )
        reworded = make_item(
            2, "Claude ships from Anthropic with tool use better",
            "https://techcrunch.com/totally-different-url", "TechCrunch AI", "blog", now,
        )
        unrelated = make_item(
            3, "Mistral releases a new open weights model",
            "https://mistral.ai/news/y", "Mistral AI", "blog", now,
        )

        remaining = seen.filter_unseen([same_story, reworded, unrelated], reloaded)
        remaining_ids = {item["id"] for item in remaining}

        assert 1 not in remaining_ids, "the same URL must be filtered on a later run"
        assert 2 not in remaining_ids, "the same story reworded elsewhere must also be filtered"
        assert 3 in remaining_ids, "an unrelated story must still get through"


def test_seen_store_prunes_entries_past_retention():
    old = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    recent = datetime.now(timezone.utc).isoformat()
    store = {"urls": {"https://old": old, "https://recent": recent}, "titles": {}}

    pruned = seen.prune(store, retention_days=30)

    assert "https://old" not in pruned["urls"], "entries past the retention window must be dropped"
    assert "https://recent" in pruned["urls"], "recent entries must be retained"


def test_editor_hydrate_keeps_plain_headline_and_real_url():
    """The model only ever sees integer ids, so hydrate is what guarantees a
    published story points at a real URL and keeps its real title visible."""
    now = datetime.now(timezone.utc)
    considered = [
        make_item(4, "Sparse Mixture-of-Experts Routing at Scale",
                  "https://arxiv.org/abs/1234.5678", "arXiv (cs.LG)", "paper", now),
    ]
    edited = {
        "overview": "Today in AI.",
        "sections": [
            {
                "name": "Model Plumbing",
                "intro": "How models are built.",
                "items": [
                    {"id": 4, "headline": "A cheaper way to run giant AI models",
                     "blurb": "Explains it plainly.", "why_it_matters": "Cheaper inference."},
                    {"id": 999, "headline": "Invented", "blurb": "Hallucinated story", "why_it_matters": "x"},
                ],
            }
        ],
        "glossary": [{"term": "Inference", "definition": "Running a trained model."}],
    }

    sections = editor.hydrate(edited, considered)

    assert len(sections) == 1
    items = sections[0]["items"]
    assert len(items) == 1, "the invented id 999 must be dropped, not published"
    assert items[0]["url"] == "https://arxiv.org/abs/1234.5678"
    assert items[0]["headline"] == "A cheaper way to run giant AI models"
    assert items[0]["original_title"] == "Sparse Mixture-of-Experts Routing at Scale"
    assert items[0]["why_it_matters"] == "Cheaper inference."
    assert sections[0]["intro"] == "How models are built."

    extras = editor.extract_extras(edited)
    assert extras["overview"] == "Today in AI."
    assert extras["glossary"] == [{"term": "Inference", "definition": "Running a trained model."}]


def test_plain_language_fields_reach_both_renderings():
    now = datetime.now(timezone.utc)
    sections = [
        {
            "name": "Section",
            "intro": "What this group is about.",
            "items": [
                {
                    **make_item(0, "Original Jargon Title", "https://example.com/story",
                                "Source", "blog", now),
                    "headline": "A plain English headline",
                    "original_title": "Original Jargon Title",
                    "blurb": "Three sentences explaining it simply.",
                    "why_it_matters": "This affects real people.",
                    "rank_position": 1,
                }
            ],
        }
    ]
    meta = {
        "date": "Test Day", "subject": "Mag", "total_stories": 1, "sources_count": 1,
        "overview": "The day in brief.",
        "glossary": [{"term": "Model", "definition": "A trained program."}],
        "footer_note": "smoke test",
    }

    html = render.render_html(sections, meta)
    for expected in ("The day in brief.", "What this group is about.", "A plain English headline",
                     "This affects real people.", "Words used in this issue", "A trained program."):
        assert expected in html, f"missing from HTML rendering: {expected!r}"
    assert "Original Jargon Title" in html, "the publisher's real title must stay visible"

    text = render.render_text(sections, meta)
    for expected in ("The day in brief.", "A plain English headline", "WHY IT MATTERS",
                     "WORDS USED IN THIS ISSUE", "https://example.com/story"):
        assert expected in text, f"missing from text rendering: {expected!r}"


def _run_all() -> int:
    tests = [obj for name, obj in globals().items() if name.startswith("test_") and callable(obj)]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"OK   {test.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {test.__name__}: {exc}")
    if failures:
        print(f"\n{failures}/{len(tests)} tests failed")
    else:
        print(f"\nAll {len(tests)} smoke tests passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
