"""
LLM editorial pass for the Mag digest.

Two interchangeable backends produce the same JSON:

* ``cli``  — shells out to the Claude Code CLI (``claude -p``), which
  authenticates with a Claude Pro/Max **subscription**. No API key and no
  pay-as-you-go API credit is required.
* ``api``  — the classic ``anthropic`` SDK path, needs ``ANTHROPIC_API_KEY``.

``auto`` (the default) prefers the CLI when the ``claude`` binary is on
PATH, and otherwise falls back to the API key.

Exactly one model call is made per run (with one retry on failure). The
model sees stories as a numbered JSON list and may only refer to them by
integer id — it never sees or invents URLs. `hydrate()` splices the real
URL/source/metadata back in afterwards and silently drops any id the model
invented that doesn't correspond to a real story. If both attempts fail,
`run_editor` returns None and the caller should use `fallback_digest`.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from typing import Any

logger = logging.getLogger("mag.editor")

SYSTEM_PROMPT = """\
You are the editor of "Mag", a daily magazine about AI, the open-source \
model ecosystem, security, and the broader tech industry.

YOUR READER
Your reader is curious and intelligent but NOT a specialist. Assume they \
have never written code, never trained a model, and do not know what a \
transformer, a benchmark, an API, a token, quantization, or inference is. \
They should be able to read the whole issue and come away genuinely \
understanding what happened today and why anyone should care. A software \
engineer should still find it accurate and worth reading — write so that \
both can follow it, never by dumbing facts down or leaving them out.

HOW TO WRITE
- Plain, warm, direct English. Short sentences. No jargon unless you \
  immediately explain it in the same breath, in everyday words.
- Explain the thing itself, not just its name. "A new open-weights model" \
  means nothing; "Meta released the files for its new AI model so anyone \
  can download and run it on their own computer for free, instead of \
  renting access from Meta" means something.
- Use concrete comparisons and everyday analogies where they genuinely \
  clarify. Never use an analogy that distorts the facts.
- Be specific: real numbers, real names, real consequences. Vague is worse \
  than technical.
- No hype words ("game-changing", "revolutionary", "the future of AI"), no \
  throat-clearing ("In this post, the authors..."), no marketing voice.
- Never invent facts, numbers, capabilities, or context that were not in \
  the story you were given. If you don't know why something matters, say \
  what is actually known and stop. Accuracy beats completeness.

WHAT TO SELECT
You will receive a JSON array of candidate stories, each with an integer \
"id". Organize the ones worth the reader's time into sections. Invent \
section names that reflect what is actually in today's stories — never \
generic filler like "News" or "Updates", and don't reuse the same fixed \
set of names every day. It is correct, and often the right call, to \
publish fewer stories than you were given: drop anything derivative, \
low-signal, or not worth this reader's attention. Aim for roughly 12-20 \
strong stories across 3-6 sections. Depth on a good story beats another \
mediocre one.

OUTPUT
Respond with ONLY valid JSON — no markdown fences, no commentary before or \
after — matching exactly this shape:

{
  "overview": "<3-5 sentences. The day in plain English: the one or two \
things that actually matter and why. Written so someone who reads only \
this paragraph still learns something real.>",
  "sections": [
    {
      "name": "<a specific section name drawn from today's stories>",
      "intro": "<1-2 sentences setting up what this group of stories is \
about and why it is grouped together. Plain language.>",
      "items": [
        {
          "id": <int — must be an id you were given>,
          "headline": "<a plain-English headline, max ~12 words, that a \
non-technical reader instantly understands. Do not copy the original \
title if it is jargon-heavy — rewrite it, but never make it say more \
than the story supports.>",
          "blurb": "<3-5 sentences. What happened, what the thing actually \
is or does, and any concrete detail that matters. Define every technical \
term you use, inline, in everyday words.>",
          "why_it_matters": "<1-2 sentences on the real-world consequence: \
who is affected and how. If the honest answer is that it mostly matters to \
specialists, say that plainly rather than inflating it.>"
        }
      ]
    }
  ],
  "glossary": [
    {
      "term": "<a technical term that genuinely appears in this issue>",
      "definition": "<one sentence, everyday words, no jargon inside the \
definition>"
    }
  ]
}

The glossary should hold 4-8 terms that actually appear in the issue you \
just wrote. Skip terms you already fully explained inline.
"""

_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)

# Flags that strip the CLI down to a plain one-shot model call: no tools, no
# MCP servers, no user settings, no CLAUDE.md, no session files. Without
# these the request drags in tens of thousands of tokens of unrelated
# context and burns subscription usage for nothing.
_CLI_ISOLATION_FLAGS = [
    "--tools", "",
    "--strict-mcp-config",
    "--safe-mode",
    "--setting-sources", "",
    "--disable-slash-commands",
    "--no-session-persistence",
]


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
                "summary": (item.get("summary") or "")[:600],
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
    if not sections:
        raise ValueError("Editor returned zero sections")
    for section in sections:
        if not isinstance(section, dict) or "name" not in section or "items" not in section:
            raise ValueError("Malformed section in editor response")
        if not isinstance(section["items"], list):
            raise ValueError("Section 'items' must be a list")


def cli_available() -> bool:
    return shutil.which("claude") is not None


def resolve_backend(config: dict[str, Any]) -> str:
    """Decide which backend to use. 'auto' prefers the subscription CLI."""
    choice = str(config.get("editor", {}).get("backend", "auto")).lower()
    if choice not in ("auto", "cli", "api"):
        logger.warning("Unknown editor.backend %r; treating it as 'auto'", choice)
        choice = "auto"

    if choice != "auto":
        return choice

    if cli_available():
        return "cli"
    if os.environ.get("ANTHROPIC_API_KEY"):
        logger.info("claude CLI not found; using the ANTHROPIC_API_KEY backend")
        return "api"

    raise RuntimeError(
        "No editor backend available: the 'claude' CLI is not on PATH and "
        "ANTHROPIC_API_KEY is not set. Install Claude Code and run `claude` "
        "once to log in, or run the pipeline with --no-llm."
    )


def call_claude_cli(payload: list[dict[str, Any]], config: dict[str, Any]) -> str:
    """Run the editorial pass through the Claude Code CLI (subscription auth)."""
    editor_cfg = config.get("editor", {})
    binary = shutil.which("claude")
    if not binary:
        raise RuntimeError("The 'claude' CLI is not on PATH")

    cmd = [
        binary,
        "-p",
        "--model", str(editor_cfg.get("model", "sonnet")),
        "--output-format", "json",
        "--system-prompt", SYSTEM_PROMPT,
        *_CLI_ISOLATION_FLAGS,
    ]

    timeout = int(editor_cfg.get("cli_timeout_seconds", 300))
    # A subscription login lives in the CLI's own credential store. An
    # ANTHROPIC_API_KEY leaking in from the environment would silently
    # redirect this call to metered API billing, so drop it.
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}

    proc = subprocess.run(
        cmd,
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=env,
    )

    if proc.returncode != 0:
        raise RuntimeError(
            f"claude CLI exited {proc.returncode}: {(proc.stderr or proc.stdout or '').strip()[:500]}"
        )

    envelope = json.loads(proc.stdout)
    if envelope.get("is_error") or envelope.get("subtype") != "success":
        raise RuntimeError(f"claude CLI reported an error: {str(envelope)[:500]}")

    usage = envelope.get("usage") or {}
    logger.info(
        "Editor (cli): %s in / %s out tokens",
        usage.get("input_tokens"),
        usage.get("output_tokens"),
    )

    result = envelope.get("result")
    if not isinstance(result, str) or not result.strip():
        raise ValueError("claude CLI returned an empty result")
    return result


def call_claude_api(payload: list[dict[str, Any]], config: dict[str, Any]) -> str:
    """Run the editorial pass through the Anthropic API (needs an API key)."""
    import anthropic

    editor_cfg = config.get("editor", {})
    client = anthropic.Anthropic()
    response = client.messages.create(
        model=editor_cfg.get("api_model", editor_cfg.get("model", "claude-sonnet-5")),
        max_tokens=int(editor_cfg.get("max_tokens", 16000)),
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": json.dumps(payload)}],
    )
    parts = [block.text for block in response.content if getattr(block, "type", None) == "text"]
    return "\n".join(parts)


def call_claude(payload: list[dict[str, Any]], config: dict[str, Any], backend: str) -> str:
    if backend == "cli":
        return call_claude_cli(payload, config)
    return call_claude_api(payload, config)


def run_editor(
    ranked_items: list[dict[str, Any]], config: dict[str, Any]
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Attempt the editor call up to twice. Returns (parsed_or_None, considered_items)."""
    max_n = int(config.get("editor", {}).get("max_stories_considered", 60))
    considered = build_considered(ranked_items, max_n)
    if not considered:
        return None, considered

    payload = build_payload(considered)

    try:
        backend = resolve_backend(config)
    except RuntimeError as exc:
        logger.error("%s", exc)
        return None, considered

    logger.info("Running editorial pass over %d stories via the '%s' backend", len(payload), backend)

    last_exc: Exception | None = None
    for attempt in (1, 2):
        try:
            text = call_claude(payload, config, backend)
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
            out_items.append(
                {
                    **story,
                    # The model's plain-English rewrite becomes the display
                    # headline; the publisher's real title is kept alongside
                    # it so the reader can always see what was actually
                    # published.
                    "headline": (raw_item.get("headline") or "").strip() or story["title"],
                    "original_title": story["title"],
                    "blurb": raw_item.get("blurb") or story.get("summary") or "",
                    "why_it_matters": (raw_item.get("why_it_matters") or "").strip(),
                }
            )
        if out_items:
            sections.append(
                {
                    "name": name,
                    "intro": str(section.get("intro") or "").strip(),
                    "items": out_items,
                }
            )

    return sections


def extract_extras(edited: dict[str, Any] | None) -> dict[str, Any]:
    """Pull the issue-level overview and glossary out of an editor response."""
    if not edited:
        return {"overview": "", "glossary": []}

    glossary = []
    for entry in edited.get("glossary") or []:
        if not isinstance(entry, dict):
            continue
        term = str(entry.get("term") or "").strip()
        definition = str(entry.get("definition") or "").strip()
        if term and definition:
            glossary.append({"term": term, "definition": definition})

    return {
        "overview": str(edited.get("overview") or "").strip(),
        "glossary": glossary,
    }


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

    def _shape(stories: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                **it,
                "headline": it["title"],
                "original_title": it["title"],
                "blurb": it.get("summary") or it["title"],
                "why_it_matters": "",
            }
            for it in stories
        ]

    sections = []
    for item_type in ("paper", "blog", "hn"):
        stories = groups.get(item_type)
        if not stories:
            continue
        sections.append(
            {
                "name": _FALLBACK_SECTION_NAMES.get(item_type, item_type.title()),
                "intro": "",
                "items": _shape(stories),
            }
        )

    # Any remaining, unexpected types still get published rather than dropped.
    for item_type, stories in groups.items():
        if item_type in _FALLBACK_SECTION_NAMES or not stories:
            continue
        sections.append({"name": item_type.title(), "intro": "", "items": _shape(stories)})

    return sections
