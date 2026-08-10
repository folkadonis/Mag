"""
LLM editorial pass for the Mag digest.

Exactly one Claude call is made per run (with one retry on failure). The
model sees stories as a numbered JSON list and may only refer to them by
integer id — it never sees or invents URLs. `hydrate()` splices the real
URL/source/metadata back in afterwards and silently drops any id the model
invented that doesn't correspond to a real story. If both attempts fail,
`run_editor` returns None and the caller should use `fallback_digest`.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger("mag.editor")

SYSTEM_PROMPT = """\
You are the editor of "Mag", a daily digest for a production ML/software \
engineer who already knows the fundamentals and wants a clear picture of \
where AI, the open-source model ecosystem, and the broader tech industry \
are moving — new model releases, research papers, infra/engineering \
postmortems from big tech, security incidents, and local/self-hosted \
inference developments. Do not explain what an LLM, transformer, or \
benchmark is. Do not use hype language ("game-changing", "revolutionary", \
"the future of AI"). Be specific and terse.

You will receive a JSON array of candidate stories, each with an integer \
"id". Organize the ones worth the reader's time into sections. Invent \
section names that reflect what is actually in today's stories (e.g. a \
name tied to a real theme you see) — never use generic filler names like \
"News" or "Updates" and do not reuse the same fixed set of section names \
every day. It is correct, and often the right call, to publish fewer \
stories than you were given: drop anything derivative, low-signal, or not \
worth this reader's attention. Publishing 8 sharp items beats publishing \
20 mediocre ones.

Rules:
- Refer to stories ONLY by their integer "id" field. Never write out a URL \
  or invent one.
- Do not invent stories or ids that were not given to you.
- Each blurb is 1-2 sentences: specific, concrete, no fluff, no throat \
  clearing ("In this post, the authors...").
- Respond with ONLY valid JSON, no markdown fences, no commentary, \
  matching exactly this shape:
  {"sections": [{"name": "<section name>", "items": [{"id": <int>, \
  "blurb": "<1-2 sentences>"}]}]}
"""

_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


def build_considered(ranked_items: list[dict[str, Any]], max_n: int) -> list[dict[str, Any]]:
    return ranked_items[:max_n]


def build_payload(considered: list[dict[str, Any]]) -> list[dict[str, Any]]:
    payload = []
    for item in considered:
        payload.append(
            {
                "id": item["id"],
                "title": item["title"],
                "source": item.get("source", ""),
                "type": item.get("type", ""),
                "summary": (item.get("summary") or "")[:400],
                "points": item.get("points", 0),
                "also_seen_count": len(item.get("also_seen") or []),
            }
        )
    return payload


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = _JSON_BLOCK_RE.search(text)
    if match:
        return json.loads(match.group(0))
    raise ValueError("No JSON object found in editor response")


def _validate_shape(parsed: dict[str, Any]) -> None:
    if not isinstance(parsed, dict) or "sections" not in parsed:
        raise ValueError("Editor response missing 'sections'")
    sections = parsed["sections"]
    if not isinstance(sections, list):
        raise ValueError("'sections' must be a list")
    for section in sections:
        if not isinstance(section, dict) or "name" not in section or "items" not in section:
            raise ValueError("Malformed section in editor response")
        if not isinstance(section["items"], list):
            raise ValueError("Section 'items' must be a list")


def call_claude(payload: list[dict[str, Any]], config: dict[str, Any]) -> str:
    import anthropic

    editor_cfg = config.get("editor", {})
    client = anthropic.Anthropic()
    response = client.messages.create(
        model=editor_cfg.get("model", "claude-sonnet-5"),
        max_tokens=int(editor_cfg.get("max_tokens", 4000)),
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": json.dumps(payload)}],
    )
    parts = [block.text for block in response.content if getattr(block, "type", None) == "text"]
    return "\n".join(parts)


def run_editor(
    ranked_items: list[dict[str, Any]], config: dict[str, Any]
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Attempt the editor call up to twice. Returns (parsed_or_None, considered_items)."""
    max_n = int(config.get("editor", {}).get("max_stories_considered", 60))
    considered = build_considered(ranked_items, max_n)
    if not considered:
        return None, considered

    payload = build_payload(considered)

    last_exc: Exception | None = None
    for attempt in (1, 2):
        try:
            text = call_claude(payload, config)
            parsed = _extract_json(text)
            _validate_shape(parsed)
            return parsed, considered
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            logger.warning("Editor call attempt %d failed: %s", attempt, exc)

    logger.error("Editor call failed twice, falling back to unedited digest: %s", last_exc)
    return None, considered


def hydrate(edited: dict[str, Any], considered: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Splice real story data back into the model's id-only sections,
    dropping any id that doesn't match a real considered story."""
    by_id = {item["id"]: item for item in considered}
    sections: list[dict[str, Any]] = []

    for section in edited.get("sections", []):
        name = str(section.get("name") or "Stories").strip()
        out_items = []
        for raw_item in section.get("items", []):
            if not isinstance(raw_item, dict):
                continue
            sid = raw_item.get("id")
            if not isinstance(sid, int) or sid not in by_id:
                logger.warning("Dropping story with invalid/unknown id: %r", sid)
                continue
            story = by_id[sid]
            out_items.append({**story, "blurb": raw_item.get("blurb") or story.get("summary") or ""})
        if out_items:
            sections.append({"name": name, "items": out_items})

    return sections


_FALLBACK_SECTION_NAMES = {
    "paper": "Research Papers",
    "blog": "Industry & Labs",
    "hn": "Hacker News Buzz",
}


def fallback_digest(considered: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Unedited digest grouped by story type, used when the LLM call fails
    twice or when running with --no-llm."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in considered:
        groups.setdefault(item.get("type", "blog"), []).append(item)

    sections = []
    for item_type in ("paper", "blog", "hn"):
        stories = groups.get(item_type)
        if not stories:
            continue
        items = [{**it, "blurb": it.get("summary") or it["title"]} for it in stories]
        sections.append({"name": _FALLBACK_SECTION_NAMES.get(item_type, item_type.title()), "items": items})

    # Any remaining, unexpected types still get published rather than dropped.
    for item_type, stories in groups.items():
        if item_type in _FALLBACK_SECTION_NAMES or not stories:
            continue
        items = [{**it, "blurb": it.get("summary") or it["title"]} for it in stories]
        sections.append({"name": item_type.title(), "items": items})

    return sections
