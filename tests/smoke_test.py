"""
Offline smoke test for the Mag pipeline. No network access is used —
everything runs against in-memory fixtures. Covers three load-bearing
guarantees:

1. Jaccard title dedupe actually merges near-duplicate stories and credits
   the loser to the winner's `also_seen`.
2. The topic-keyword filter drops off-topic non-paper items but exempts
   papers from the filter entirely.
3. Real story URLs survive all the way from raw item -> ranked -> fallback
   digest -> rendered HTML (i.e. hydrate/render never invents or drops a
   link for a story that should be published).
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import editor, rank, render  # noqa: E402


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
