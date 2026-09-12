# Repo notes for Claude

This repo has two automated jobs:

1. **README builder** — `build_readme.py` + `.github/workflows/build.yml`. Hourly GitHub Action; not your concern unless asked.
2. **Podcast tracker** — `podcast_tracker/`. Runs as a scheduled Claude Code session twice weekly.

## When the session prompt is "Run the podcast tracker"

Open `podcast_tracker/AGENT_PLAYBOOK.md` and follow it step-by-step. It tells you exactly which scripts to run, the digest format, where to commit, and how to email the result.

Key facts:
- Branch: `claude/podcast-tracking-agent-SgDto`
- Email recipient: `vpst6tdbw2@privaterelay.appleid.com`
- Style: detailed structured notes (per-topic breakdown, see playbook for skeleton)
- Source: show notes + best-effort transcript scrape
- Do not commit `podcast_tracker/cache/pending_episodes.json` (gitignored).
