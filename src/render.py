"""
Render the ranked, edited sections into an email-safe HTML magazine and a
plain-text alternative.

The HTML is table-based with inline CSS only (no flex/grid, nothing that
depends on a <style> block surviving email-client sanitization), capped at
640px, with Georgia headlines, a system sans-serif body, and a monospace
utility rail showing each story's rank number and a 5-cell score meter.
"""

from __future__ import annotations

import os
import textwrap
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

_TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates")


def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(_TEMPLATE_DIR),
        autoescape=select_autoescape(["html", "j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )


def prepare_sections(sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach a 1-5 filled/empty score meter to every item, relative to the
    highest-scoring story in the whole digest. Idempotent."""
    all_items = [item for section in sections for item in section.get("items", [])]
    if not all_items:
        return sections

    max_score = max((item.get("score") or 0.0) for item in all_items) or 1.0
    for item in all_items:
        ratio = (item.get("score") or 0.0) / max_score
        filled = max(1, min(5, round(ratio * 5)))
        item["meter_filled"] = filled
        item["meter_empty"] = 5 - filled

    return sections


def render_html(sections: list[dict[str, Any]], meta: dict[str, Any]) -> str:
    prepare_sections(sections)
    template = _env().get_template("magazine.html.j2")
    return template.render(sections=sections, meta=meta)


def _wrap(text: str, indent: str = "      ", width: int = 74) -> list[str]:
    """Hard-wrap a paragraph so the plain-text edition stays readable in
    terminals and text-only mail clients."""
    if not text:
        return []
    return textwrap.wrap(text, width=width, initial_indent=indent, subsequent_indent=indent)


def render_text(sections: list[dict[str, Any]], meta: dict[str, Any]) -> str:
    prepare_sections(sections)
    lines: list[str] = []
    lines.append(f"MAG — {meta.get('date', '')}")
    lines.append(f"{meta.get('total_stories', 0)} stories from {meta.get('sources_count', 0)} sources")
    lines.append("=" * 78)

    if meta.get("overview"):
        lines.append("")
        lines.append("START HERE")
        lines.extend(_wrap(meta["overview"], indent=""))

    for section in sections:
        lines.append("")
        lines.append("")
        lines.append(section["name"].upper())
        lines.append("-" * len(section["name"]))
        if section.get("intro"):
            lines.extend(_wrap(section["intro"], indent=""))
        lines.append("")
        for item in section.get("items", []):
            rank = item.get("rank_position", "?")
            meter = "#" * item.get("meter_filled", 0) + "." * item.get("meter_empty", 0)
            lines.append(f"[{rank:>3}] ({meter}) {item.get('headline') or item['title']}")
            lines.append(f"      {item.get('source', '')}")
            lines.extend(_wrap(item.get("blurb") or ""))
            if item.get("why_it_matters"):
                lines.append("      WHY IT MATTERS:")
                lines.extend(_wrap(item["why_it_matters"], indent="        "))
            original = item.get("original_title")
            if original and original != (item.get("headline") or item["title"]):
                lines.extend(_wrap(f'Published as: "{original}"'))
            also_seen = item.get("also_seen") or []
            if also_seen:
                sources = ", ".join(a["source"] for a in also_seen)
                lines.extend(_wrap(f"Also covered by {sources}"))
            lines.append(f"      {item['url']}")
            lines.append("")

    if meta.get("glossary"):
        lines.append("")
        lines.append("WORDS USED IN THIS ISSUE")
        lines.append("-" * 24)
        for entry in meta["glossary"]:
            lines.append(f"  {entry['term']}")
            lines.extend(_wrap(entry["definition"], indent="    "))
            lines.append("")

    lines.append("-" * 78)
    lines.append(meta.get("footer_note", ""))
    return "\n".join(lines)
