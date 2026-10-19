# Mag — Daily AI Digest

A daily email magazine about AI, open-source models, security, and the wider
tech industry. It fetches ~25 sources, filters and ranks them, has Claude
write the issue in plain English, and mails it to you.

The editorial pass calls Claude. `editor.backend` in `config.yaml` picks how:
`api` (the default here) uses `ANTHROPIC_API_KEY`, billed per-use, no CLI or
subscription needed. `cli` runs through the Claude Code CLI instead, billing
against a Pro/Max subscription — set that only if you'd rather not pay
per-use and are willing to mint a token from a terminal (see below).

## What the issue looks like

Written for a curious reader who is *not* a specialist. Every issue has:

- **Start here** — a short plain-English summary of the day.
- **Sections** with names drawn from what actually happened, each with a
  one-line intro.
- **Stories** with a plain-language headline, a 3-5 sentence explanation that
  defines its own jargon, a **Why it matters** note, and the publisher's real
  headline shown underneath so nothing is obscured.
- **Words used in this issue** — a glossary of the technical terms that came up.

A software engineer should still find it accurate; a non-technical reader
should be able to follow all of it.

## Pipeline

```
sources.fetch_all   ~25 feeds in parallel, each with a recency window
      ↓
rank.rank           topic filter → Jaccard title dedupe → recency/weight score
      ↓
seen.filter_unseen  drop anything already published in an earlier issue
      ↓
editor.run_editor   one Claude call: select, group, and write in plain English
      ↓
render              email-safe HTML (tables + inline CSS) and a plain-text copy
      ↓
mailer.send_email   Gmail SMTP, or Resend if RESEND_API_KEY is set
```

Every stage degrades instead of failing. A dead feed is skipped, and if the
Claude call fails twice the run still publishes an unedited digest grouped by
source type.

## Setup

### 1. Install

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt   # Windows
# source .venv/bin/activate && pip install -r requirements.txt   # macOS/Linux
```

With the default `editor.backend: api`, set `ANTHROPIC_API_KEY` (from
console.anthropic.com) in your `.env` — that's it, no CLI needed.

To use a Claude Pro/Max subscription instead, set `editor.backend: cli` in
`config.yaml` and install the Claude Code CLI on your PATH, logged in once
interactively:

```bash
npm install -g @anthropic-ai/claude-code
claude          # log in with your Pro account, then quit
```

### 2. Configure email

```bash
cp .env.example .env
```

Fill in `.env`. For Gmail, `SMTP_PASS` must be a 16-character **App Password**
from <https://myaccount.google.com/apppasswords> (2-Step Verification has to be
enabled on the account first) — a normal Google password will be rejected.

### 3. Run

```bash
.venv/Scripts/python -m src.main --dry-run     # build + archive, no email
.venv/Scripts/python -m src.main               # build + archive + send
```

| Flag | Effect |
| --- | --- |
| `--dry-run` | Build and archive the issue without emailing it. |
| `--no-llm` | Skip the Claude call; publish the raw feed digest. |
| `--ignore-seen` | Allow stories already published in an earlier issue. |
| `--record-seen` | Update the seen store even on a dry run. |
| `--config PATH` | Use a different config file. |

Output lands in `archive/YYYY-MM-DD.html` and `archive/latest.html`.

## Scheduling on GitHub Actions

`.github/workflows/daily.yml` runs the pipeline at 01:00 UTC and commits the
archive back to the repo.

Add these repository secrets (Settings → Secrets and variables → Actions):

| Secret | Value |
| --- | --- |
| `ANTHROPIC_API_KEY` | From console.anthropic.com. Only needed with the default `editor.backend: api`. |
| `SMTP_HOST` | `smtp.gmail.com` |
| `SMTP_PORT` | `465` |
| `SMTP_USER` | Your Gmail address |
| `SMTP_PASS` | Your 16-character Google App Password |
| `EMAIL_TO` | Where to send the digest |
| `EMAIL_FROM` | Optional; defaults to `Mag <SMTP_USER>` |

Trigger a manual run from the Actions tab to test before relying on the cron.

If you switch `editor.backend` to `cli` instead, replace `ANTHROPIC_API_KEY`
with a `CLAUDE_CODE_OAUTH_TOKEN` secret (output of `claude setup-token`, run
on a machine where you're logged into your Pro/Max subscription), and add
back the `Set up Node` + `Install Claude Code` steps to `daily.yml`.

Email is optional: if `EMAIL_TO` isn't set, `src/main.py` skips sending and just
writes the archive — see **Hosting on Vercel** below for a mail-free setup.
A send failure (bad SMTP creds, etc.) is logged and swallowed rather than
crashing the run, so the archive always gets committed either way.

## Hosting on Vercel

Skip email entirely and just read the issue at a URL. The daily workflow
already commits `archive/latest.html` on every run; `vercel.json` rewrites
`/` to it, so once the repo is connected the site always shows the newest
issue with zero extra secrets.

1. [vercel.com](https://vercel.com) → **Add New… → Project** → import
   `folkadonis/Mag`.
2. Framework Preset: **Other**. Leave Build Command and Output Directory
   blank — there is nothing to build, it's served as static files.
3. Deploy. Every push to the default branch (including the daily archive
   commit from GitHub Actions) triggers a redeploy automatically.
4. Visit the assigned `*.vercel.app` URL — it shows the latest issue.
   Older issues stay reachable at `/archive/YYYY-MM-DD.html`.

## Tuning

Everything tunable lives in `config.yaml` — feeds and their weights, topic
keywords, the recency and dedupe thresholds, and the editor settings. Notably:

- `sources.lookback_hours` (default 168) — how far back feeds are read. It is
  deliberately wide because most engineering blogs publish weekly, not daily;
  the seen store is what prevents repeats.
- `seen.retention_days` (default 30) — how long a published story is
  remembered. `archive/seen.json` holds this memory and is committed by CI.
- `editor.backend` — `api` (default here; needs `ANTHROPIC_API_KEY`), `cli`
  (subscription auth via the Claude Code CLI), or `auto` (CLI if installed,
  else API key — avoid in CI, since installing the CLI makes `auto` always
  pick `cli` regardless of whether it's actually authenticated).
- `editor.max_stories_considered` (default 60) — how many ranked stories the
  editor gets to choose from. It typically publishes 12-20.

To change the voice of the magazine, edit `SYSTEM_PROMPT` in `src/editor.py`.

## Notes

- The editor never sees URLs. It refers to stories by integer id only, and
  `editor.hydrate` splices the real links back in, dropping any id the model
  invented. That is what makes fabricated links structurally impossible.
- The CLI call is deliberately stripped down (`--tools ""`,
  `--strict-mcp-config`, `--safe-mode`, `--setting-sources ""`). Without those
  flags it drags in ~62k tokens of unrelated local context per run.
- Anthropic and xAI publish no RSS feed, so they are covered via targeted
  Google News queries. Uber and X Engineering block automated feed access and
  were dropped.

## Tests

```bash
.venv/Scripts/python -m pytest tests/smoke_test.py -v
```

Fully offline — no network, no model calls.
