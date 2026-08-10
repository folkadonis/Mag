"""
Mag — Daily AI News Digest.

Orchestrates fetch -> rank -> edit -> render -> mail. Run with `--dry-run`
to build and archive the digest without sending mail, and `--no-llm` to
skip the Claude editor call and publish the unedited, source-grouped feed.
"""

from __future__ import annotations

import argparse
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import yaml

from src import editor, mailer, rank, render, sources

logger = logging.getLogger("mag.main")

REPO_ROOT = Path(__file__).resolve().parent.parent
ARCHIVE_DIR = REPO_ROOT / "archive"


def load_config(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def build_meta(config: dict, fetched: list[dict], sections: list[dict], edited: bool) -> dict:
    now = datetime.now(timezone.utc)
    total_stories = sum(len(section.get("items", [])) for section in sections)
    sources_count = len({item.get("source") for item in fetched if item.get("source")})
    email_cfg = config.get("email", {})
    date_str = now.strftime("%B %-d, %Y") if os.name != "nt" else now.strftime("%B %d, %Y")

    return {
        "date": date_str,
        "subject": f"{email_cfg.get('subject_prefix', 'Mag')} — {date_str}",
        "total_stories": total_stories,
        "sources_count": sources_count,
        "footer_note": "edited by Claude" if edited else "unedited feed digest",
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build and send the Mag daily AI digest.")
    parser.add_argument("--dry-run", action="store_true", help="Build and archive the digest without emailing it.")
    parser.add_argument("--no-llm", action="store_true", help="Skip the Claude editor call; publish the raw feed digest.")
    parser.add_argument("--config", default=str(REPO_ROOT / "config.yaml"), help="Path to config.yaml.")
    return parser.parse_args(argv)


def run(argv: list[str] | None = None) -> Path:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = parse_args(argv)

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    config = load_config(Path(args.config))

    logger.info("Fetching sources...")
    fetched = sources.fetch_all(config)
    logger.info("Fetched %d raw stories", len(fetched))

    ranked = rank.rank(fetched, config)
    logger.info("%d stories survived topic filter + dedupe", len(ranked))

    max_n = int(config.get("editor", {}).get("max_stories_considered", 60))
    edited_ok = False

    if args.no_llm:
        considered = editor.build_considered(ranked, max_n)
        sections = editor.fallback_digest(considered)
    else:
        parsed, considered = editor.run_editor(ranked, config)
        if parsed is not None:
            sections = editor.hydrate(parsed, considered)
            edited_ok = True
        else:
            sections = editor.fallback_digest(considered)

    meta = build_meta(config, fetched, sections, edited_ok)

    html_body = render.render_html(sections, meta)
    text_body = render.render_text(sections, meta)

    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    dated_path = ARCHIVE_DIR / f"{today_str}.html"
    latest_path = ARCHIVE_DIR / "latest.html"
    dated_path.write_text(html_body, encoding="utf-8")
    latest_path.write_text(html_body, encoding="utf-8")
    logger.info("Wrote %s and %s", dated_path, latest_path)

    if args.dry_run:
        logger.info("--dry-run set: skipping email send. %d stories in %d sections.", meta["total_stories"], len(sections))
    else:
        mailer.send_email(meta["subject"], html_body, text_body)

    return latest_path


if __name__ == "__main__":
    run()
